from __future__ import annotations

from regulatory_poc.repo.documents import chunk_document, extract_text
from regulatory_poc.types.models import DocumentMetadata
from azure.core.exceptions import AzureError
from regulatory_poc.service.usage import timestamp, usage_scope


def ingest_bytes(repository, content: bytes, metadata: DocumentMetadata) -> int:
    store = getattr(repository, "document_store", None)
    if store:
        metadata = store.save(content, metadata)
    try:
        chunks = chunk_document(extract_text(metadata.source_name, content), metadata)
        if not chunks:
            raise ValueError("No text extracted; supply a supported text-based document.")
        with usage_scope(document_ids=[metadata.document_id], operation="document_indexing"):
            repository.delete_document(metadata.document_id)
            repository.upsert(chunks)
    except (RuntimeError, ValueError, OSError, AzureError) as exc:
        if store and hasattr(store, "mark"):
            store.mark(metadata.document_id, "failed", indexing_error=str(exc))
        raise
    if store:
        store.indexed(metadata.document_id)
        if hasattr(store, "mark"):
            store.mark(
                metadata.document_id, "indexed", chunks_indexed=len(chunks),
                indexing_error="", processed_at=timestamp(),
            )
    return len(chunks)
