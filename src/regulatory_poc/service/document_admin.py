from __future__ import annotations

from regulatory_poc.service.uploads import ingest_bytes
from regulatory_poc.service.usage import UsageTracker, usage_summary
from regulatory_poc.types.models import DocumentMetadata
from regulatory_poc.repo.blob_browser import BlobStorageBrowser


class DocumentAdminService:
    def __init__(
        self, regulatory, repository, usage: UsageTracker,
        blob_browser: BlobStorageBrowser | None = None,
    ) -> None:
        self.regulatory, self.repository, self.usage = regulatory, repository, usage
        self.blob_browser = blob_browser

    def blob_document_ids(self, container: str, name: str) -> list[str]:
        if self.blob_browser is None:
            raise ValueError("Blob storage is not configured.")
        return [
            item["metadata"]["document_id"] for item in self.documents()
            if self.blob_browser.matches_source(item["metadata"].get("source_uri", ""), container, name)
        ]

    @staticmethod
    def _related_reports(document_id: str, reports: list[dict]) -> list[dict]:
        return [
            report for report in reports
            if document_id in {
                report["inputs"]["baseline"]["document_id"],
                *(source["document_id"] for source in report["inputs"]["sources"]),
            }
        ]

    def documents(self) -> list[dict]:
        uploads = {
            item["metadata"]["document_id"]: item for item in self.usage.store.list("documents/")
        }
        inventories = {
            item["metadata"]["document_id"]: item
            for item in self.regulatory.library.list_documents()
        }
        indexed = {item.document_id: item.to_dict() for item in self.repository.list_documents()}
        reports = self.regulatory.list_reports()
        documents = []
        for document_id in sorted(uploads.keys() | inventories.keys() | indexed.keys()):
            upload, inventory = uploads.get(document_id, {}), inventories.get(document_id, {})
            record = {**upload, **inventory}
            metadata = record.get("metadata", indexed.get(document_id))
            related = self._related_reports(document_id, reports)
            documents.append({
                "metadata": metadata, "role": inventory.get("role", "General document"),
                "indexing_status": record.get("indexing_status", "indexed"),
                "uploaded_at": record.get("uploaded_at"), "processed_at": record.get("processed_at"),
                "size_bytes": record.get("size_bytes"), "chunks_indexed": record.get("chunks_indexed"),
                "requirement_count": len(inventory.get("requirements", [])),
                "extraction_complete": inventory.get("complete"),
                "indexing_error": record.get("indexing_error", ""),
                "approval_status": record.get("approval_status", "unknown"),
                "warnings": inventory.get("warnings", []),
                "original_available": bool(upload or inventory),
                "comparison_count": len(related),
                "comparison_rows_processed": sum(
                    row["status"] not in {"pending", "error"} for report in related for row in report["rows"]
                ),
                "comparison_rows_pending": sum(
                    row["status"] == "pending" for report in related for row in report["rows"]
                ),
                "comparison_rows_failed": sum(
                    row["status"] == "error" for report in related for row in report["rows"]
                ),
            })
        return documents

    def processed(self, document_id: str) -> dict:
        record = next((item for item in self.documents()
                       if item["metadata"]["document_id"] == document_id), None)
        if record is None:
            raise ValueError("Document not found; select an uploaded or indexed document.")
        inventory = next((item for item in self.regulatory.library.list_documents()
                          if item["metadata"]["document_id"] == document_id), {})
        return {
            "document": record,
            "requirements": inventory.get("requirements", []),
            "chunks": [chunk.to_dict() for chunk in self.repository.list_chunks(document_id)],
            "comparisons": self._related_reports(document_id, self.regulatory.list_reports()),
        }

    def retry(self, document_id: str) -> None:
        if any(item["metadata"]["document_id"] == document_id
               for item in self.regulatory.library.list_documents()):
            self.regulatory.library.retry_processing(document_id)
            return
        record = next((item for item in self.usage.store.list("documents/")
                       if item["metadata"]["document_id"] == document_id), None)
        if record is None:
            raise ValueError("Saved source not found; upload the document again.")
        store = getattr(self.repository, "document_store", None)
        if store is None:
            raise ValueError("Source storage is unavailable; upload the document again.")
        metadata = DocumentMetadata.from_dict(record["metadata"])
        content, _ = store.download_version(metadata)
        ingest_bytes(self.repository, content, metadata)

    def original(self, document_id: str) -> tuple[bytes, DocumentMetadata]:
        record = next((item for item in self.documents()
                       if item["metadata"]["document_id"] == document_id), None)
        if record is None or not record["original_available"]:
            raise ValueError("Saved source not found; upload the document again.")
        metadata = DocumentMetadata.from_dict(record["metadata"])
        is_inventory = any(item["metadata"]["document_id"] == document_id
                           for item in self.regulatory.library.list_documents())
        store = self.regulatory.library.sources if is_inventory else self.repository.document_store
        return store.download_version(metadata)

    def usage_report(
        self, document_id: str = "", model: str = "", operation: str = "",
        start_date: str = "", end_date: str = "",
    ) -> dict:
        events = [
            item for item in self.usage.events()
            if (not document_id or document_id in item.get("document_ids", []))
            and (not model or item["model"] == model)
            and (not operation or item["operation"] == operation)
            and (not start_date or item["started_at"][:10] >= start_date)
            and (not end_date or item["started_at"][:10] <= end_date)
        ]
        return {"summary": usage_summary(events), "events": events}
