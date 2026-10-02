from __future__ import annotations

import hashlib
import json
from pathlib import Path

from regulatory_poc.repo.documents import chunk_document, extract_text
from regulatory_poc.repo.search import SearchRepository
from regulatory_poc.repo.sources import SourceConnector
from regulatory_poc.service.uploads import ingest_bytes
from regulatory_poc.types.models import DocumentMetadata, SyncResult


class SourceSyncService:
    def __init__(self, repository: SearchRepository, manifest_path: Path, manifest_store=None) -> None:
        self._repository = repository
        self._manifest_path = manifest_path
        self._manifest_store = manifest_store

    def sync(self, connector: SourceConnector) -> SyncResult:
        manifest = self._read_manifest()
        documents = connector.list_documents()
        indexed = 0
        skipped = 0
        failed: list[str] = []
        active_keys: set[str] = set()
        for source in documents:
            key = f"{connector.name}:{source.source_id}"
            active_keys.add(key)
            prior = manifest.get(key, {})
            if prior.get("version") == source.version:
                skipped += 1
                continue
            document_id = _source_document_id(connector.name, source.source_id)
            metadata = DocumentMetadata(
                document_id=document_id,
                title=source.metadata.get("title", source.source_name),
                revision=source.metadata.get("revision", ""),
                authority=source.metadata.get("authority", "approved repository"),
                language=source.metadata.get("language", "unknown"),
                country=source.metadata.get("country", ""),
                reactor_type=source.metadata.get("reactor_type", ""),
                standard=source.metadata.get("standard", ""),
                project_number=source.metadata.get("project_number", ""),
                source_name=source.source_name,
                source_kind=source.source_kind,
                source_uri=source.source_uri,
                source_version=source.version,
            )
            try:
                ingest_bytes(self._repository, source.content, metadata)
            except (RuntimeError, ValueError) as exc:
                failed.append(f"{source.source_name}: {exc}")
                continue
            manifest[key] = {"version": source.version, "document_id": document_id}
            indexed += 1

        stale = [
            key
            for key in manifest
            if key.startswith(f"{connector.name}:") and key not in active_keys
        ]
        for key in stale:
            self._repository.delete_document(str(manifest[key]["document_id"]))
            del manifest[key]
        self._write_manifest(manifest)
        return SyncResult(
            source=connector.name,
            discovered=len(documents),
            indexed=indexed,
            skipped=skipped,
            failed=tuple(failed),
        )

    def status(self) -> dict[str, dict[str, str]]:
        return self._read_manifest()

    def _read_manifest(self) -> dict[str, dict[str, str]]:
        if self._manifest_store:
            return self._manifest_store.get("sync/manifest.json") or {}
        if not self._manifest_path.exists():
            return {}
        try:
            value = json.loads(self._manifest_path.read_text(encoding="utf-8"))
            return {
                str(key): {str(k): str(v) for k, v in item.items()}
                for key, item in value.items()
            }
        except (json.JSONDecodeError, AttributeError, TypeError) as exc:
            raise ValueError(f"Invalid sync manifest: {self._manifest_path}") from exc

    def _write_manifest(self, value: dict[str, dict[str, str]]) -> None:
        if self._manifest_store:
            self._manifest_store.put("sync/manifest.json", value)
            return
        self._manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self._manifest_path.write_text(
            json.dumps(value, indent=2, sort_keys=True), encoding="utf-8"
        )


def _source_document_id(connector_name: str, source_id: str) -> str:
    digest = hashlib.sha256(f"{connector_name}:{source_id}".encode()).hexdigest()[:20]
    source_kind = connector_name.split(":", 1)[0]
    return f"{source_kind}-{digest}"
