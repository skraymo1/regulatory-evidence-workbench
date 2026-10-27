from __future__ import annotations

from urllib.parse import unquote, urlsplit


PREVIEW_BYTES = 256 * 1024
UI_DOWNLOAD_BYTES = 64 * 1024 * 1024
SOURCE_IMPORT_BYTES = 200 * 1024 * 1024


class BlobStorageBrowser:
    """Read-only browsing of the configured account, independent of workbench manifests."""

    def __init__(self, account_url: str) -> None:
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient

        self.client = BlobServiceClient(account_url, credential=DefaultAzureCredential())

    def containers(self) -> list[dict]:
        return [
            {"name": item.name, "last_modified": item.last_modified.isoformat()}
            for item in self.client.list_containers()
        ]

    def entries(
        self, container: str, prefix: str = "", continuation_token: str | None = None,
    ) -> dict:
        from azure.storage.blob import BlobPrefix

        pages = self.client.get_container_client(container).walk_blobs(
            name_starts_with=prefix, delimiter="/", results_per_page=100,
        ).by_page(continuation_token=continuation_token)
        page = next(pages, [])
        entries = []
        for item in page:
            is_folder = isinstance(item, BlobPrefix)
            entries.append({
                "name": item.name, "kind": "folder" if is_folder else "file",
                "size_bytes": None if is_folder else item.size,
                "last_modified": None if is_folder else item.last_modified.isoformat(),
            })
        return {"entries": entries, "continuation_token": pages.continuation_token}

    def _blob(self, container: str, name: str):
        return self.client.get_blob_client(container=container, blob=name)

    def preview(self, container: str, name: str) -> dict:
        from azure.core import MatchConditions

        client = self._blob(container, name)
        properties = client.get_blob_properties()
        length = min(properties.size, PREVIEW_BYTES)
        content = client.download_blob(
            offset=0, length=length, etag=properties.etag,
            match_condition=MatchConditions.IfNotModified,
        ).readall() if length else b""
        content_type = properties.content_settings.content_type or "application/octet-stream"
        is_text = content_type.startswith("text/") or content_type == "application/json" or name.lower().endswith(
            (".json", ".txt", ".md", ".csv", ".xml", ".yaml", ".yml", ".log"),
        )
        text, decode_error = None, ""
        if is_text:
            try:
                # Ignore only an incomplete trailing UTF-8 character in a bounded preview.
                import codecs

                text = codecs.getincrementaldecoder("utf-8-sig")().decode(
                    content, final=properties.size <= PREVIEW_BYTES,
                )
            except UnicodeDecodeError:
                decode_error = "This file is not UTF-8 text. Download it to inspect its original encoding."
        return {
            "container": container, "name": name, "size_bytes": properties.size,
            "last_modified": properties.last_modified.isoformat(), "content_type": content_type,
            "metadata": properties.metadata, "etag": properties.etag,
            "text": text, "preview_error": decode_error, "truncated": properties.size > PREVIEW_BYTES,
        }

    def download(self, container: str, name: str, *, ui: bool = False):
        from azure.core import MatchConditions

        client = self._blob(container, name)
        properties = client.get_blob_properties()
        if ui and properties.size > UI_DOWNLOAD_BYTES:
            raise ValueError("Files over 64 MiB must be downloaded through the streaming Blob admin API.")
        return client.download_blob(
            etag=properties.etag, match_condition=MatchConditions.IfNotModified,
        )

    def read_pdf(self, container: str, name: str) -> tuple[bytes, str]:
        from azure.core import MatchConditions

        if not name.lower().endswith(".pdf"):
            raise ValueError("Comparison sources must be text-based PDF documents.")
        client = self._blob(container, name)
        properties = client.get_blob_properties()
        if properties.size > SOURCE_IMPORT_BYTES:
            raise ValueError("Comparison source PDFs must be 200 MiB or smaller.")
        content = client.download_blob(
            etag=properties.etag, match_condition=MatchConditions.IfNotModified,
        ).readall()
        return content, client.url

    def source_location(self, source_uri: str) -> tuple[str, str] | None:
        source, account = urlsplit(source_uri), urlsplit(self.client.url)
        if source.scheme != account.scheme or source.netloc.casefold() != account.netloc.casefold():
            return None
        path = unquote(source.path)
        base = account.path.rstrip("/") + "/"
        if not path.startswith(base) or source.query or source.fragment:
            return None
        container, separator, name = path[len(base):].partition("/")
        return (container, name) if container and separator and name else None

    def matches_source(self, source_uri: str, container: str, name: str) -> bool:
        return self.source_location(source_uri) == (container, name)
