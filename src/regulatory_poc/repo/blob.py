from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from urllib.parse import quote, unquote, urlsplit
from datetime import datetime, timezone

from regulatory_poc.types.models import ChatMessage, DocumentMetadata
from regulatory_poc.repo.regulatory_store import update_document_record


class BlobJsonStore:
    def __init__(self, account_url: str, container: str) -> None:
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient

        self.client = BlobServiceClient(
            account_url, credential=DefaultAzureCredential()
        ).get_container_client(container)

    def get(self, key: str) -> dict | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return json.loads(self.client.download_blob(key).readall())
        except ResourceNotFoundError:
            return None

    def put(self, key: str, value: dict) -> None:
        from azure.storage.blob import ContentSettings

        self.client.upload_blob(
            key, json.dumps(value, ensure_ascii=False).encode(), overwrite=True,
            content_settings=ContentSettings(content_type="application/json"),
        )

    def list(self, prefix: str) -> list[dict]:
        return [
            value for item in self.client.list_blobs(name_starts_with=prefix)
            if (value := self.get(item.name)) is not None
        ]


class BlobDocumentStore:
    def __init__(self, account_url: str, container: str, state: BlobJsonStore) -> None:
        self._source = BlobJsonStore(account_url, container)
        self.state = state

    def save(self, content: bytes, metadata: DocumentMetadata) -> DocumentMetadata:
        from azure.storage.blob import ContentSettings

        digest = hashlib.sha256(content).hexdigest()
        name = f"{metadata.document_id}/{digest}/{quote(metadata.source_name, safe='')}"
        client = self._source.client.get_blob_client(name)
        client.upload_blob(
            content, overwrite=True, metadata={"sha256": digest, "status": "sample-unapproved"},
            content_settings=ContentSettings(content_type="application/octet-stream"),
        )
        metadata = replace(metadata, source_uri=client.url, source_version=digest)
        self._save_manifest(metadata, len(content), name)
        return metadata

    def _save_manifest(self, metadata: DocumentMetadata, size: int, name: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        prior = self.state.get(f"documents/{metadata.document_id}.json") or {}
        self.state.put(f"documents/{metadata.document_id}.json", {
            "metadata": metadata.to_dict(), "blob_name": name, "indexing_status": "pending",
            "approval_status": "sample-unapproved",
            "size_bytes": size, "uploaded_at": prior.get("uploaded_at", now),
            "updated_at": now,
        })

    def _validate_source_uri(self, metadata: DocumentMetadata) -> None:
        source, configured = urlsplit(metadata.source_uri), urlsplit(self._source.client.url)
        if (source.scheme != configured.scheme or source.netloc.casefold() != configured.netloc.casefold()
                or source.query or source.fragment or len(source.path.strip("/").split("/", 1)) != 2):
            raise ValueError("Saved Blob source must belong to the configured storage account.")

    def register_existing(self, content: bytes, metadata: DocumentMetadata) -> DocumentMetadata:
        self._validate_source_uri(metadata)
        if hashlib.sha256(content).hexdigest() != metadata.source_version:
            raise ValueError("Stored source hash mismatch; restore the verified source.")
        self._save_manifest(metadata, len(content), unquote(urlsplit(metadata.source_uri).path).split("/", 2)[-1])
        return metadata

    def indexed(self, document_id: str) -> None:
        self.mark(document_id, "indexed")

    def mark(self, document_id: str, status: str, **values) -> None:
        update_document_record(self.state, document_id, status, **values)

    def list_documents(self) -> list[DocumentMetadata]:
        return [
            DocumentMetadata.from_dict(value["metadata"])
            for value in self.state.list("documents/")
            if value["indexing_status"] == "indexed"
        ]

    def download(self, document_id: str) -> tuple[bytes, DocumentMetadata]:
        record = self.state.get(f"documents/{document_id}.json")
        if not record:
            raise ValueError("Document not found; verify its identifier.")
        metadata = DocumentMetadata.from_dict(record["metadata"])
        return self.download_version(metadata)

    def download_version(self, metadata: DocumentMetadata) -> tuple[bytes, DocumentMetadata]:
        if metadata.source_kind == "blob" and metadata.source_uri:
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import BlobClient

            self._validate_source_uri(metadata)
            client = BlobClient.from_blob_url(metadata.source_uri, credential=DefaultAzureCredential())
            content = client.download_blob().readall()
        else:
            name = (
                f"{metadata.document_id}/{metadata.source_version}/"
                f"{quote(metadata.source_name, safe='')}"
            )
            content = self._source.client.download_blob(name).readall()
        if hashlib.sha256(content).hexdigest() != metadata.source_version:
            raise ValueError("Stored source hash mismatch; restore the verified source.")
        return content, metadata


class BlobConversationRepository:
    def __init__(self, store: BlobJsonStore, prefix: str = "conversations") -> None:
        self._store, self._prefix = store, prefix

    def _key(self, conversation_id: str) -> str:
        digest = hashlib.sha256(conversation_id.encode()).hexdigest()
        return f"{self._prefix}/{digest}.json"

    def get(self, conversation_id: str) -> tuple[ChatMessage, ...]:
        value = self._store.get(self._key(conversation_id)) or {"messages": []}
        return tuple(ChatMessage(**item) for item in value["messages"])

    def append(self, conversation_id: str, *messages: ChatMessage) -> None:
        # One Blob per conversation avoids cross-conversation lost updates.
        self._store.put(self._key(conversation_id), {
            "messages": [asdict(item) for item in (*self.get(conversation_id), *messages)]
        })
