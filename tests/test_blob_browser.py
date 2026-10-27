from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from regulatory_poc.repo.blob_browser import BlobStorageBrowser, PREVIEW_BYTES, UI_DOWNLOAD_BYTES
from regulatory_poc.repo.search import AzureAISearchRepository
from regulatory_poc.service.uploads import ingest_bytes
from regulatory_poc.types.models import DocumentMetadata
from test_document_admin import make_admin, render_admin
from test_regulatory_ui import example_report


NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)


class FakeBrowser:
    client = SimpleNamespace(url="https://example.blob.core.windows.net")

    def containers(self):
        return [{"name": "approved-documents"}, {"name": "extra-container"}]

    def entries(self, container, prefix="", continuation_token=None):
        if not prefix and not continuation_token:
            return {"entries": [{"name": "nested/", "kind": "folder", "size_bytes": None,
                                 "last_modified": None}], "continuation_token": "next"}
        return {"entries": [{"name": prefix + "saved.json", "kind": "file",
                             "size_bytes": 15, "last_modified": NOW.isoformat()}],
                "continuation_token": None}

    def preview(self, container, name):
        return {"container": container, "name": name, "text": '{"saved": true}',
                "preview_error": "", "truncated": False}

    def matches_source(self, source_uri, container, name):
        return False


def render_browser(root):
    from test_blob_browser import FakeBrowser
    from test_document_admin import make_admin
    from regulatory_poc.ui.document_admin import document_admin

    admin = make_admin(root)
    admin.blob_browser = FakeBrowser()
    document_admin(admin)


def render_picker():
    import streamlit as st
    from test_blob_browser import FakeBrowser
    from regulatory_poc.ui.document_admin import blob_file_picker

    if st.checkbox("Show browser", value=True, key="show-picker"):
        blob_file_picker(FakeBrowser())


def seed_processed(admin):
    report = example_report()
    report["created_at"] = NOW.isoformat()
    metadata = DocumentMetadata(
        "national", "National regulation", "Test", "Spanish", source_name="national.pdf",
    )
    ingest_bytes(admin.repository, b"Document safety requirements.", replace(metadata, source_name="national.txt"))
    admin.usage.store.put("regulatory-documents/national.json", {
        "metadata": metadata.to_dict(), "role": "National regulation",
        "requirements": report["inventories"]["national"], "warnings": [],
        "complete": False, "indexing_status": "indexed",
    })
    admin.regulatory.reports.put(f"regulatory-reports/{report['report_id']}.json", report)
    return report


