from __future__ import annotations

from functools import lru_cache
from dataclasses import asdict
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response
from azure.core.exceptions import AzureError
from pydantic import BaseModel, Field

from regulatory_poc.config.settings import Settings
from regulatory_poc.repo.documents import chunk_document, extract_text, make_document_id
from regulatory_poc.repo.sources import BlobSourceConnector, SharePointSourceConnector
from regulatory_poc.runtime.container import (
    build_chat_service,
    build_comparison_service,
    build_search_repository,
    build_sync_service,
    build_report_search,
)
from regulatory_poc.service.authorization import authorize_principal
from regulatory_poc.service.uploads import ingest_bytes
from regulatory_poc.service.ingestion import SourceSyncService
from regulatory_poc.ui.regulatory_api import router as regulatory_router
from regulatory_poc.types.models import (
    ChatRequest,
    ComparisonRequest,
    DocumentMetadata,
    SearchFilters,
)


class ComparisonBody(BaseModel):
    question: str = Field(min_length=1)
    document_a_id: str = Field(min_length=1)
    document_b_id: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=20)


class FilterBody(BaseModel):
    document_ids: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    reactor_types: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    standards: list[str] = Field(default_factory=list)

    def to_filters(self) -> SearchFilters:
        return SearchFilters(
            document_ids=tuple(self.document_ids),
            countries=tuple(self.countries),
            reactor_types=tuple(self.reactor_types),
            languages=tuple(self.languages),
            standards=tuple(self.standards),
        )


class SearchBody(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(default=8, ge=1, le=50)
    filters: FilterBody = Field(default_factory=FilterBody)


class ChatBody(SearchBody):
    conversation_id: str = ""


app = FastAPI(title="Regulatory Evidence Workbench", version="0.1.0")
app.include_router(regulatory_router)


@app.exception_handler(AzureError)
async def azure_request_error(request, exc: AzureError):
    return JSONResponse(
        {"detail": f"Azure request failed ({type(exc).__name__}); check credentials/service availability and retry."},
        status_code=503,
    )


@app.middleware("http")
async def require_account(request, call_next):
    settings = Settings.from_env()
    if settings.allowed_oid and not authorize_principal(
        request.headers, settings.allowed_oid, settings.tenant_id
    ):
        return JSONResponse({"detail": "Approved Entra account required."}, status_code=403)
    return await call_next(request)


@lru_cache
def _runtime() -> tuple[Settings, object, object, object]:
    settings = Settings.from_env()
    settings.validate()
    repository = build_search_repository(settings)
    service = build_comparison_service(settings, repository)
    chat = build_chat_service(settings, repository)
    return settings, repository, service, chat


def _available_runtime() -> tuple[Settings, object, object, object]:
    try:
        return _runtime()
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/health")
def health() -> dict[str, str]:
    try:
        settings, _, _, _ = _runtime()
    except (RuntimeError, ValueError) as exc:
        return {"status": "degraded", "detail": str(exc)}
    return {
        "status": "ok",
        "agent_mode": settings.agent_mode,
        "search_mode": settings.search_mode,
        "model_deployment": settings.model_deployment_name,
        "blob_source": (
            f"{settings.blob_account_url}/{settings.blob_container}"
            if settings.blob_account_url and settings.blob_container
            else ""
        ),
    }


@app.get("/documents")
def list_documents() -> list[dict[str, str]]:
    _, repository, _, _ = _available_runtime()
    return [document.to_dict() for document in repository.list_documents()]


@app.post("/documents", status_code=201)
async def ingest_document(
    file: Annotated[UploadFile, File()],
    title: Annotated[str, Form()],
    authority: Annotated[str, Form()],
    language: Annotated[str, Form()] = "unknown",
    country: Annotated[str, Form()] = "",
    reactor_type: Annotated[str, Form()] = "",
    standard: Annotated[str, Form()] = "",
    revision: Annotated[str, Form()] = "",
    project_number: Annotated[str, Form()] = "",
) -> dict[str, str | int]:
    _, repository, _, _ = _available_runtime()
    content = await file.read()
    document_id = make_document_id(file.filename or "document", content)
    metadata = DocumentMetadata(
        document_id=document_id,
        title=title.strip(),
        authority=authority.strip(),
        language=language.strip() or "unknown",
        country=country.strip(),
        reactor_type=reactor_type.strip(),
        standard=standard.strip(),
        revision=revision.strip(),
        project_number=project_number.strip(),
        source_name=file.filename or "document",
    )
    try:
        count = ingest_bytes(repository, content, metadata)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"document_id": document_id, "chunks_indexed": count}


