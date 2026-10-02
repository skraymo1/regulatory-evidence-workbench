import asyncio
import base64
import json
import hashlib
import unittest
from dataclasses import replace
from unittest.mock import Mock

from regulatory_poc.config.settings import Settings
from regulatory_poc.repo.iq import FoundryIQRepository
from regulatory_poc.repo.blob import BlobDocumentStore
from regulatory_poc.service.authorization import authorize_principal
from regulatory_poc.service.reports import SavedComparisonService
from regulatory_poc.service.comparison import _citation_error, RegulatoryComparisonService
from regulatory_poc.service.uploads import ingest_bytes
from regulatory_poc.types.models import (
    ComparisonRequest, ComparisonResult, DocumentMetadata, Citation,
)


class MemoryStore:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def put(self, key, value):
        self.values[key] = json.loads(json.dumps(value))


class AzurePocTests(unittest.TestCase):
    def test_principal_restriction_fails_closed(self):
        def headers(oid, tid="tenant"):
            raw = {"auth_typ": "aad", "claims": [
                {"typ": "oid", "val": oid}, {"typ": "tid", "val": tid},
            ]}
            return {"x-ms-client-principal": base64.b64encode(json.dumps(raw).encode()).decode()}
        self.assertTrue(authorize_principal(headers("user"), "user", "tenant"))
        self.assertFalse(authorize_principal(headers("other"), "user", "tenant"))
        self.assertFalse(authorize_principal(headers("user", "other"), "user", "tenant"))
        self.assertFalse(authorize_principal({}, "user", "tenant"))
        self.assertFalse(authorize_principal({"x-ms-client-principal": "bad!"}, "user", "tenant"))
        self.assertFalse(authorize_principal(headers("user"), "", "tenant"))

    def test_aca_federated_principal_uses_verified_provider_header(self):
        raw = {"auth_typ": "federation", "claims": [
            {"typ": "http://schemas.microsoft.com/identity/claims/objectidentifier", "val": "user"},
            {"typ": "http://schemas.microsoft.com/identity/claims/tenantid", "val": "tenant"},
        ]}
        headers = {
            "X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(raw).encode()).decode(),
            "X-MS-CLIENT-PRINCIPAL-IDP": "aad",
        }
        self.assertTrue(authorize_principal(headers, "user", "tenant"))
        self.assertFalse(authorize_principal(headers, "other", "tenant"))
        self.assertFalse(authorize_principal(headers, "user", "other"))
        self.assertFalse(authorize_principal({
            **headers, "X-MS-CLIENT-PRINCIPAL-IDP": "github",
        }, "user", "tenant"))
        self.assertFalse(authorize_principal({
            "X-MS-CLIENT-PRINCIPAL-ID": "user", "X-MS-CLIENT-PRINCIPAL-IDP": "aad",
        }, "user", "tenant"))

    def test_upload_persists_before_index(self):
        events = []
        repo = Mock()
        metadata = DocumentMetadata("doc", "Title", "sample", source_name="sample.txt")
        repo.document_store.save.side_effect = lambda content, meta: (events.append("blob") or meta)
        repo.delete_document.side_effect = lambda doc: events.append("delete")
        repo.upsert.side_effect = lambda chunks: events.append("index")
        repo.document_store.indexed.side_effect = lambda doc: events.append("manifest")
        ingest_bytes(repo, b"Defence in depth shall be implemented.", metadata)
        self.assertEqual(events, ["blob", "delete", "index", "manifest"])

    def test_report_source_download_uses_pinned_hash(self):
        store = BlobDocumentStore.__new__(BlobDocumentStore)
        store._source = Mock()
        content = b"original edition"
        digest = hashlib.sha256(content).hexdigest()
        metadata = DocumentMetadata(
            "doc", "Original", "reference", source_name="edition.pdf", source_version=digest,
        )
        store._source.client.download_blob.return_value.readall.return_value = content
        self.assertEqual(store.download_version(metadata), (content, metadata))
        store._source.client.download_blob.assert_called_once_with(f"doc/{digest}/edition.pdf")
        store._source.client.download_blob.return_value.readall.return_value = b"changed edition"
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            store.download_version(metadata)

    def test_iq_reference_filters_are_enforced(self):
        from azure.search.documents.knowledgebases.models import (
            KnowledgeBaseRetrievalResponse, KnowledgeBaseSearchIndexReference,
        )
        repo = FoundryIQRepository.__new__(FoundryIQRepository)
        repo._knowledge_source = "source"
        repo._kb = Mock()
        data = {
            "id": "chunk", "ordinal": 1, "content": "Safety", "document_id": "other",
            "title": "Edition", "authority": "reference",
        }
        repo._kb.retrieve.return_value = KnowledgeBaseRetrievalResponse(references=[
            KnowledgeBaseSearchIndexReference(id="0", activity_source=0, source_data=data),
        ])
        with self.assertRaisesRegex(RuntimeError, "outside"):
            repo.search("Safety", 5, document_id="expected")
        request = repo._kb.retrieve.call_args.args[0]
        self.assertEqual(request.knowledge_source_params[0].filter_add_on, "document_id eq 'expected'")
        self.assertTrue(request.knowledge_source_params[0].include_reference_source_data)
        self.assertEqual(request.knowledge_source_params[0].max_output_documents, 50)

    def test_fidelity_conclusion_requires_both_editions(self):
        citations = (
            Citation("A1", "a", "English", "reference", 1, "shall", "a.pdf"),
            Citation("B1", "b", "French", "reference", 1, "doit", "b.pdf"),
        )
        self.assertIn("both language", _citation_error("Meaning is preserved [A1].", citations))
        self.assertEqual(_citation_error("Meaning is preserved [A1][B1].", citations), "")

    def test_cloud_comparison_rejects_missing_or_different_revisions(self):
        docs = [
            DocumentMetadata("a", "English", "reference", "English", standard="reference profile", revision="Rev. 1"),
            DocumentMetadata("b", "French", "reference", "French", standard="reference profile", revision="Rev. 2"),
        ]
        sources, agent = Mock(), Mock()
        sources.list_documents.side_effect = lambda: docs
        service = RegulatoryComparisonService(sources, agent, require_revision=True)
        request = ComparisonRequest("Compare", "a", "b")
        with self.assertRaisesRegex(ValueError, "same publication revision"):
            asyncio.run(service.compare(request))
        docs[:] = [replace(item, revision="") for item in docs]
        with self.assertRaisesRegex(ValueError, "same publication revision"):
            asyncio.run(service.compare(request))
        sources.search.assert_not_called()
        agent.compare.assert_not_called()

    def test_saved_report_failure_retry_and_cache_invalidation(self):
        docs = [
            DocumentMetadata("a", "English", "reference", "English", standard="reference profile Rev1", source_version="hash-a"),
            DocumentMetadata("b", "French", "reference", "French", standard="reference profile Rev1", source_version="hash-b"),
        ]
        sources = Mock()
        sources.list_documents.side_effect = lambda: docs
        comparison = Mock()
        calls = []
        async def compare(request):
            calls.append(request)
            return ComparisonResult("Meaning [A1][B1]", (
                Citation("A1", "a", "English", "reference", 1, "shall", "a.pdf"),
            ), True)
        comparison.compare = compare
        index = Mock()
        index.upsert.side_effect = RuntimeError("unavailable")
        settings = replace(Settings.from_env(), agent_version="1", model_version="2026-07-09")
        service = SavedComparisonService(comparison, sources, MemoryStore(), index, settings)
        request = ComparisonRequest("Compare meaning", "a", "b")
        first = asyncio.run(service.compare(request))
        self.assertEqual(first.indexing_status, "pending")
        self.assertEqual(service.get(first.report_id)["review_status"], "unreviewed")
        cached = asyncio.run(service.compare(request))
        self.assertTrue(cached.cached)
        self.assertEqual(len(calls), 1)
        index.upsert.side_effect = None
        self.assertEqual(service.retry_index(first.report_id).indexing_status, "indexed")
        chunks = index.upsert.call_args.args[0]
        self.assertTrue(all("original_sources" in chunk.text for chunk in chunks))
        self.assertEqual(len(calls), 1)
        docs[0] = replace(docs[0], source_version="changed")
        changed = asyncio.run(service.compare(request))
        self.assertNotEqual(first.report_id, changed.report_id)
        self.assertEqual(len(calls), 2)
        service.settings = replace(settings, agent_version="2")
        self.assertNotEqual(changed.report_id, asyncio.run(service.compare(request)).report_id)


if __name__ == "__main__":
    unittest.main()
