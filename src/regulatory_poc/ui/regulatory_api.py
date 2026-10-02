from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from regulatory_poc.config.settings import Settings
from regulatory_poc.runtime.regulatory import build_regulatory_service
from regulatory_poc.types.models import DocumentMetadata


router = APIRouter(prefix="/regulatory", tags=["Requirement-based comparison"])


class CountryTableBody(BaseModel):
    baseline_id: str = Field(min_length=1)
    country: str
    document_ids: list[str] = Field(min_length=1)
    selected_ids: list[str] = Field(default_factory=list)
    reference_id: str = ""
    reference_source_id: str = ""


class RowBody(BaseModel):
    requirement_key: str = Field(min_length=1)


class TranslationBody(RowBody):
    article_key: str = Field(min_length=1)


class ArticleBody(BaseModel):
    article_key: str = Field(min_length=1)


def _service():
    settings = Settings.from_env()
    settings.validate()
    return build_regulatory_service(settings)


def _translate_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=422 if isinstance(exc, ValueError) else 503, detail=str(exc))


@router.get("/documents")
def documents() -> list[dict]:
    try:
        return _service().library.list_documents()
    except (RuntimeError, ValueError, OSError) as exc:
        raise _translate_error(exc) from exc


@router.post("/documents", status_code=201)
async def import_document(
    file: Annotated[UploadFile, File()], role: Annotated[str, Form()],
    language: Annotated[str, Form()], standard: Annotated[str, Form()] = "",
    revision: Annotated[str, Form()] = "",
    jurisdiction: Annotated[str, Form()] = "", extraction_profile: Annotated[str, Form()] = "auto",
) -> dict:
    try:
        return _service().library.import_document(
            await file.read(), file.filename or "source.pdf", role, language, standard, revision,
            jurisdiction, extraction_profile,
        )
    except (RuntimeError, ValueError, OSError) as exc:
        raise _translate_error(exc) from exc


@router.post("/documents/{document_id}/retry-index")
def retry_source(document_id: str) -> dict:
    try:
        return _service().library.retry_index(document_id)
    except (RuntimeError, ValueError, OSError) as exc:
        raise _translate_error(exc) from exc


@router.get("/documents/{document_id}/content")
def original_document(document_id: str) -> Response:
    try:
        service = _service()
        metadata = DocumentMetadata.from_dict(service.library.get(document_id)["metadata"])
        content, _ = service.library.sources.download_version(metadata)
        return Response(content, media_type="application/pdf")
    except (RuntimeError, ValueError, OSError) as exc:
        raise _translate_error(exc) from exc


@router.post("/tables", status_code=201)
def create_table(body: CountryTableBody) -> dict:
    try:
        return _service().create(
            body.baseline_id, body.country, body.document_ids, body.selected_ids, body.reference_id,
            body.reference_source_id,
        )
    except (RuntimeError, ValueError, OSError) as exc:
        raise _translate_error(exc) from exc


@router.get("/tables")
def tables() -> list[dict]:
    try:
        return _service().list_reports()
    except (RuntimeError, ValueError, OSError) as exc:
        raise _translate_error(exc) from exc


@router.get("/tables/{report_id}")
def table(report_id: str) -> dict:
    try:
        return _service().get(report_id)
    except (RuntimeError, ValueError, OSError) as exc:
        raise _translate_error(exc) from exc


@router.post("/tables/{report_id}/process-row")
async def process_row(report_id: str, body: RowBody) -> dict:
    try:
        return await _service().process_row(report_id, body.requirement_key)
    except (RuntimeError, ValueError, OSError) as exc:
        raise _translate_error(exc) from exc


@router.post("/tables/{report_id}/retry-index")
def index_table(report_id: str) -> dict:
    try:
        return _service().index_report(report_id)
    except (RuntimeError, ValueError, OSError) as exc:
        raise _translate_error(exc) from exc


@router.post("/tables/{report_id}/verify-translation")
async def translation_check(report_id: str, body: TranslationBody) -> dict:
    try:
        return await _service().verify_article(report_id, body.requirement_key, body.article_key)
    except (RuntimeError, ValueError, OSError) as exc:
        raise _translate_error(exc) from exc


@router.post("/tables/{report_id}/verify-article")
async def article_check(report_id: str, body: ArticleBody) -> dict:
    try:
        return await _service().verify_scope_article(report_id, body.article_key)
    except (RuntimeError, ValueError, OSError) as exc:
        raise _translate_error(exc) from exc
