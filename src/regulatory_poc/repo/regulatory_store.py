from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from datetime import datetime, timezone
from typing import Protocol

from regulatory_poc.types.models import DocumentMetadata


class RecordStore(Protocol):
    def get(self, key: str) -> dict | None: ...
    def put(self, key: str, value: dict) -> None: ...
    def list(self, prefix: str) -> list[dict]: ...


class SourceStore(Protocol):
    def save(self, content: bytes, metadata: DocumentMetadata) -> DocumentMetadata: ...
    def download_version(self, metadata: DocumentMetadata) -> tuple[bytes, DocumentMetadata]: ...


def update_document_record(state: RecordStore, document_id: str, status: str, **values) -> None:
    key = f"documents/{document_id}.json"
    record = state.get(key)
    if record is None:
        raise ValueError("Source manifest missing; upload the source bytes first.")
    record.update(indexing_status=status, updated_at=datetime.now(timezone.utc).isoformat(), **values)
    state.put(key, record)


class LocalRecordStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Invalid record path; use a record identifier.")
        return path

    def get(self, key: str) -> dict | None:
        path = self._path(key)
        if not path.exists():
            return None
        value = json.loads(path.read_text("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Stored record is invalid; restore it from a verified backup.")
        return value

    def put(self, key: str, value: dict) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def list(self, prefix: str) -> list[dict]:
        return [
            value for path in sorted(self._path(prefix).glob("*.json"))
            if (value := self.get(str(path.relative_to(self.root)))) is not None
        ]


class LocalSourceStore:
    def __init__(self, root: Path, state: RecordStore | None = None) -> None:
        self.root, self.state = root, state

    def save(self, content: bytes, metadata: DocumentMetadata) -> DocumentMetadata:
        digest = hashlib.sha256(content).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / digest
        path.write_bytes(content)
        metadata = replace(metadata, source_version=digest, source_uri=path.resolve().as_uri())
        if self.state is not None:
            now = datetime.now(timezone.utc).isoformat()
            prior = self.state.get(f"documents/{metadata.document_id}.json") or {}
            self.state.put(f"documents/{metadata.document_id}.json", {
                "metadata": metadata.to_dict(), "indexing_status": "pending",
                "approval_status": "unapproved", "size_bytes": len(content),
                "uploaded_at": prior.get("uploaded_at", now), "updated_at": now,
            })
        return metadata

    def mark(self, document_id: str, status: str, **values) -> None:
        if self.state is not None:
            update_document_record(self.state, document_id, status, **values)

    def indexed(self, document_id: str) -> None:
        self.mark(document_id, "indexed")

    def download(self, document_id: str) -> tuple[bytes, DocumentMetadata]:
        record = self.state.get(f"documents/{document_id}.json") if self.state else None
        if record is None:
            raise ValueError("Document not found; verify its identifier.")
        return self.download_version(DocumentMetadata.from_dict(record["metadata"]))

    def download_version(self, metadata: DocumentMetadata) -> tuple[bytes, DocumentMetadata]:
        digest = metadata.source_version
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("Invalid source hash; import the source again.")
        content = (self.root / digest).read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("Source hash mismatch; restore the original source.")
        return content, metadata