class BlobBrowserTests(unittest.TestCase):
    def setUp(self):
        self.browser = BlobStorageBrowser.__new__(BlobStorageBrowser)
        self.browser.client = MagicMock()
        self.browser.client.url = "https://example.blob.core.windows.net"

    def test_lists_every_container_without_application_name_filter(self):
        self.browser.client.list_containers.return_value = [
            SimpleNamespace(name=name, last_modified=NOW)
            for name in ("approved-documents", "comparison-reports", "poc-state", "new-container")
        ]
        self.assertEqual(4, len(self.browser.containers()))
        self.browser.client.list_containers.assert_called_once_with()

    def test_folder_listing_preserves_sdk_continuation_and_prefix(self):
        from azure.storage.blob import BlobPrefix

        pages = MagicMock()
        pages.__next__.return_value = iter([
            BlobPrefix(prefix="deep/folder/"),
            SimpleNamespace(name="deep/raw.json", size=50, last_modified=NOW),
        ])
        pages.continuation_token = "next-page"
        client = self.browser.client.get_container_client.return_value
        client.walk_blobs.return_value.by_page.return_value = pages
        result = self.browser.entries("new-container", "deep/", "page-token")
        self.assertEqual(["folder", "file"], [item["kind"] for item in result["entries"]])
        self.assertEqual("deep/folder/", result["entries"][0]["name"])
        self.assertEqual("next-page", result["continuation_token"])
        client.walk_blobs.assert_called_once_with(name_starts_with="deep/", delimiter="/", results_per_page=100)
        client.walk_blobs.return_value.by_page.assert_called_once_with(continuation_token="page-token")

    def properties(self, size, content_type="application/json"):
        blob = self.browser.client.get_blob_client.return_value
        blob.get_blob_properties.return_value = SimpleNamespace(
            size=size, content_settings=SimpleNamespace(content_type=content_type),
            last_modified=NOW, metadata={"kind": "saved"}, etag="version",
        )
        return blob

    def test_preview_is_bounded_and_etag_pinned(self):
        blob = self.properties(PREVIEW_BYTES + 50)
        blob.download_blob.return_value.readall.return_value = b'{"value": "saved"}'
        result = self.browser.preview("poc-state", "nested/state.json")
        self.assertTrue(result["truncated"])
        self.assertEqual('{"value": "saved"}', result["text"])
        self.assertEqual(PREVIEW_BYTES, blob.download_blob.call_args.kwargs["length"])
        self.assertEqual("version", blob.download_blob.call_args.kwargs["etag"])

    def test_binary_and_non_utf8_previews_are_explicit(self):
        blob = self.properties(5, "application/pdf")
        blob.download_blob.return_value.readall.return_value = b"%PDF-"
        self.assertIsNone(self.browser.preview("raw", "file.pdf")["text"])
        blob = self.properties(2)
        blob.download_blob.return_value.readall.return_value = b"\xff\xfe"
        self.assertIn("not UTF-8", self.browser.preview("raw", "file.txt")["preview_error"])

    def test_ui_size_limit_does_not_limit_streaming_api(self):
        blob = self.properties(UI_DOWNLOAD_BYTES + 1)
        with self.assertRaisesRegex(ValueError, "streaming"):
            self.browser.download("raw", "large.pdf", ui=True)
        self.assertEqual(blob.download_blob.return_value, self.browser.download("raw", "large.pdf"))
        blob.upload_blob.assert_not_called()
        blob.delete_blob.assert_not_called()

    def test_source_association_requires_matching_account_and_exact_blob_path(self):
        self.assertTrue(self.browser.matches_source(
            "https://example.blob.core.windows.net/raw/folder/file%20name.txt", "raw", "folder/file name.txt",
        ))
        self.assertFalse(self.browser.matches_source(
            "https://other.blob.core.windows.net/raw/folder/file.txt", "raw", "folder/file.txt",
        ))
        self.assertFalse(self.browser.matches_source(
            "https://example.blob.core.windows.net/raw/other.txt", "raw", "folder/file.txt",
        ))

    def test_storage_api_lists_previews_and_streams_without_model_calls(self):
        from regulatory_poc.ui.api import app

        self.properties(3)
        self.browser.client.list_containers.return_value = [SimpleNamespace(name="extra", last_modified=NOW)]
        blob = self.browser.client.get_blob_client.return_value
        blob.download_blob.return_value.chunks.return_value = iter([b"a", b"bc"])
        blob.download_blob.return_value.readall.return_value = b"abc"
        with patch("regulatory_poc.ui.api._blob_browser", return_value=self.browser):
            client = TestClient(app)
            self.assertEqual("extra", client.get("/admin/storage/containers").json()[0]["name"])
            preview = client.get("/admin/storage/extra/preview", params={"name": "nested/file.txt"})
            self.assertEqual("abc", preview.json()["text"])
            response = client.get("/admin/storage/extra/content", params={"name": "nested/file.txt"})
            self.assertEqual(b"abc", response.content)
            self.assertIn("filename*=UTF-8''file.txt", response.headers["content-disposition"])

    def test_browser_ui_supports_new_container_folders_pagination_and_preview(self):
        with tempfile.TemporaryDirectory() as root:
            app = AppTest.from_function(render_browser, args=(root,), default_timeout=30).run()
            self.assertFalse(app.exception)
            self.assertIn("extra-container", app.selectbox(key="blob-container").options)
            app.selectbox(key="blob-container").select("extra-container").run()
            app.button(key="blob-next").click().run()
            self.assertFalse(app.exception)
            app.checkbox(key="blob-inspect").check().run()
            self.assertTrue(any(json.loads(item.value) == {"saved": True} for item in app.json))
            app.button(key="blob-previous").click().run()
            app.button(key="blob-folder-extra-container-nested/").click().run()
            self.assertEqual("nested/", app.session_state["blob-prefix-extra-container"])
            app.button(key="blob-root").click().run()
            self.assertFalse(app.exception)
            self.assertEqual("", app.session_state["blob-prefix-extra-container"])

    def test_one_click_folders_and_readable_file_names_replace_select_open_steps(self):
        app = AppTest.from_function(render_picker, default_timeout=30).run()
        self.assertFalse(any(item.label == "Folder on this page" for item in app.selectbox))
        self.assertFalse(any(item.label == "Open folder" for item in app.button))
        app.button(key="blob-folder-approved-documents-nested/").click().run()
        self.assertFalse(app.exception)
        self.assertEqual("nested/", app.session_state["blob-prefix-approved-documents"])
        self.assertEqual(["saved.json"], app.selectbox(key="blob-file-approved-documents-nested/").options)
        self.assertEqual("nested/saved.json", app.selectbox(key="blob-file-approved-documents-nested/").value)
        app.button(key="blob-up").click().run()
        self.assertEqual("", app.session_state["blob-prefix-approved-documents"])

    def test_jump_url_switches_container_and_breadcrumb_returns_to_ancestor(self):
        app = AppTest.from_function(render_picker, default_timeout=30).run()
        app.text_input(key="blob-jump").set_value(
            "https://example.blob.core.windows.net/extra-container/nested/deep%20folder/"
        )
        next(item for item in app.button if item.label == "Go to folder").click().run()
        self.assertFalse(app.exception)
        self.assertEqual("extra-container", app.selectbox(key="blob-container").value)
        self.assertEqual("nested/deep folder/", app.session_state["blob-prefix-extra-container"])
        app.button(key="blob-crumb-extra-container-0").click().run()
        self.assertEqual("nested/", app.session_state["blob-prefix-extra-container"])
        app.button(key="blob-root").click().run()
        self.assertEqual("", app.session_state["blob-prefix-extra-container"])

    def test_folder_and_container_are_remembered_when_picker_is_hidden(self):
        app = AppTest.from_function(render_picker, default_timeout=30).run()
        app.selectbox(key="blob-container").select("extra-container").run()
        app.button(key="blob-folder-extra-container-nested/").click().run()
        app.checkbox(key="show-picker").uncheck().run()
        app.checkbox(key="show-picker").check().run()
        self.assertFalse(app.exception)
        self.assertEqual("extra-container", app.selectbox(key="blob-container").value)
        self.assertEqual("nested/", app.session_state["blob-prefix-extra-container"])
        app.selectbox(key="blob-container").select("approved-documents").run()
        app.selectbox(key="blob-container").select("extra-container").run()
        self.assertEqual("nested/", app.session_state["blob-prefix-extra-container"])

    def test_jump_accepts_paths_and_container_root_and_resets_paging(self):
        app = AppTest.from_function(render_picker, default_timeout=30).run()
        app.button(key="blob-next").click().run()
        for location, container, prefix in [
            ("extra-container/nested/deep", "extra-container", "nested/deep/"),
            ("relative/folder", "extra-container", "relative/folder/"),
            ("https://example.blob.core.windows.net/approved-documents", "approved-documents", ""),
        ]:
            with self.subTest(location=location):
                app.text_input(key="blob-jump").set_value(location)
                next(item for item in app.button if item.label == "Go to folder").click().run()
                self.assertFalse(app.exception)
                self.assertEqual(container, app.selectbox(key="blob-container").value)
                self.assertEqual(prefix, app.session_state[f"blob-prefix-{container}"])
                self.assertEqual([None], app.session_state[f"blob-pages-{container}-{prefix}"])

    def test_invalid_jump_reports_error_without_changing_location(self):
        app = AppTest.from_function(render_picker, default_timeout=30).run()
        app.button(key="blob-folder-approved-documents-nested/").click().run()
        for location in [
            "https://other.blob.core.windows.net/extra-container/folder/",
            "https://example.blob.core.windows.net/extra-container/folder/?sig=secret",
            "https://example.blob.core.windows.net/missing-container/folder/",
            "https://[invalid",
        ]:
            with self.subTest(location=location):
                app.text_input(key="blob-jump").set_value(location)
                next(item for item in app.button if item.label == "Go to folder").click().run()
                self.assertFalse(app.exception)
                self.assertTrue(app.error)
                self.assertEqual("nested/", app.session_state["blob-prefix-approved-documents"])
                self.assertEqual("approved-documents", app.selectbox(key="blob-container").value)
        app.text_input(key="blob-jump").set_value("approved-documents")
        next(item for item in app.button if item.label == "Go to folder").click().run()
        self.assertFalse(app.error)