@app.post("/comparisons")
async def compare_documents(body: ComparisonBody) -> dict[str, object]:
    _, _, service, _ = _available_runtime()
    try:
        result = await service.compare(
            ComparisonRequest(
                question=body.question,
                document_a_id=body.document_a_id,
                document_b_id=body.document_b_id,
                top_k=body.top_k,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _result_dict(result)


@app.post("/search")
def search_documents(body: SearchBody) -> list[dict[str, object]]:
    _, repository, _, _ = _available_runtime()
    hits = repository.search(body.query, body.top_k, filters=body.filters.to_filters())
    return [
        {
            "score": hit.score,
            "text": hit.chunk.text,
            "ordinal": hit.chunk.ordinal,
            "metadata": hit.chunk.metadata.to_dict(),
        }
        for hit in hits
    ]


@app.post("/chat")
async def chat(body: ChatBody) -> dict[str, object]:
    _, _, _, service = _available_runtime()
    result = await service.answer(
        ChatRequest(
            question=body.query,
            conversation_id=body.conversation_id,
            filters=body.filters.to_filters(),
            top_k=body.top_k,
        )
    )
    return {
        "conversation_id": result.conversation_id,
        **_result_dict(result),
    }


@app.post("/sources/{source}/sync")
def sync_source(source: str) -> dict[str, object]:
    settings, repository, _, _ = _available_runtime()
    if source == "blob":
        if not settings.blob_account_url or not settings.blob_container:
            raise HTTPException(status_code=422, detail="Blob source is not configured.")
        connector = BlobSourceConnector(
            settings.blob_account_url, settings.blob_container, settings.blob_prefix
        )
    elif source == "sharepoint":
        if not settings.sharepoint_site_id or not settings.sharepoint_drive_id:
            raise HTTPException(status_code=422, detail="SharePoint source is not configured.")
        connector = SharePointSourceConnector(
            settings.sharepoint_site_id,
            settings.sharepoint_drive_id,
            settings.sharepoint_folder_path,
        )
    else:
        raise HTTPException(status_code=404, detail="Unknown source.")
    if settings.source_knowledge_base and source == "blob":
        raise HTTPException(
            status_code=422, detail="Managed source corpus: use uploads or per-document indexing retry."
        )
    result = build_sync_service(settings, repository).sync(connector)
    return {
        "source": result.source,
        "discovered": result.discovered,
        "indexed": result.indexed,
        "skipped": result.skipped,
        "failed": list(result.failed),
    }


@app.get("/sources/status")
def source_status() -> dict[str, dict[str, str]]:
    settings, repository, _, _ = _available_runtime()
    return build_sync_service(settings, repository).status()


def _result_dict(result: object) -> dict[str, object]:
    return asdict(result)


@app.get("/documents/{document_id}/content")
def download_document(document_id: str):
    _, repository, _, _ = _available_runtime()
    if not getattr(repository, "document_store", None):
        raise HTTPException(status_code=404, detail="Blob document storage is not configured.")
    try:
        content, metadata = repository.document_store.download(document_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(content, media_type="application/octet-stream")


@app.post("/documents/{document_id}/retry-index")
def retry_source_index(document_id: str):
    _, repository, _, _ = _available_runtime()
    content, metadata = repository.document_store.download(document_id)
    return {"chunks_indexed": ingest_bytes(repository, content, metadata)}


def _reports():
    settings, _, service, _ = _available_runtime()
    if not settings.source_knowledge_base:
        raise HTTPException(status_code=404, detail="Saved reports require Azure IQ mode.")
    return settings, service


@app.get("/reports")
def list_reports():
    _, service = _reports()
    return service.list_reports()


@app.get("/reports/{report_id}")
def get_report(report_id: str):
    _, service = _reports()
    return service.get(report_id)


@app.post("/reports/{report_id}/retry-index")
def retry_report_index(report_id: str):
    _, service = _reports()
    return asdict(service.retry_index(report_id))


@app.post("/reports/search")
def search_reports(body: SearchBody):
    settings, _ = _reports()
    return [
        asdict(hit) for hit in build_report_search(settings).search(body.query, body.top_k)
    ]


@app.post("/reports/chat")
async def report_chat(body: ChatBody):
    settings, _ = _reports()
    return asdict(await build_chat_service(settings, reports=True).answer(ChatRequest(
        question=body.query, conversation_id=body.conversation_id, top_k=body.top_k,
    )))
