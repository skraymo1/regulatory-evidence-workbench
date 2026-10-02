from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from regulatory_poc.repo.documents import chunk_document, extract_text, make_document_id
from regulatory_poc.repo.search import SearchRepository
from regulatory_poc.service.uploads import ingest_bytes
from regulatory_poc.types.models import DocumentMetadata


class SampleCatalogService:
    def __init__(self, repository: SearchRepository, catalog_root: Path) -> None:
        self._repository = repository
        self._catalog_root = catalog_root

    def seed(self) -> tuple[DocumentMetadata, ...]:
        manifest = self._load_manifest()
        authority = str(manifest.get("authority", "International Atomic Energy Agency"))
        indexed: list[DocumentMetadata] = []
        for item in manifest["documents"]:
            path = self._catalog_root / str(item["file"])
            content = path.read_bytes()
            actual_hash = hashlib.sha256(content).hexdigest()
            if actual_hash != item["sha256"]:
                raise ValueError(
                    f"Sample '{path.name}' failed SHA-256 verification. "
                    "Restore it from the official URL in manifest.json."
                )
            metadata = DocumentMetadata(
                document_id=make_document_id(path.name, content),
                title=str(item["title"]),
                authority=authority,
                language=str(item["language"]),
                standard=str(item["standard"]),
                revision=str(item.get("revision", "")),
                source_name=path.name,
                source_kind="sample",
                source_uri=str(item["publication_page"]),
                source_version=str(item["sha256"]),
            )
            ingest_bytes(self._repository, content, metadata)
            indexed.append(metadata)
        return tuple(indexed)

    def _load_manifest(self) -> dict[str, Any]:
        path = self._catalog_root / "manifest.json"
        if not path.exists():
            raise ValueError(f"Sample manifest was not found: {path}")
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            documents = manifest["documents"]
            if not isinstance(documents, list) or not documents:
                raise ValueError("documents must be a non-empty list")
            return manifest
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Sample manifest is invalid: {path}") from exc
