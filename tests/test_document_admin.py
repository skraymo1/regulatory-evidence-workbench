from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from regulatory_poc.config.settings import Settings
from regulatory_poc.repo.agents import FoundryComparisonAgent
from regulatory_poc.repo.embeddings import FoundryEmbeddingProvider
from regulatory_poc.repo.regulatory_store import LocalRecordStore
from regulatory_poc.runtime.container import build_search_repository, build_usage_tracker
from regulatory_poc.runtime.regulatory import build_regulatory_service
from regulatory_poc.service.document_admin import DocumentAdminService
from regulatory_poc.service.uploads import ingest_bytes
from regulatory_poc.service.usage import UsageTracker, parse_pricing, usage_scope, usage_summary
from regulatory_poc.types.models import DocumentMetadata
from regulatory_poc.types.requirements import Requirement, RequirementInventory


PRICES = '{"model":{"input":2,"output":8},"embedding":{"input":0.1,"output":0}}'


def make_admin(root):
    settings = replace(
        Settings.from_env(), index_path=Path(root) / "index.json",
        agent_mode="offline", search_mode="local", source_knowledge_base="", model_pricing=PRICES,
    )
    repository = build_search_repository(settings)
    return DocumentAdminService(
        build_regulatory_service(settings, repository), repository, build_usage_tracker(settings),
    )


def render_admin(root):
    from test_document_admin import make_admin
    from regulatory_poc.ui.document_admin import document_admin, model_usage

    admin = make_admin(root)
    document_admin(admin)
    model_usage(admin)


class DocumentAdminTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.admin = make_admin(self.temp.name)
        self.metadata = DocumentMetadata(
            document_id="example", title="Example document", authority="Test",
            language="English", source_name="example.txt",
        )

    def test_generic_upload_is_durable_and_downloadable(self):
        count = ingest_bytes(self.admin.repository, b"Document safety requirements.", self.metadata)
        documents = make_admin(self.temp.name).documents()
        self.assertEqual(1, len(documents))
        record = documents[0]
        self.assertEqual("indexed", record["indexing_status"])
        self.assertEqual(count, record["chunks_indexed"])
        self.assertTrue(record["uploaded_at"])
        self.assertTrue(record["processed_at"])
        self.assertEqual(b"Document safety requirements.", self.admin.original("example")[0])

    def test_generic_failure_is_visible_and_retryable(self):
        with patch.object(self.admin.repository, "upsert", side_effect=RuntimeError("Search unavailable")):
            with self.assertRaisesRegex(RuntimeError, "Search unavailable"):
                ingest_bytes(self.admin.repository, b"Safety requirements.", self.metadata)
        record = self.admin.documents()[0]
        self.assertEqual("failed", record["indexing_status"])
        self.assertIn("Search unavailable", record["indexing_error"])
        uploaded_at = record["uploaded_at"]
        self.admin.retry("example")
        record = self.admin.documents()[0]
        self.assertEqual("indexed", record["indexing_status"])
        self.assertEqual(uploaded_at, record["uploaded_at"])
        self.assertEqual("", record["indexing_error"])
        self.assertEqual(1, len(self.admin.documents()))

    def test_malformed_pdf_failure_is_recorded(self):
        metadata = replace(self.metadata, source_name="broken.pdf")
        with self.assertRaisesRegex(ValueError, "Cannot read"):
            ingest_bytes(self.admin.repository, b"%PDF-1.7 invalid", metadata)
        record = self.admin.documents()[0]
        self.assertEqual("failed", record["indexing_status"])
        self.assertIn("Cannot read", record["indexing_error"])

    def test_extraction_failure_retains_source_then_retry_extracts(self):
        library = self.admin.regulatory.library
        with patch("regulatory_poc.service.regulatory_library.extract_requirements",
                   side_effect=ValueError("Invalid PDF")):
            with self.assertRaisesRegex(ValueError, "Invalid PDF"):
                library.import_document(b"source", "test.pdf", "National regulation", "English", jurisdiction="Test")
        record = self.admin.documents()[0]
        self.assertEqual("failed", record["indexing_status"])
        self.assertEqual(0, record["requirement_count"])
        document_id = record["metadata"]["document_id"]
        self.assertEqual(b"source", self.admin.original(document_id)[0])
        metadata = DocumentMetadata.from_dict(record["metadata"])
        requirement = Requirement(
            key="example-1", identifier="Article 1", title="Safety", description="Ensure safety.",
            document_id=document_id, source_name="test.pdf", source_hash=metadata.source_version,
            page=1, end_page=1, language="English",
        )
        inventory = RequirementInventory(metadata, (requirement,), (), False)
        with patch("regulatory_poc.service.regulatory_library.extract_requirements", return_value=inventory):
            self.admin.retry(document_id)
        saved = self.admin.documents()
        self.assertEqual(1, len(saved))
        self.assertEqual("indexed", saved[0]["indexing_status"])
        self.assertEqual(1, saved[0]["requirement_count"])
        self.assertTrue(saved[0]["processed_at"])

    def test_legacy_index_is_visible_without_fabricated_timestamps(self):
        from regulatory_poc.repo.documents import chunk_document

        self.admin.repository.upsert(chunk_document("Legacy safety document.", self.metadata))
        record = self.admin.documents()[0]
        self.assertEqual("indexed", record["indexing_status"])
        self.assertIsNone(record["uploaded_at"])
        self.assertFalse(record["original_available"])

    def test_api_lists_failures_retries_and_filters_usage(self):
        from regulatory_poc.ui.api import app

        with patch.object(self.admin.repository, "upsert", side_effect=RuntimeError("Index failure")):
            with self.assertRaises(RuntimeError):
                ingest_bytes(self.admin.repository, b"Safety requirements.", self.metadata)
        with usage_scope(document_ids=["example"]):
            with self.admin.usage.call("model", "comparison") as call:
                call.response = SimpleNamespace(usage={"input_tokens": 100, "output_tokens": 50})
        with patch("regulatory_poc.ui.api._document_admin", return_value=self.admin):
            client = TestClient(app)
            response = client.get("/admin/documents")
            self.assertEqual(200, response.status_code)
            self.assertEqual("failed", response.json()[0]["indexing_status"])
            self.assertEqual(200, client.post("/admin/documents/example/retry").status_code)
            response = client.get("/admin/model-usage?document_id=example")
            self.assertEqual(100, response.json()["summary"]["input_tokens"])
            self.assertEqual(0, client.get("/admin/model-usage?document_id=absent").json()["summary"]["calls"])
            self.assertEqual(422, client.get("/admin/model-usage?start_date=invalid").status_code)
            self.assertEqual(422, client.get(
                "/admin/model-usage?start_date=2026-02-02&end_date=2026-01-01",
            ).status_code)
            self.assertEqual(422, client.post("/admin/documents/absent/retry").status_code)

    def test_admin_screens_render_filter_and_show_unknown_costs(self):
        ingest_bytes(self.admin.repository, b"Safety requirements.", self.metadata)
        with self.admin.usage.call("unpriced-model", "comparison") as call:
            call.response = SimpleNamespace(usage={"input_tokens": 100, "output_tokens": 20})
        app = AppTest.from_function(render_admin, args=(self.temp.name,), default_timeout=30).run()
        self.assertFalse(app.exception)
        metrics = {item.label: item.value for item in app.metric}
        self.assertEqual("1", metrics["Indexed"])
        self.assertEqual("100", metrics["Reported input tokens"])
        self.assertEqual("Unknown", metrics["Estimated USD (known calls)"])
        self.assertTrue(any("Totals are partial" in warning.value for warning in app.warning))
        app.selectbox(key="admin-status").select("failed").run()
        self.assertFalse(app.exception)
        self.assertTrue(any("No documents match" in item.value for item in app.info))
        app.text_area[0].set_value('{"model":{"input":3,"output":9}}')
        app.button(key="FormSubmitter:model-pricing-Save model prices").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(3, self.admin.usage.pricing["model"]["input"])

    def test_full_workbench_exposes_admin_and_usage_tabs_offline(self):
        import os

        script = Path(__file__).parents[1] / "src" / "regulatory_poc" / "ui" / "app.py"
        with patch.dict(os.environ, {
            "POC_AGENT_MODE": "offline", "POC_SEARCH_MODE": "local", "POC_SOURCE_KB": "",
            "POC_INDEX_PATH": str(Path(self.temp.name) / "index.json"), "POC_AUTH_ENABLED": "false",
        }):
            app = AppTest.from_file(str(script), default_timeout=30).run()
        self.assertFalse(app.exception)
        self.assertIn("Document admin", [tab.label for tab in app.tabs])
        self.assertIn("Model usage & costs", [tab.label for tab in app.tabs])
        self.assertFalse(self.admin.usage.events())


class UsageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LocalRecordStore(Path(self.temp.name))
        self.tracker = UsageTracker(self.store, PRICES)

    async def test_reported_tokens_rates_and_scope_are_persisted(self):
        with usage_scope(document_ids=["one", "two"], operation="requirement_comparison", report_id="report"):
            with self.tracker.call("model", "compare") as call:
                call.response = SimpleNamespace(usage_details={"input_token_count": 1000, "output_token_count": 250})
        event = UsageTracker(self.store).events()[0]
        self.assertEqual("requirement_comparison", event["operation"])
        self.assertEqual(["one", "two"], event["document_ids"])
        self.assertEqual("report", event["report_id"])
        self.assertAlmostEqual(0.004, event["estimated_cost_usd"])
        self.assertEqual({"input": 2, "output": 8}, event["rates_usd_per_million"])
        self.assertNotIn("prompt", event)

    async def test_concurrent_async_and_thread_calls_do_not_lose_scope_or_events(self):
        async def run(index):
            with usage_scope(document_ids=[str(index)]):
                with self.tracker.call("model", "comparison") as call:
                    await asyncio.sleep(0.001)
                    call.response = SimpleNamespace(usage={"input_tokens": index, "output_tokens": 1})
                await asyncio.to_thread(thread_call, index)

        def thread_call(index):
            with self.tracker.call("embedding", "embedding", embedding=True) as call:
                call.response = SimpleNamespace(usage={"prompt_tokens": index})

        await asyncio.gather(*(run(index) for index in range(10)))
        events = self.tracker.events()
        self.assertEqual(20, len(events))
        self.assertTrue(all(item["document_ids"] == [str(item["input_tokens"])] for item in events))

    async def test_unknown_usage_failures_and_missing_prices_are_not_free(self):
        with self.tracker.call("model", "no-usage"):
            pass
        with self.assertRaisesRegex(RuntimeError, "failed"):
            with self.tracker.call("model", "failure"):
                raise RuntimeError("failed")
        with self.tracker.call("unpriced", "comparison") as call:
            call.response = SimpleNamespace(usage={"input_tokens": 50, "output_tokens": 10})
        events = self.tracker.events()
        summary = usage_summary(events)
        self.assertEqual(3, summary["unpriced_or_unknown_calls"])
        self.assertEqual(2, summary["unknown_usage_calls"])
        self.assertIsNone(summary["estimated_cost_usd"])
        self.assertEqual(1, sum(item["status"] == "failed" for item in events))

    async def test_changed_rates_apply_to_existing_tracker_but_not_history(self):
        with self.tracker.call("model", "before") as call:
            call.response = SimpleNamespace(usage={"input_tokens": 1_000_000, "output_tokens": 0})
        UsageTracker(self.store).set_pricing('{"model":{"input":3,"output":10}}')
        with self.tracker.call("model", "after") as call:
            call.response = SimpleNamespace(usage={"input_tokens": 1_000_000, "output_tokens": 0})
        self.assertEqual({"before": 2, "after": 3},
                         {item["operation"]: item["estimated_cost_usd"] for item in self.tracker.events()})

    async def test_pricing_rejects_bad_shapes_and_numbers(self):
        for value in ('[]', '{"model":{"input":-1,"output":2}}', '{"model":{}}',
                      '{"model":{"input":true,"output":2}}', '{"model":{"input":NaN,"output":2}}'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_pricing(value)

    async def test_foundry_adapter_records_actual_response_usage_and_retries(self):
        from agent_framework import AgentResponse
        from agent_framework.exceptions import ChatClientException

        framework_agent = MagicMock()
        framework_agent.run = AsyncMock(side_effect=[
            ChatClientException("Temporary"),
            AgentResponse(usage_details={"input_token_count": 30, "output_token_count": 5}),
        ])
        # A non-transient failure is metered without retry; success is a separate call.
        with patch("agent_framework.Agent", return_value=framework_agent), patch(
            "agent_framework.foundry.FoundryChatClient",
        ):
            agent = FoundryComparisonAgent("https://example.test", "model", usage=self.tracker)
            with self.assertRaises(RuntimeError):
                await agent.compare("private prompt")
            await agent.compare("private prompt")
        events = self.tracker.events()
        self.assertEqual(2, len(events))
        self.assertEqual(30, next(item for item in events if item["status"] == "completed")["input_tokens"])

    async def test_embedding_adapter_records_actual_prompt_tokens(self):
        provider = FoundryEmbeddingProvider.__new__(FoundryEmbeddingProvider)
        provider._model, provider._usage = "embedding", self.tracker
        provider._openai = MagicMock()
        provider._openai.embeddings.create.return_value = SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=200), data=[SimpleNamespace(embedding=[0.1, 0.2])],
        )
        self.assertEqual([0.1, 0.2], provider.embed("private source text"))
        event = self.tracker.events()[0]
        self.assertEqual(200, event["input_tokens"])
        self.assertEqual(0, event["output_tokens"])
        self.assertAlmostEqual(0.00002, event["estimated_cost_usd"])

    async def test_inference_embedding_usage_is_recorded(self):
        provider = FoundryEmbeddingProvider.__new__(FoundryEmbeddingProvider)
        provider._model, provider._usage, provider._openai = "embedding", self.tracker, None
        provider._client = MagicMock()
        provider._client.embed.return_value = SimpleNamespace(
            usage={"prompt_tokens": 40}, data=[SimpleNamespace(embedding=[0.1])],
        )
        self.assertEqual([0.1], provider.embed("private source text"))
        self.assertEqual(40, self.tracker.events()[0]["input_tokens"])

    async def test_persisted_agent_records_usage_and_document_scope_in_thread(self):
        from regulatory_poc.repo.prompt_agents import PersistedFoundryAgent

        project, client = MagicMock(), MagicMock()
        project.get_openai_client.return_value.__enter__.return_value = client
        client.responses.create.return_value = SimpleNamespace(
            status="completed", output_text="Answer", usage=SimpleNamespace(input_tokens=12, output_tokens=3),
        )
        with patch("azure.ai.projects.AIProjectClient") as project_type, patch(
            "azure.identity.DefaultAzureCredential",
        ):
            project_type.return_value.__enter__.return_value = project
            agent = PersistedFoundryAgent("https://example.test", "agent", "1", self.tracker, "model")
            with usage_scope(document_ids=["source"], operation="evidence_chat"):
                self.assertEqual("Answer", await agent.answer("private prompt"))
        event = self.tracker.events()[0]
        self.assertEqual(["source"], event["document_ids"])
        self.assertEqual("evidence_chat", event["operation"])
        self.assertEqual(12, event["input_tokens"])
        self.assertEqual(3, event["output_tokens"])

    async def test_incomplete_persisted_response_keeps_reported_billable_usage(self):
        from regulatory_poc.repo.prompt_agents import PersistedFoundryAgent

        project, client = MagicMock(), MagicMock()
        project.get_openai_client.return_value.__enter__.return_value = client
        client.responses.create.return_value = SimpleNamespace(
            status="incomplete", output_text="", usage=SimpleNamespace(input_tokens=12, output_tokens=3),
        )
        with patch("azure.ai.projects.AIProjectClient") as project_type, patch(
            "azure.identity.DefaultAzureCredential",
        ):
            project_type.return_value.__enter__.return_value = project
            agent = PersistedFoundryAgent("https://example.test", "agent", "1", self.tracker, "model")
            with self.assertRaisesRegex(RuntimeError, "did not complete"):
                await agent.compare("private prompt")
        event = self.tracker.events()[0]
        self.assertEqual("failed", event["status"])
        self.assertEqual(12, event["input_tokens"])
        self.assertGreater(event["estimated_cost_usd"], 0)
