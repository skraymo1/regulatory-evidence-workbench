from __future__ import annotations

import json
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from azure.core.exceptions import HttpResponseError

from regulatory_poc.repo.documents import make_document_id
from regulatory_poc.repo.regulatory_store import LocalRecordStore, LocalSourceStore
from regulatory_poc.repo.search import JsonSearchRepository
from regulatory_poc.service.regulatory_comparison import RegulatoryTableService
from regulatory_poc.service.regulatory_mapping import validate_mapping
from regulatory_poc.service.regulatory_library import RegulatoryLibrary
from regulatory_poc.service.translation import verify_translation
from regulatory_poc.types.models import DocumentChunk, DocumentMetadata
from regulatory_poc.types.requirements import Requirement
from regulatory_poc.ui.api import app
from regulatory_poc.ui.regulatory import markdown_table, table_rows


def requirement(key: str, document: str, identifier: str = "Article 1") -> Requirement:
    return Requirement(
        key=key, identifier=identifier, title="Safety responsibility",
        description="The operator shall ensure safety.", document_id=document,
        source_name=document + ".pdf", source_hash="a" * 64, page=2, end_page=2,
        language="English",
    )


RULES = {
    "version": "test", "screen_instruction": "screen", "residual_instruction": "residual",
    "analysis_instruction": "analyse",
}