class ProcessedInformationTests(unittest.TestCase):
    def test_processed_inventories_chunks_and_comparison_outputs_are_read_only(self):
        with tempfile.TemporaryDirectory() as root:
            admin = make_admin(root)
            report = seed_processed(admin)
            with patch.object(admin.repository, "search", side_effect=AssertionError("Must not retrieve/model")):
                details = admin.processed("national")
            self.assertEqual("Exact source line one.\nExact source line two | [not a link].",
                             details["requirements"][0]["description"])
            self.assertEqual("Document safety requirements.", details["chunks"][0]["text"])
            self.assertEqual(report["report_id"], details["comparisons"][0]["report_id"])
            self.assertEqual("Proposed relationship requires expert review.",
                             details["comparisons"][0]["rows"][0]["matches"][0]["analysis"])
            self.assertFalse(admin.usage.events())
            with self.assertRaisesRegex(ValueError, "not found"):
                admin.processed("missing")

    def test_processed_api_returns_saved_results_and_not_found(self):
        from regulatory_poc.ui.api import app

        with tempfile.TemporaryDirectory() as root:
            admin = make_admin(root)
            seed_processed(admin)
            with patch("regulatory_poc.ui.api._document_admin", return_value=admin):
                client = TestClient(app)
                response = client.get("/admin/documents/national/processed")
                self.assertEqual(200, response.status_code)
                self.assertEqual(1, len(response.json()["requirements"]))
                self.assertEqual(1, len(response.json()["comparisons"]))
                self.assertEqual(404, client.get("/admin/documents/missing/processed").status_code)

    def test_processed_ui_shows_literal_text_and_generated_analysis_separately(self):
        with tempfile.TemporaryDirectory() as root:
            admin = make_admin(root)
            seed_processed(admin)
            app = AppTest.from_function(render_admin, args=(root,), default_timeout=30).run()
            app.checkbox(key="admin-show-processed").check().run()
            self.assertFalse(app.exception)
            labels = [tab.label for tab in app.tabs]
            self.assertIn("Extracted requirements", labels)
            self.assertIn("Indexed passages", labels)
            self.assertIn("Saved comparison results", labels)
            self.assertTrue(any("Exact source line one.\nExact source line two" in item.value for item in app.code))
            self.assertTrue(any("Document safety requirements." == item.value for item in app.code))
            self.assertTrue(any("Clause-by-clause analysis (English)" in frame.value.columns
                                for frame in app.dataframe))
            self.assertFalse(admin.usage.events())

    def test_azure_chunk_listing_uses_ordinary_index_read_not_embeddings_or_iq(self):
        client, embedding = MagicMock(), MagicMock()
        metadata = DocumentMetadata("doc'one", "Title", "Test")
        client.search.return_value = [
            {"id": "second", "content": "Second saved chunk", "ordinal": 2, **metadata.to_dict()},
            {"id": "first", "content": "First saved chunk", "ordinal": 1, **metadata.to_dict()},
        ]
        with patch("azure.search.documents.SearchClient", return_value=client):
            repository = AzureAISearchRepository("https://search.test", "index", "semantic", embedding)
        chunks = repository.list_chunks("doc'one")
        self.assertEqual(["first", "second"], [chunk.chunk_id for chunk in chunks])
        self.assertEqual("document_id eq 'doc''one'", client.search.call_args.kwargs["filter"])
        self.assertNotIn("vector_queries", client.search.call_args.kwargs)
        embedding.embed.assert_not_called()
