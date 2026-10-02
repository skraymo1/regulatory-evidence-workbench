from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Protocol

from regulatory_poc.types.models import SourceDocument


class SourceConnector(Protocol):
    @property
    def name(self) -> str: ...

    def list_documents(self) -> list[SourceDocument]: ...


class BlobSourceConnector:
    def __init__(self, account_url: str, container: str, prefix: str = "") -> None:
        try:
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import ContainerClient
        except ImportError as exc:
            raise RuntimeError(
                "Blob ingestion requires azure-identity and azure-storage-blob."
            ) from exc
        self._container = ContainerClient(
            account_url=account_url,
            container_name=container,
            credential=DefaultAzureCredential(),
        )
        self._prefix = prefix
        self._name = f"blob:{container}"

    @property
    def name(self) -> str:
        return self._name

    def list_documents(self) -> list[SourceDocument]:
        documents: list[SourceDocument] = []
        for blob in self._container.list_blobs(name_starts_with=self._prefix):
            if not _supported(blob.name):
                continue
            content = self._container.download_blob(blob.name).readall()
            metadata = {str(key): str(value) for key, value in (blob.metadata or {}).items()}
            documents.append(
                SourceDocument(
                    source_id=blob.name,
                    source_kind="blob",
                    source_uri=f"{self._container.url}/{urllib.parse.quote(blob.name)}",
                    source_name=blob.name.rsplit("/", 1)[-1],
                    version=str(blob.etag or blob.last_modified or ""),
                    content=content,
                    metadata=metadata,
                )
            )
        return documents


class SharePointSourceConnector:
    GRAPH_ROOT = "https://graph.microsoft.com/v1.0"

    def __init__(self, site_id: str, drive_id: str, folder_path: str = "") -> None:
        try:
            from azure.identity import DefaultAzureCredential
        except ImportError as exc:
            raise RuntimeError("SharePoint ingestion requires azure-identity.") from exc
        self._credential = DefaultAzureCredential()
        self._site_id = site_id
        self._drive_id = drive_id
        self._folder_path = folder_path.strip("/")

    @property
    def name(self) -> str:
        return f"sharepoint:{self._site_id}:{self._drive_id}"

    def list_documents(self) -> list[SourceDocument]:
        items = self._walk_folder(self._folder_path)
        documents: list[SourceDocument] = []
        for item in items:
            if "file" not in item or not _supported(str(item.get("name", ""))):
                continue
            item_id = str(item["id"])
            content = self._get_bytes(
                f"{self.GRAPH_ROOT}/drives/{self._drive_id}/items/{item_id}/content"
            )
            fields = item.get("listItem", {}).get("fields", {})
            documents.append(
                SourceDocument(
                    source_id=item_id,
                    source_kind="sharepoint",
                    source_uri=str(item.get("webUrl", "")),
                    source_name=str(item["name"]),
                    version=str(item.get("eTag") or item.get("lastModifiedDateTime") or ""),
                    content=content,
                    metadata={
                        "site_id": self._site_id,
                        **{
                            key: str(fields.get(key, ""))
                            for key in (
                                "title",
                                "authority",
                                "language",
                                "country",
                                "reactor_type",
                                "standard",
                                "project_number",
                            )
                            if fields.get(key)
                        },
                    },
                )
            )
        return documents

    def _walk_folder(self, folder_path: str) -> list[dict[str, object]]:
        if folder_path:
            encoded = urllib.parse.quote(folder_path)
            base = f"{self.GRAPH_ROOT}/drives/{self._drive_id}/root:/{encoded}:/children"
        else:
            base = f"{self.GRAPH_ROOT}/drives/{self._drive_id}/root/children"
        query = (
            "?$select=id,name,eTag,lastModifiedDateTime,webUrl,file,folder,parentReference"
            "&$expand=listItem($expand=fields)"
        )
        items = self._paged_get(base + query)
        files = [item for item in items if "file" in item]
        for folder in (item for item in items if "folder" in item):
            nested = "/".join(part for part in (folder_path, str(folder["name"])) if part)
            files.extend(self._walk_folder(nested))
        return files

    def _headers(self) -> dict[str, str]:
        token = self._credential.get_token("https://graph.microsoft.com/.default")
        return {"Authorization": f"Bearer {token.token}", "Accept": "application/json"}

    def _paged_get(self, url: str) -> list[dict[str, object]]:
        values: list[dict[str, object]] = []
        while url:
            request = urllib.request.Request(url, headers=self._headers())
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read())
            values.extend(payload.get("value", []))
            url = str(payload.get("@odata.nextLink", ""))
        return values

    def _get_bytes(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.read()


def _supported(name: str) -> bool:
    return name.casefold().endswith((".docx", ".md", ".pdf", ".txt"))
