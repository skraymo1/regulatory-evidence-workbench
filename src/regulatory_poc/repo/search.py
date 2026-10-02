from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Protocol

from regulatory_poc.repo.embeddings import EmbeddingProvider
from regulatory_poc.types.models import (
    DocumentChunk,
    DocumentMetadata,
    SearchFilters,
    SearchHit,
)


TOKEN_PATTERN = re.compile(r"\w+", re.UNICODE)
CONCEPTS = {
    "safety": {
        "safety", "surete", "sûreté", "securite", "sécurité", "безопасность",
        "безопасности", "안전",
    },
    "reactor": {"reactor", "reacteur", "réacteur", "реактор", "реактора", "원자로"},
    "risk": {"risk", "risque", "риск", "риска", "위험"},
    "emergency": {"emergency", "urgence", "авария", "аварийный", "비상"},
    "defence": {
        "defence",
        "defense",
        "depth",
        "défense",
        "profondeur",
        "глубокоэшелонированная",
        "защита",
        "심층방어",
    },
    "requirement": {
        "requirement", "requirements", "exigence", "exigences", "требование",
        "требования", "요건",
    },
}


def tokenize(value: str) -> set[str]:
    tokens = {
        token.casefold()
        for token in TOKEN_PATTERN.findall(value)
        if len(token) > 2 or not token.isascii()
    }
    for concept, variants in CONCEPTS.items():
        if tokens & variants:
            tokens.add(concept)
    return tokens


class SearchRepository(Protocol):
    def upsert(self, chunks: list[DocumentChunk]) -> None: ...

    def search(
        self,
        query: str,
        top_k: int,
        document_id: str | None = None,
        filters: SearchFilters | None = None,
    ) -> list[SearchHit]: ...

    def list_documents(self) -> list[DocumentMetadata]: ...

    def delete_document(self, document_id: str) -> None: ...


class JsonSearchRepository:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._chunks: dict[str, DocumentChunk] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            values = json.loads(self._path.read_text(encoding="utf-8"))
            self._chunks = {
                chunk.chunk_id: chunk
                for chunk in (DocumentChunk.from_dict(value) for value in values)
            }
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"Local index '{self._path}' is invalid. Remove it and ingest the documents again."
            ) from exc

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = [
            self._chunks[key].to_dict()
            for key in sorted(self._chunks)
        ]
        self._path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def upsert(self, chunks: list[DocumentChunk]) -> None:
        for chunk in chunks:
            self._chunks[chunk.chunk_id] = chunk
        self._save()

    def search(
        self,
        query: str,
        top_k: int,
        document_id: str | None = None,
        filters: SearchFilters | None = None,
    ) -> list[SearchHit]:
        query_tokens = tokenize(query)
        if not query_tokens:
            return []
        hits: list[SearchHit] = []
        for chunk in self._chunks.values():
            if document_id and chunk.metadata.document_id != document_id:
                continue
            if filters and not _matches(chunk.metadata, filters):
                continue
            chunk_tokens = tokenize(chunk.text)
            overlap = query_tokens & chunk_tokens
            if not overlap:
                continue
            coverage = len(overlap) / len(query_tokens)
            specificity = len(overlap) / math.sqrt(max(len(chunk_tokens), 1))
            hits.append(SearchHit(chunk=chunk, score=coverage + specificity))
        return sorted(
            hits,
            key=lambda hit: (-hit.score, hit.chunk.metadata.document_id, hit.chunk.ordinal),
        )[:top_k]

    def list_documents(self) -> list[DocumentMetadata]:
        documents = {
            chunk.metadata.document_id: chunk.metadata for chunk in self._chunks.values()
        }
        return sorted(documents.values(), key=lambda value: (value.authority, value.title))

    def delete_document(self, document_id: str) -> None:
        self._chunks = {
            key: chunk
            for key, chunk in self._chunks.items()
            if chunk.metadata.document_id != document_id
        }
        self._save()