class MappingAgent:
    """Fake model that answers each pipeline stage; every clause is relevant unless disabled."""

    def __init__(self, relevant=True):
        self.calls = 0
        self.relevant = relevant
        self.tasks = []

    async def compare(self, prompt):
        self.calls += 1
        data = json.loads(prompt)
        self.tasks.append(data.get("task"))
        if data.get("task") in {"screen", "residual_screen"}:
            return json.dumps({
                "topics": [{"id": "T1", "topic": "Operator safety responsibility", "source": "statement"}],
                "relevant": [
                    {"key": item["key"], "topic_ids": ["T1"]} for item in data["clauses"]
                ] if self.relevant else [],
            })
        keys = [item["key"] for item in data["candidates"]]
        return json.dumps({
            "matches": [{
                "key": key, "analysis": "Proposed shared safety obligation; review scope.",
                "english_title": "", "english_description": "",
                "title": "Invented title must never become a quotation",
            } for key in keys],
            "topics": [{"id": "T1", "status": "explicit", "keys": keys, "explanation": "Stated."}],
            "national_only_topics": [], "terminology": [], "cited_instruments": [],
            "outcome": "proposed", "note": "Proposed mapping; expert review required.",
        })


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.state = LocalRecordStore(root)
        self.search = JsonSearchRepository(root / "source-index.json")
        self.report_search = JsonSearchRepository(root / "report-index.json")
        self.library = RegulatoryLibrary(self.state, LocalSourceStore(root / "sources"), self.search)
        self.agent = MappingAgent()
        self.service = RegulatoryTableService(
            self.library, self.state, self.agent, RULES, {"model": "test"},
            self.report_search,
        )
        self.baseline = self.add_document("reference", "Reference baseline", count=82)
        self.uk = self.add_document("uk", "UK")
        self.ar1 = self.add_document("ar1", "Argentina")
        self.ar2 = self.add_document("ar2", "Argentina")
        self.ko = self.add_document("ko", "Korea")
        self.en = self.add_document("en", "Korean English reference")

    def add_document(self, identifier, role, count=1):
        metadata = DocumentMetadata(
            document_id=identifier, title=identifier, authority=role, language="English",
            source_name=identifier + ".pdf", source_version="a" * 64,
            standard="Reference standard" if role == "Reference baseline" else identifier, revision="Rev. 1",
        )
        requirements = [
            requirement(f"{identifier}-{index}", identifier, f"Requirement {index + 1}" if count > 1 else "Article 1")
            for index in range(count)
        ]
        self.state.put(f"regulatory-documents/{identifier}.json", {
            "metadata": metadata.to_dict(), "role": role,
            "requirements": [item.to_dict() for item in requirements],
            "warnings": [], "complete": True, "indexing_status": "indexed",
        })
        self.search.upsert([
            DocumentChunk(item.key, f"REQKEY:{item.key}\n{item.title}\n{item.description}", 0, metadata)
            for item in requirements
        ])
        return identifier

    async def test_every_baseline_row_remains_and_argentina_is_collective(self):
        report = self.service.create(self.baseline, "Argentina", [self.ar1, self.ar2])
        self.assertEqual(82, len(report["rows"]))
        key = report["rows"][0]["baseline"]["key"]
        report = await self.service.process_row(report["report_id"], key)
        row = report["rows"][0]
        self.assertEqual("proposed", row["status"])
        self.assertEqual({"ar1", "ar2"}, {item["source"]["document_id"] for item in row["matches"]})
        self.assertTrue(all(item["source"]["title"] == "Safety responsibility" for item in row["matches"]))
        self.assertEqual(81, sum(item["status"] == "pending" for item in report["rows"]))
        self.assertEqual(82, len(table_rows(report)))
        self.assertIn("Requirement 82", markdown_table(report))

    async def test_resume_uses_saved_rows_and_pinned_inventory(self):
        report = self.service.create(self.baseline, "UK", [self.uk])
        key = report["rows"][0]["baseline"]["key"]
        source = self.library.get(self.uk)
        source["requirements"][0]["description"] = "Changed extraction must not change existing report."
        self.state.put(f"regulatory-documents/{self.uk}.json", source)
        processed = await self.service.process_row(report["report_id"], key)
        self.assertEqual("The operator shall ensure safety.", processed["rows"][0]["matches"][0]["source"]["description"])
        await self.service.process_row(report["report_id"], key)
        self.assertEqual(["screen", "analyse"], self.agent.tasks)
        next_report = self.service.create(self.baseline, "UK", [self.uk])
        self.assertNotEqual(report["report_id"], next_report["report_id"])

    async def test_concurrent_rows_do_not_erase_each_other(self):
        report = self.service.create(self.baseline, "UK", [self.uk])
        original_compare = self.agent.compare
        async def delayed(prompt):
            await asyncio.sleep(0.01)
            return await original_compare(prompt)
        with patch.object(self.agent, "compare", side_effect=delayed):
            await asyncio.gather(*[
                self.service.process_row(report["report_id"], row["baseline"]["key"])
                for row in report["rows"][:2]
            ])
        saved = self.service.get(report["report_id"])
        self.assertEqual(["proposed", "proposed"], [row["status"] for row in saved["rows"][:2]])

    async def test_new_row_invalidates_index_snapshot(self):
        report = self.service.create(self.baseline, "UK", [self.uk])
        self.service.index_report(report["report_id"])
        self.assertEqual("indexed", self.service.get(report["report_id"])["indexing_status"])
        report = await self.service.process_row(report["report_id"], report["rows"][0]["baseline"]["key"])
        self.assertEqual("pending", report["indexing_status"])

    async def test_offline_candidates_are_not_claimed_as_matches(self):
        self.service.agent = None
        report = self.service.create(self.baseline, "UK", [self.uk])
        report = await self.service.process_row(report["report_id"], report["rows"][0]["baseline"]["key"])
        self.assertEqual("candidates_only", report["rows"][0]["status"])
        self.assertEqual([], report["rows"][0]["matches"])
        self.assertTrue(report["rows"][0]["candidates"])

    async def test_no_relevant_clause_after_exhaustive_and_residual_screen_is_scoped_no_comparable(self):
        self.agent.relevant = False
        report = self.service.create(self.baseline, "UK", [self.uk])
        report = await self.service.process_row(report["report_id"], report["rows"][0]["baseline"]["key"])
        row = report["rows"][0]
        self.assertEqual("no_comparable", row["status"])
        self.assertEqual("major_differences", row["equivalence"])
        self.assertIn("not proof", row["note"])
        self.assertEqual(["screen", "residual_screen"], self.agent.tasks)
        self.assertEqual(["screen", "residual_screen"], [item["pass"] for item in row["screening"]["passes"]])

    async def test_index_failure_does_not_regenerate_and_report_chat_can_retrieve(self):
        report = self.service.create(self.baseline, "UK", [self.uk])
        report = await self.service.process_row(report["report_id"], report["rows"][0]["baseline"]["key"])
        with patch.object(self.report_search, "upsert", side_effect=RuntimeError("outage")):
            with self.assertRaisesRegex(RuntimeError, "Table saved"):
                self.service.index_report(report["report_id"])
        self.assertEqual("pending", self.service.get(report["report_id"])["indexing_status"])
        indexed = self.service.index_report(report["report_id"])
        self.assertEqual("indexed", indexed["indexing_status"])
        self.assertEqual(2, self.agent.calls)
        self.assertTrue(self.report_search.search("safety obligation", 5))

    async def test_model_error_is_durable_and_retryable(self):
        report = self.service.create(self.baseline, "UK", [self.uk])
        with patch.object(self.agent, "compare", return_value='{"matches":[{"key":"fabricated"}],"note":""}'):
            with self.assertRaisesRegex(RuntimeError, "enumerate"):
                await self.service.process_row(report["report_id"], report["rows"][0]["baseline"]["key"])
        self.assertEqual("error", self.service.get(report["report_id"])["rows"][0]["status"])
        report = await self.service.process_row(report["report_id"], report["rows"][0]["baseline"]["key"])
        self.assertEqual("proposed", report["rows"][0]["status"])

    async def test_azure_failure_preserves_retryable_row(self):
        report = self.service.create(self.baseline, "UK", [self.uk])
        with patch.object(self.agent, "compare", side_effect=HttpResponseError("service unavailable")):
            with self.assertRaisesRegex(RuntimeError, "Retry the saved table"):
                await self.service.process_row(report["report_id"], report["rows"][0]["baseline"]["key"])
        self.assertEqual("error", self.service.get(report["report_id"])["rows"][0]["status"])

    async def test_model_timeout_saves_error_and_explicit_resume_retries(self):
        report = self.service.create(self.baseline, "UK", [self.uk])
        key = report["rows"][0]["baseline"]["key"]
        async def stalled(prompt):
            await asyncio.sleep(60)
        with patch("regulatory_poc.service.regulatory_comparison.MODEL_TIMEOUT_SECONDS", 0.01):
            with patch.object(self.agent, "compare", side_effect=stalled):
                with self.assertRaisesRegex(RuntimeError, "exceeded 0.01 seconds"):
                    await self.service.process_row(report["report_id"], key)
        saved = self.service.get(report["report_id"])
        self.assertEqual("error", saved["rows"][0]["status"])
        self.assertIn("no result was saved", saved["rows"][0]["note"])
        self.assertEqual("proposed", (await self.service.process_row(report["report_id"], key))["rows"][0]["status"])

    def test_scope_validation_and_cache_invalidation(self):
        self.assertEqual(82, len(self.service.create(self.baseline, "Korea", [self.ko])["rows"]))
        with self.assertRaisesRegex(ValueError, "do not exist"):
            self.service.create(self.baseline, "Korea", [self.ko], ["Article 900"])
        with self.assertRaisesRegex(ValueError, "selected jurisdiction"):
            self.service.create(self.baseline, "UK", [self.ar1])
        report = self.service.create(self.baseline, "Korea", [self.ko], ["Article 1"], self.en)
        self.assertEqual(82, len(report["rows"]))
        self.assertTrue(any("revisions" in value for value in report["warnings"]))
        before = self.service.create(self.baseline, "Argentina", [self.ar1])
        after = self.service.create(self.baseline, "Argentina", [self.ar1, self.ar2])
        self.assertNotEqual(before["report_id"], after["report_id"])

    def test_source_inventory_survives_index_failure(self):
        metadata = DocumentMetadata("temporary", "test", "UK")
        from regulatory_poc.types.requirements import RequirementInventory
        inventory = RequirementInventory(metadata, (requirement("unit", "temporary"),), (), False)
        with patch("regulatory_poc.service.regulatory_library.extract_requirements", return_value=inventory):
            with patch.object(self.search, "upsert", side_effect=RuntimeError("outage")):
                with self.assertRaisesRegex(RuntimeError, "Source saved"):
                    self.library.import_document(b"source bytes", "test.pdf", "UK", "English")
        key = make_document_id("test.pdf", b"source bytes")
        record = self.library.get(key)
        self.assertEqual("pending", record["indexing_status"])
        content, _ = self.library.sources.download_version(DocumentMetadata.from_dict(record["metadata"]))
        self.assertEqual(b"source bytes", content)

    def test_api_create_resume_invalid_scope_and_download(self):
        with patch("regulatory_poc.ui.regulatory_api._service", return_value=self.service):
            with TestClient(app) as client:
                response = client.post("/regulatory/tables", json={
                    "baseline_id": self.baseline, "country": "Argentina",
                    "document_ids": [self.ar1, self.ar2],
                })
                self.assertEqual(201, response.status_code)
                report = response.json()
                self.assertEqual(82, len(report["rows"]))
                self.assertEqual(200, client.get("/regulatory/tables/" + report["report_id"]).status_code)
                self.assertEqual(422, client.post("/regulatory/tables", json={
                    "baseline_id": self.baseline, "country": "Unrelated jurisdiction", "document_ids": [self.ko],
                }).status_code)
                processed = client.post("/regulatory/tables/" + report["report_id"] + "/process-row",
                                        json={"requirement_key": report["rows"][0]["baseline"]["key"]})
                self.assertEqual(200, processed.status_code)
                self.assertEqual("proposed", processed.json()["rows"][0]["status"])

    async def test_translation_quote_guard_and_separate_result(self):
        source, reference = requirement("ko-0", "ko"), requirement("en-0", "en")
        payload = {
            "assessment": "no_difference_identified", "explanation": "Provisional example.",
            "source_quote": source.title, "reference_quote": reference.description,
        }
        with patch.object(self.agent, "compare", return_value=json.dumps(payload)):
            result = await verify_translation(self.agent, source, reference)
        self.assertEqual("unreviewed", result["review_status"])
        payload["source_quote"] = "invented"
        with patch.object(self.agent, "compare", return_value=json.dumps(payload)):
            with self.assertRaisesRegex(ValueError, "exact source"):
                await verify_translation(self.agent, source, reference)

    async def test_korean_translation_can_run_before_any_mapping(self):
        report = self.service.create(self.baseline, "Korea", [self.ko], ["Article 1"], self.en)
        self.service.translation_agent = self.agent
        payload = {
            "assessment": "potential_difference", "explanation": "Review the supplied versions.",
            "source_quote": "Safety responsibility", "reference_quote": "The operator shall ensure safety.",
        }
        with patch.object(self.agent, "compare", return_value=json.dumps(payload)):
            report = await self.service.verify_scope_article(report["report_id"], "ko-0")
        self.assertEqual(82, sum(row["status"] == "pending" for row in report["rows"]))
        self.assertEqual("potential_difference", report["article_checks"]["ko-0"]["assessment"])
        with self.assertRaisesRegex(ValueError, "outside"):
            await self.service.verify_scope_article(report["report_id"], "not-selected")

    def test_mapping_rejects_unstructured_and_duplicate_outputs(self):
        with self.assertRaises(ValueError):
            validate_mapping("not JSON", [], [])
        match = {"key": "x", "analysis": "test", "english_title": "", "english_description": ""}
        payload = {"matches": [match, match], "topics": [], "national_only_topics": [], "terminology": [],
                   "outcome": "proposed", "note": "n"}
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_mapping(json.dumps(payload), [{"key": "x"}], [])
