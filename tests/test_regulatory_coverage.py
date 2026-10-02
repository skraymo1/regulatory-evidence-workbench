import copy
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

import test_regulatory_workflow as workflow
from test_regulatory_ui import example_report, render_saved_table
from regulatory_poc.service.regulatory_mapping import validate_mapping
from regulatory_poc.service.regulatory_coverage import national_rows
from regulatory_poc.ui.api import app
from regulatory_poc.ui.regulatory import markdown_table, table_rows


class CoverageTests(unittest.IsolatedAsyncioTestCase):
    add_document = workflow.WorkflowTests.add_document

    def setUp(self):
        workflow.WorkflowTests.setUp(self)

    async def test_explicit_no_comparable_is_persisted_and_exported(self):
        report = self.service.create(self.baseline, "Argentina", [self.ar1, self.ar2])
        self.agent.relevant = False
        report = await self.service.process_row(report["report_id"], report["rows"][0]["baseline"]["key"])
        self.assertEqual("no_comparable", report["rows"][0]["status"])
        self.assertEqual(82, len(report["rows"]))
        self.assertEqual(84, len(table_rows(report)))
        self.assertIn("No comparable national requirement found", markdown_table(report))
        self.assertIn("Operator safety responsibility (not explicit)", markdown_table(report))
        self.assertEqual({"national_pending"}, {row["status"] for row in report["national_rows"]})
        self.assertEqual({"ar1", "ar2"}, {row["source"]["document_id"] for row in report["national_rows"]})
        with patch("regulatory_poc.ui.regulatory_api._service", return_value=self.service):
            with TestClient(app) as client:
                saved = client.get("/regulatory/tables/" + report["report_id"]).json()
        self.assertEqual(report["national_rows"], saved["national_rows"])
        self.assertEqual("no_comparable", saved["rows"][0]["status"])
        self.service.index_report(report["report_id"])
        self.assertTrue(self.report_search.search("national_pending", 10))

    async def test_insufficient_evidence_remains_unresolved_without_residual_pass(self):
        report = self.service.create(self.baseline, "UK", [self.uk])
        original = self.agent.compare
        async def insufficient(prompt):
            if json.loads(prompt)["task"] != "analyse":
                return await original(prompt)
            self.agent.tasks.append("analyse")
            return json.dumps({
                "matches": [], "topics": [{"id": "T1", "status": "not_explicit", "keys": []}],
                "national_only_topics": [], "terminology": [], "outcome": "insufficient_evidence",
                "note": "Extraction is too fragmentary to judge.",
            })
        with patch.object(self.agent, "compare", side_effect=insufficient):
            report = await self.service.process_row(report["report_id"], report["rows"][0]["baseline"]["key"])
        self.assertEqual("unresolved", report["rows"][0]["status"])
        self.assertEqual("not_assessed", report["rows"][0]["equivalence"])
        self.assertEqual(["screen", "analyse"], self.agent.tasks)

    def test_exhaustive_screening_marks_unmatched_clause_country_specific(self):
        report = self.service.create(self.baseline, "UK", [self.uk])
        for row in report["rows"]:
            row.update(status="no_comparable", candidates=[], screening={"complete": True})
        result = national_rows(report)
        self.assertEqual("country_specific_candidate", result[0]["status"])
        self.assertIn("screened against every Reference requirement", result[0]["note"])
        report["rows"][5]["screening"]["complete"] = False
        self.assertEqual("national_unresolved", national_rows(report)[0]["status"])

    def test_completed_coverage_is_only_potential_country_specific(self):
        report = self.service.create(self.baseline, "UK", [self.uk])
        source = copy.deepcopy(report["inventories"][self.uk][0])
        for row in report["rows"]:
            row.update(status="no_comparable", candidates=[source])
        result = national_rows(report)
        self.assertEqual("country_specific_candidate", result[0]["status"])
        self.assertIn("not confirmed national uniqueness", result[0]["note"])
        self.assertEqual(source, result[0]["source"])
        for state in ("unresolved", "candidates_only", "error", "pending"):
            report["rows"][0]["status"] = state
            self.assertNotEqual("country_specific_candidate", national_rows(report)[0]["status"])
        report["rows"][0]["status"] = "no_comparable"
        for row in report["rows"]:
            row["candidates"] = []
        self.assertEqual("national_unresolved", national_rows(report)[0]["status"])

    async def test_scope_reference_pinning_and_matching_remove_extra_rows(self):
        report = self.service.create(self.baseline, "Korea", [self.ko], ["ko-0"], self.en)
        self.assertEqual(["ko-0"], [row["source"]["key"] for row in report["national_rows"]])
        changed = self.library.get(self.ko)
        changed["requirements"][0]["description"] = "New inventory text"
        self.state.put(f"regulatory-documents/{self.ko}.json", changed)
        self.assertEqual("The operator shall ensure safety.",
                         self.service.get(report["report_id"])["national_rows"][0]["source"]["description"])
        self.service.index_report(report["report_id"])
        updated = await self.service.process_row(report["report_id"], report["rows"][0]["baseline"]["key"])
        self.assertEqual([], updated["national_rows"])
        self.assertEqual("pending", updated["indexing_status"])
        self.assertEqual(2, self.agent.calls)
        self.service.get(report["report_id"])
        self.assertEqual(2, self.agent.calls)
        self.add_document("scope", "UK", count=2)
        scoped = self.service.create(self.baseline, "UK", ["scope"], ["scope-1"])
        self.assertEqual(["scope-1"], [row["source"]["key"] for row in scoped["national_rows"]])

    def test_old_report_adds_coverage_without_reclassifying_or_writing(self):
        report = self.service.create(self.baseline, "UK", [self.uk])
        saved = self.state.get(f"regulatory-reports/{report['report_id']}.json")
        saved["inputs"]["schema_version"] = "regulatory-table-v3"
        saved["rows"][0].update(status="unresolved", note="Historic uncertainty.")
        self.state.put(f"regulatory-reports/{report['report_id']}.json", saved)
        with patch.object(self.state, "put") as write:
            loaded = self.service.get(report["report_id"])
        write.assert_not_called()
        self.assertEqual("unresolved", loaded["rows"][0]["status"])
        self.assertEqual(1, len(loaded["national_rows"]))
        self.assertEqual(0, self.agent.calls)

    def test_reject_contradictory_malformed_or_unexplained_outcomes(self):
        topics = [{"id": "T1", "topic": "Leak tightness", "source": "statement"}]
        candidates = [{"key": "x"}]
        match = [{"key": "x", "analysis": "a", "english_title": "", "english_description": ""}]
        base = {"matches": [], "topics": [{"id": "T1", "status": "not_explicit", "keys": []}],
                "national_only_topics": [], "terminology": [], "outcome": "no_comparable", "note": "n"}
        for change in [
            {"outcome": "proposed"},
            {"note": " "},
            {"outcome": {}},
            {"outcome": "absent"},
            {"topics": []},
            {"matches": match, "outcome": "proposed"},
            {"matches": match, "outcome": "proposed",
             "topics": [{"id": "T1", "status": "explicit", "keys": []}]},
            {"matches": match, "outcome": "proposed",
             "topics": [{"id": "T1", "status": "explicit", "keys": ["y"]}]},
            {"matches": match, "outcome": "insufficient_evidence",
             "topics": [{"id": "T1", "status": "partial", "keys": ["x"]}]},
            {"terminology": [{"term": "confinamiento"}]},
        ]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_mapping(json.dumps({**base, **change}), candidates, topics)
        valid = validate_mapping(json.dumps({**base, "matches": match, "outcome": "proposed",
                                             "topics": [{"id": "T1", "status": "partial", "keys": ["x"]}]}),
                                 candidates, topics)
        self.assertEqual("partial", valid["topics"][0]["status"])

    def test_detail_and_markdown_show_unmatched_original_without_invented_translation(self):
        report = example_report()
        report["rows"][0].update(status="no_comparable", matches=[], note="No comparable reviewed evidence.")
        for row in report["rows"][1:]:
            row.update(status="no_comparable", note="Reviewed candidates.")
        report["rows"][0]["candidates"] = [
            item for inventory in report["inventories"].values() for item in inventory
        ]
        result = AppTest.from_function(render_saved_table, args=(report,), default_timeout=30).run()
        self.assertFalse(result.exception)
        table = result.tabs[0].dataframe[0].value
        self.assertEqual(84, len(table))
        self.assertEqual("country_specific_candidate", table.iloc[-1]["Status"])
        self.assertEqual("", table.iloc[-1]["National English translation (machine-generated)"])
        self.assertEqual("Argentina", table.iloc[-1]["Jurisdiction"])
        source = report["inventories"]["second"][0]
        result.tabs[2].selectbox[0].select("national:" + source["key"]).run()
        self.assertFalse(result.exception)
        self.assertEqual(source["description"], result.tabs[2].code[1].value)
        self.assertIn("not confirmed national uniqueness", result.tabs[2].info[0].value)
        self.assertIn("second.pdf", markdown_table(report))
        self.assertIn("\\|", markdown_table(report))
