from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import quote

from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from regulatory_poc.repo.blob import BlobDocumentStore
from regulatory_poc.repo.blob_browser import BlobStorageBrowser, SOURCE_IMPORT_BYTES
from regulatory_poc.repo.documents import make_document_id
from regulatory_poc.repo.regulatory_store import LocalRecordStore
from regulatory_poc.repo.search import JsonSearchRepository
from regulatory_poc.service.regulatory_comparison import RegulatoryTableService
from regulatory_poc.service.regulatory_library import RegulatoryLibrary
from regulatory_poc.types.models import DocumentMetadata
from regulatory_poc.types.requirements import Requirement, RequirementInventory


CONTENT = b"%PDF- fake stored fixture"
ACCOUNT = "https://example.blob.core.windows.net"
URI = ACCOUNT + "/approved-documents/nested/saved.pdf"
DIGEST = hashlib.sha256(CONTENT).hexdigest()


class StoredPdfBrowser(BlobStorageBrowser):
    def __init__(self):
        self.client = SimpleNamespace(url=ACCOUNT)

    def containers(self):
        return [{"name": "approved-documents"}, {"name": "future-container"}]

    def entries(self, container, prefix="", continuation_token=None):
        entries = [
            {"name": "nested/", "kind": "folder", "size_bytes": None, "last_modified": None}
        ] if not prefix else [
            {"name": prefix + "saved.pdf", "kind": "file", "size_bytes": len(CONTENT),
             "last_modified": None},
            {"name": prefix + "report.json", "kind": "file", "size_bytes": 2, "last_modified": None},
        ]
        return {"entries": entries, "continuation_token": None}

    def read_pdf(self, container, name):
        if not name.lower().endswith(".pdf"):
            raise ValueError("Comparison sources must be text-based PDF documents.")
        return CONTENT, f"{ACCOUNT}/{container}/{quote(name, safe='/')}"


def make_library(root):
    state = LocalRecordStore(Path(root))
    sources = BlobDocumentStore.__new__(BlobDocumentStore)
    sources.state = state
    sources._source = SimpleNamespace(client=MagicMock(url=ACCOUNT + "/approved-documents"))
    return RegulatoryLibrary(state, sources, JsonSearchRepository(Path(root) / "index.json"))


def seed_upload(library):
    metadata = DocumentMetadata(
        "existing-document", "Existing stored source", "Sample", "English",
        source_name="saved.pdf", source_uri=URI, source_version=DIGEST,
    )
    library.state.put(f"documents/{metadata.document_id}.json", {
        "metadata": metadata.to_dict(), "indexing_status": "indexed",
        "uploaded_at": "2026-01-01T00:00:00+00:00",
    })
    return metadata


def extracted_inventory(content, metadata, *, kind):
    item = Requirement(
        key=metadata.document_id + "-1", identifier="1", title="Safety",
        description="The operator shall ensure safety.", document_id=metadata.document_id,
        source_name=metadata.source_name, source_hash=metadata.source_version,
        page=1, end_page=1, language=metadata.language,
    )
    return RequirementInventory(metadata, (item,), (), True)


def render_stored_sources(root):
    from types import SimpleNamespace
    from test_existing_blob_sources import make_library
    from regulatory_poc.ui.regulatory_sources import comparison_sources

    comparison_sources(SimpleNamespace(library=make_library(root)), SimpleNamespace())


class ExistingBlobSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.library = make_library(self.temp.name)
        self.browser = StoredPdfBrowser()
        extraction = patch(
            "regulatory_poc.service.regulatory_library.extract_requirements",
            side_effect=extracted_inventory,
        )
        self.extract = extraction.start()
        self.addCleanup(extraction.stop)

    def import_national(self, container="approved-documents", name="nested/saved.pdf", **kwargs):
        return self.library.import_blob_document(
            self.browser, container, name, "National regulation", "English",
            jurisdiction="Example authority", **kwargs,
        )

    def test_existing_upload_keeps_identity_and_original_without_blob_upload(self):
        original = seed_upload(self.library)
        with patch.object(self.library.sources, "save") as save:
            record = self.import_national()
        save.assert_not_called()
        self.library.sources._source.client.upload_blob.assert_not_called()
        self.assertEqual(original.document_id, record["metadata"]["document_id"])
        self.assertEqual(URI, record["metadata"]["source_uri"])
        self.assertEqual(DIGEST, record["requirements"][0]["source_hash"])
        self.assertEqual("indexed", record["indexing_status"])
        manifest = self.library.state.get("documents/existing-document.json")
        self.assertEqual("2026-01-01T00:00:00+00:00", manifest["uploaded_at"])
        self.assertEqual("indexed", manifest["indexing_status"])
        self.assertEqual("blob", manifest["metadata"]["source_kind"])
        self.assertEqual(1, len(self.library.search.list_chunks(original.document_id)))

    def test_new_container_source_can_be_downloaded_and_hash_checked_at_original_uri(self):
        record = self.import_national("future-container", "nested/original name.pdf")
        metadata = DocumentMetadata.from_dict(record["metadata"])
        expected = ACCOUNT + "/future-container/nested/original%20name.pdf"
        self.assertEqual(expected, metadata.source_uri)
        self.assertEqual(make_document_id("original name.pdf", CONTENT), metadata.document_id)
        with patch("azure.storage.blob.BlobClient.from_blob_url") as create:
            client = create.return_value
            client.download_blob.return_value.readall.return_value = CONTENT
            self.assertEqual((CONTENT, metadata), self.library.sources.download_version(metadata))
            self.assertEqual(expected, create.call_args.args[0])
            client.download_blob.return_value.readall.return_value = b"changed source"
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                self.library.sources.download_version(metadata)
            client.upload_blob.assert_not_called()
        self.assertEqual(("future-container", "nested/original name.pdf"),
                         self.browser.source_location(metadata.source_uri))

    def test_source_registration_rejects_other_accounts_and_credential_urls(self):
        metadata = seed_upload(self.library)
        for uri in [
            URI.replace("example.blob", "other.blob"), URI + "?sig=secret", URI + "#fragment",
        ]:
            with self.subTest(uri=uri), self.assertRaisesRegex(ValueError, "configured storage account"):
                self.library.sources.register_existing(CONTENT, replace(metadata, source_uri=uri))
            self.assertIsNone(self.browser.source_location(uri))
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.library.sources.register_existing(b"other bytes", metadata)

    def test_extraction_failure_can_retry_original_blob_without_copying(self):
        original = seed_upload(self.library)
        self.extract.side_effect = ValueError("Extraction failed")
        with self.assertRaisesRegex(ValueError, "Extraction failed"):
            self.import_national()
        record = self.library.get(original.document_id)
        self.assertEqual("failed", record["indexing_status"])
        self.extract.side_effect = extracted_inventory
        metadata = DocumentMetadata.from_dict(record["metadata"])
        with patch.object(self.library.sources, "download_version", return_value=(CONTENT, metadata)):
            with patch.object(self.library.sources, "save") as save:
                retried = self.library.retry_processing(original.document_id)
        save.assert_not_called()
        self.assertEqual("indexed", retried["indexing_status"])
        self.assertEqual(URI, retried["metadata"]["source_uri"])

    def test_repeat_import_is_idempotent_and_conflicting_role_is_not_overwritten(self):
        record = self.import_national()
        with patch.object(self.library.search, "upsert") as upsert:
            self.assertEqual(record, self.import_national())
            upsert.assert_not_called()
        self.assertEqual(1, self.extract.call_count)
        with self.assertRaisesRegex(ValueError, "different metadata"):
            self.library.import_blob_document(
                self.browser, "approved-documents", "nested/saved.pdf",
                "Translation reference", "English", jurisdiction="Example authority",
            )

    def test_imported_inventory_is_usable_in_comparison_setup(self):
        national = self.import_national()
        baseline = self.library.import_blob_document(
            self.browser, "future-container", "nested/baseline.pdf",
            "Reference baseline", "English", "Reference standard", "Rev. 1",
        )
        service = RegulatoryTableService(
            self.library, self.library.state, None, {}, {}, self.library.search,
        )
        with patch.object(self.library, "supporting_paragraphs", return_value=({}, [])):
            report = service.create(
                baseline["metadata"]["document_id"], "Example authority",
                [national["metadata"]["document_id"]],
            )
        self.assertEqual(1, len(report["rows"]))
        self.assertEqual(URI, report["inputs"]["sources"][0]["source_uri"])
        self.assertEqual("pending", report["rows"][0]["status"])

    def test_api_imports_stored_pdf_and_rejects_invalid_selection(self):
        from regulatory_poc.ui.api import app

        with patch("regulatory_poc.ui.regulatory_api._service",
                   return_value=SimpleNamespace(library=self.library)):
            with patch("regulatory_poc.ui.regulatory_api.build_blob_browser", return_value=self.browser):
                client = TestClient(app)
                body = {
                    "container": "future-container", "name": "nested/saved.pdf",
                    "role": "National regulation", "language": "English", "jurisdiction": "Example authority",
                }
                response = client.post("/regulatory/documents/from-blob", json=body)
                self.assertEqual(201, response.status_code, response.text)
                self.assertEqual(1, len(response.json()["requirements"]))
                body["name"] = "nested/report.json"
                self.assertEqual(422, client.post("/regulatory/documents/from-blob", json=body).status_code)

    def test_visible_selector_imports_known_pdf_only_on_explicit_submit(self):
        seed_upload(self.library)
        with patch("regulatory_poc.ui.regulatory_sources.build_blob_browser", return_value=self.browser):
            app = AppTest.from_function(render_stored_sources, args=(self.temp.name,), default_timeout=30).run()
            self.assertFalse(app.exception)
            self.assertIn("Use an existing Blob document", [item.label for item in app.expander])
            self.assertIn("Existing stored source", app.selectbox(key="source-blob-document").options[0])
            self.extract.assert_not_called()
            app.selectbox(key="source-blob-role").select("National regulation")
            app.text_input(key="source-blob-language").set_value("English")
            app.text_input(key="source-blob-jurisdiction").set_value("Example authority")
            next(button for button in app.button if button.label.startswith("Use selected Blob")).click().run()
            self.assertFalse(app.exception)
            self.assertEqual(1, len(self.library.list_documents()))
            self.assertTrue(any("Stored PDF imported for comparison" in item.value for item in app.success))

    def test_selector_browses_new_containers_and_filters_non_pdf_files(self):
        with patch("regulatory_poc.ui.regulatory_sources.build_blob_browser", return_value=self.browser):
            app = AppTest.from_function(render_stored_sources, args=(self.temp.name,), default_timeout=30).run()
            self.assertFalse(app.exception)
            app.selectbox(key="source-blob-container").select("future-container").run()
            app.button(key="source-blob-folder-future-container-nested/").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(["saved.pdf"],
                             app.selectbox(key="source-blob-file-future-container-nested/").options)
            self.extract.assert_not_called()
            next(button for button in app.button if button.label.startswith("Use selected Blob")).click().run()
            self.assertTrue(any("Supply the source language" in item.value for item in app.error))
            self.extract.assert_not_called()

    def test_pdf_read_is_size_bounded_and_etag_pinned(self):
        browser = BlobStorageBrowser.__new__(BlobStorageBrowser)
        browser.client = MagicMock()
        client = browser.client.get_blob_client.return_value
        client.url = URI
        client.get_blob_properties.return_value = SimpleNamespace(size=len(CONTENT), etag="pinned")
        client.download_blob.return_value.readall.return_value = CONTENT
        self.assertEqual((CONTENT, URI), browser.read_pdf("approved-documents", "nested/saved.pdf"))
        self.assertEqual("pinned", client.download_blob.call_args.kwargs["etag"])
        client.get_blob_properties.return_value.size = SOURCE_IMPORT_BYTES + 1
        with self.assertRaisesRegex(ValueError, "200 MiB"):
            browser.read_pdf("approved-documents", "nested/large.pdf")
        with self.assertRaisesRegex(ValueError, "PDF"):
            browser.read_pdf("poc-state", "state.json")
        client.upload_blob.assert_not_called()


if __name__ == "__main__":
    unittest.main()