class AzureAISearchRepository:
    def __init__(
        self,
        endpoint: str,
        index_name: str,
        semantic_configuration: str,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        try:
            from azure.identity import DefaultAzureCredential
            from azure.search.documents import SearchClient
        except ImportError as exc:
            raise RuntimeError(
                "Azure AI Search mode requires azure-identity and azure-search-documents. "
                "Run 'python -m pip install --pre -e .'."
            ) from exc
        self._client = SearchClient(
            endpoint=endpoint,
            index_name=index_name,
            credential=DefaultAzureCredential(),
        )
        self._semantic_configuration = semantic_configuration
        self._embedding_provider = embedding_provider

    def upsert(self, chunks: list[DocumentChunk]) -> None:
        documents = []
        for chunk in chunks:
            document = {
                "id": chunk.chunk_id,
                "content": chunk.text,
                "ordinal": chunk.ordinal,
                **chunk.metadata.to_dict(),
            }
            if self._embedding_provider:
                document["content_vector"] = self._embedding_provider.embed(chunk.text)
            documents.append(document)
        results = self._client.upload_documents(documents=documents)
        failures = [result.key for result in results if not result.succeeded]
        if failures:
            raise RuntimeError(
                f"Azure AI Search rejected {len(failures)} chunks: {', '.join(failures[:5])}."
            )

    def search(
        self,
        query: str,
        top_k: int,
        document_id: str | None = None,
        filters: SearchFilters | None = None,
    ) -> list[SearchHit]:
        arguments: dict[str, object] = {
            "search_text": query,
            "filter": _odata_filter(document_id, filters),
            "query_type": "semantic",
            "semantic_configuration_name": self._semantic_configuration,
            "top": top_k,
        }
        if self._embedding_provider:
            from azure.search.documents.models import VectorizedQuery

            arguments["vector_queries"] = [
                VectorizedQuery(
                    vector=self._embedding_provider.embed(query),
                    k_nearest_neighbors=max(top_k * 3, 20),
                    fields="content_vector",
                )
            ]
        results = self._client.search(**arguments)
        hits: list[SearchHit] = []
        for result in results:
            metadata = DocumentMetadata.from_dict(result)
            chunk = DocumentChunk(
                chunk_id=str(result["id"]),
                text=str(result["content"]),
                ordinal=int(result["ordinal"]),
                metadata=metadata,
            )
            hits.append(SearchHit(chunk=chunk, score=float(result.get("@search.score", 0.0))))
        return hits

    def list_documents(self) -> list[DocumentMetadata]:
        results = self._client.search(
            search_text="*",
            select=list(DocumentMetadata.__dataclass_fields__),
        )
        documents = {
            str(result["document_id"]): DocumentMetadata.from_dict(result)
            for result in results
        }
        return sorted(documents.values(), key=lambda value: (value.authority, value.title))

    def delete_document(self, document_id: str) -> None:
        escaped = document_id.replace("'", "''")
        results = self._client.search(
            search_text="*",
            filter=f"document_id eq '{escaped}'",
            select=["id"],
        )
        keys = [{"id": str(result["id"])} for result in results]
        for start in range(0, len(keys), 1000):
            outcomes = self._client.delete_documents(documents=keys[start : start + 1000])
            failures = [result.key for result in outcomes if not result.succeeded]
            if failures:
                raise RuntimeError(
                    f"Azure AI Search failed to delete {len(failures)} chunks: "
                    f"{', '.join(failures[:5])}."
                )


def _matches(metadata: DocumentMetadata, filters: SearchFilters) -> bool:
    checks = (
        (filters.document_ids, metadata.document_id),
        (filters.countries, metadata.country),
        (filters.reactor_types, metadata.reactor_type),
        (filters.languages, metadata.language),
        (filters.standards, metadata.standard),
    )
    return all(
        not allowed or value.casefold() in {item.casefold() for item in allowed}
        for allowed, value in checks
    )


def _odata_filter(document_id: str | None, filters: SearchFilters | None) -> str | None:
    clauses: list[str] = []
    if document_id:
        clauses.append(_odata_equals("document_id", document_id))
    if filters:
        fields = {
            "document_id": filters.document_ids,
            "country": filters.countries,
            "reactor_type": filters.reactor_types,
            "language": filters.languages,
            "standard": filters.standards,
        }
        for field, values in fields.items():
            if values:
                clauses.append(
                    "(" + " or ".join(_odata_equals(field, value) for value in values) + ")"
                )
    return " and ".join(clauses) or None


def _odata_equals(field: str, value: str) -> str:
    escaped = value.replace("'", "''")
    return f"{field} eq '{escaped}'"
