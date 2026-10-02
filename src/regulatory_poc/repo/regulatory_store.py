from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from regulatory_poc.types.models import DocumentMetadata


class RecordStore(Protocol):
    def get(self, key: str) -> dict | None: ...
    def put(self, key: str, value: dict) -> None: ...
    def list(self, prefix: str) -> list[dict]: ...


class SourceStore(Protocol):
    def save(self, content: bytes, metadata: DocumentMetadata) -> DocumentMetadata: ...
    def download_version(self, metadata: DocumentMetadata) -> tuple[bytes, DocumentMetadata]: ...


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
    def __init__(self, root: Path) -> None:
        self.root = root

    def save(self, content: bytes, metadata: DocumentMetadata) -> DocumentMetadata:
        digest = hashlib.sha256(content).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / digest
        path.write_bytes(content)
        return replace(metadata, source_version=digest, source_uri=path.resolve().as_uri())

    def download_version(self, metadata: DocumentMetadata) -> tuple[bytes, DocumentMetadata]:
        digest = metadata.source_version
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("Invalid source hash; import the source again.")
        content = (self.root / digest).read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("Source hash mismatch; restore the original source.")
        return content, metadata
