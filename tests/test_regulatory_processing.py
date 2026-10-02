from __future__ import annotations

import asyncio
import copy
import unittest
from unittest.mock import AsyncMock, Mock, patch

from streamlit.testing.v1 import AppTest

from test_multilingual_ui import render_setup
from test_regulatory_ui import example_report


def render_processing(service, report, parallel=3):
    from regulatory_poc.ui.regulatory_processing import process_requirements
    process_requirements(service, report, parallel=parallel)


class ProcessingUiTests(unittest.TestCase):
    def service_and_report(self):
        report = example_report("Example authority")
        report["inputs"]["baseline"]["document_id"] = "baseline"
        report["inputs"]["sources"] = [{"document_id": "national"}]
        report["inputs"]["reference_source_id"] = ""
        report["rows"] = report["rows"][:4]
        service = Mock()
        service.library.requirements.return_value = []
        service.create.return_value = report
        service.get.side_effect = lambda _: copy.deepcopy(report)
        async def process(report_id, key):
            row = next(item for item in report["rows"] if item["baseline"]["key"] == key)
            row.update(status="proposed", note="Saved model result.")
            return copy.deepcopy(report)
        service.process_row = AsyncMock(side_effect=process)
        return service, report

    def test_create_processes_pending_and_errors_once_but_viewing_does_not(self):
        service, report = self.service_and_report()
        report["rows"][3]["status"] = "pending"
        documents = [{
            "role": "National regulation", "metadata": {
                "document_id": "national", "title": "Original",
                "country": "Example authority", "language": "Spanish",
            },
        }]
        app = AppTest.from_function(render_setup, args=(service, documents), default_timeout=30).run()
        service.process_row.assert_not_called()
        app.checkbox(key="comparison-scope-acknowledged").check().run()
        self.assertEqual("All pending", app.selectbox(key="create-process-count").value)
        app.button(key="create-comparison").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(["reference-2", "reference-3"],
                         [call.args[1] for call in service.process_row.call_args_list])
        self.assertTrue(any("4/4 rows processed" in item.value for item in app.info))
        app.run()
        app.button(key="create-comparison").click().run()
        self.assertEqual(2, service.process_row.call_count)

    def test_single_failure_shows_warning_and_preserves_progress(self):
        service, report = self.service_and_report()
        report["rows"][3]["status"] = "pending"
        service.process_row.side_effect = RuntimeError("Timed out; retry the saved row.")
        app = AppTest.from_function(render_processing, args=(service, report, 1), default_timeout=30).run()
        self.assertFalse(app.exception)
        self.assertEqual(2, service.process_row.call_count)
        self.assertTrue(any("Timed out" in item.value for item in app.warning))

    def test_parallel_rows_respect_limit_and_process_all_pending(self):
        report = example_report("Example authority")
        for row in report["rows"][:6]:
            row["status"] = "pending"
        report["rows"] = report["rows"][:6]
        service = Mock()
        service.get.side_effect = lambda _: copy.deepcopy(report)
        state = {"active": 0, "peak": 0}
        async def process(report_id, key):
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            await asyncio.sleep(0.05)
            state["active"] -= 1
            next(item for item in report["rows"] if item["baseline"]["key"] == key)["status"] = "proposed"
            return copy.deepcopy(report)
        service.process_row = AsyncMock(side_effect=process)
        app = AppTest.from_function(render_processing, args=(service, report, 3), default_timeout=30).run()
        self.assertFalse(app.exception)
        self.assertEqual(6, service.process_row.call_count)
        self.assertEqual(3, state["peak"])
        self.assertTrue(any("6/6 rows processed" in item.value for item in app.info))

    def test_one_failed_row_is_saved_while_other_rows_continue(self):
        report = example_report("Example authority")
        for row in report["rows"][:6]:
            row["status"] = "pending"
        report["rows"] = report["rows"][:6]
        first_key = report["rows"][0]["baseline"]["key"]
        service = Mock()
        service.get.side_effect = lambda _: copy.deepcopy(report)
        async def process(report_id, key):
            if key == first_key:
                raise RuntimeError("Quota exceeded.")
            await asyncio.sleep(0.05)
            next(item for item in report["rows"] if item["baseline"]["key"] == key)["status"] = "proposed"
            return copy.deepcopy(report)
        service.process_row = AsyncMock(side_effect=process)
        app = AppTest.from_function(render_processing, args=(service, report, 3), default_timeout=30).run()
        self.assertFalse(app.exception)
        self.assertEqual(6, service.process_row.call_count)
        self.assertEqual(5, sum(row["status"] == "proposed" for row in report["rows"]))
        self.assertTrue(any("Quota exceeded" in item.value for item in app.warning))
        self.assertFalse(app.error)

    def test_consecutive_failures_stop_starting_new_rows(self):
        service, report = self.service_and_report()
        for row in report["rows"]:
            row["status"] = "pending"
        service.process_row.side_effect = RuntimeError("Sign-in failed.")
        app = AppTest.from_function(render_processing, args=(service, report, 1), default_timeout=30).run()
        self.assertFalse(app.exception)
        self.assertEqual(3, service.process_row.call_count)
        self.assertTrue(any("3 consecutive failures" in item.value for item in app.error))

    def test_batch_limit_pauses_without_starting_more_work(self):
        service, report = self.service_and_report()
        with patch("regulatory_poc.ui.regulatory_processing.BATCH_SECONDS", 0):
            app = AppTest.from_function(render_processing, args=(service, report), default_timeout=30).run()
        self.assertFalse(app.exception)
        service.process_row.assert_not_called()
        self.assertTrue(any("batch limit" in item.value for item in app.warning))
