from __future__ import annotations

from regulatory_poc.repo.documents import chunk_document, extract_text
from regulatory_poc.types.models import DocumentMetadata


def ingest_bytes(repository, content: bytes, metadata: DocumentMetadata) -> int:
    store = getattr(repository, "document_store", None)
    if store:
        metadata = store.save(content, metadata)
    chunks = chunk_document(extract_text(metadata.source_name, content), metadata)
    repository.delete_document(metadata.document_id)
    repository.upsert(chunks)
    if store:
        store.indexed(metadata.document_id)
    return len(chunks)
