from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock, patch

from azure.core.exceptions import HttpResponseError
from streamlit.testing.v1 import AppTest

from regulatory_poc.types.models import ChatResult


def render_chat(azure_mode=False, empty=False):
    from types import SimpleNamespace
    from regulatory_poc.ui.regulatory_chat import evidence_chat
    documents = [{
        "role": "UK", "indexing_status": "indexed",
        "metadata": {"document_id": "uk", "title": "UK SAPs", "language": "English"},
    }, {
        "role": "Reference baseline", "indexing_status": "indexed",
        "metadata": {"document_id": "reference", "title": "Reference standard", "language": "English"},
    }]
    reports = [
        {"report_id": "table", "indexing_status": "indexed", "created_at": "2026-09-17",
         "inputs": {"country": "UK"}},
        {"report_id": "stale", "indexing_status": "pending", "created_at": "2026-09-16",
         "inputs": {"country": "Argentina"}},
    ]
    evidence_chat(
        SimpleNamespace(source_knowledge_base="source-kb" if azure_mode else ""),
        "source-repository", "report-repository",
        [] if empty else documents, [] if empty else reports,
    )


class EvidenceChatUiTests(unittest.TestCase):
    def test_source_and_report_search_have_separate_evidence_and_histories(self):
        backend = Mock()
        backend.answer = AsyncMock(side_effect=[
            ChatResult("source-conversation", "Source answer."),
            ChatResult("report-conversation", "Report answer."),
        ])
        with patch("regulatory_poc.ui.regulatory_chat.build_chat_service", return_value=backend) as build:
            app = AppTest.from_function(render_chat).run()
            self.assertFalse(app.exception)
            build.assert_not_called()
            app.chat_input(key="regulatory-source-input").set_value("What are the obligations?").run()
            self.assertFalse(app.exception)
            self.assertEqual("source-repository", build.call_args.args[1])
            self.assertFalse(build.call_args.kwargs["reports"])
            request = backend.answer.call_args.args[0]
            self.assertEqual(("uk", "reference"), request.filters.document_ids)
            self.assertEqual("", request.conversation_id)
            self.assertEqual(2, len(app.chat_message))
            app.radio(key="evidence-scope").set_value("Saved comparison tables").run()
            self.assertFalse(app.exception)
            self.assertFalse(app.chat_message)
            self.assertEqual(1, len(app.multiselect[0].options))
            self.assertTrue(any("not authoritative" in item.value for item in app.warning))
            app.chat_input(key="regulatory-report-input").set_value("Summarize the saved comparison.").run()
            self.assertFalse(app.exception)
            self.assertEqual("report-repository", build.call_args.args[1])
            self.assertFalse(build.call_args.kwargs["reports"])
            request = backend.answer.call_args.args[0]
            self.assertEqual(("table",), request.filters.document_ids)
            self.assertEqual("", request.conversation_id)
            app.radio(key="evidence-scope").set_value("Original regulations").run()
            self.assertEqual("source-conversation", app.session_state["regulatory-source-conversation"])
            self.assertEqual("report-conversation", app.session_state["regulatory-report-conversation"])
            self.assertEqual("Source answer.", app.chat_message[1].markdown[0].value)
            self.assertEqual(2, build.call_count)

    def test_cloud_report_chat_uses_report_knowledge_base_and_selected_scope(self):
        backend = Mock(answer=AsyncMock(return_value=ChatResult("conversation", "Answer.")))
        with patch("regulatory_poc.ui.regulatory_chat.build_chat_service", return_value=backend) as build:
            app = AppTest.from_function(render_chat, args=(True,)).run()
            app.radio(key="evidence-scope").set_value("Saved comparison tables").run()
            app.chat_input(key="regulatory-report-input").set_value("Compare the findings.").run()
            self.assertFalse(app.exception)
            self.assertTrue(build.call_args.kwargs["reports"])
            self.assertEqual(("table",), backend.answer.call_args.args[0].filters.document_ids)

    def test_empty_evidence_disables_chat_without_unscoped_retrieval(self):
        with patch("regulatory_poc.ui.regulatory_chat.build_chat_service") as build:
            app = AppTest.from_function(render_chat, args=(False, True)).run()
            self.assertFalse(app.exception)
            self.assertFalse(app.chat_input)
            self.assertTrue(any("Import and index" in item.value for item in app.info))
            app.radio(key="evidence-scope").set_value("Saved comparison tables").run()
            self.assertFalse(app.chat_input)
            self.assertTrue(any("Index a saved comparison table" in item.value for item in app.info))
            build.assert_not_called()

    def test_selected_documents_and_errors_are_explicit(self):
        backend = Mock(answer=AsyncMock(side_effect=HttpResponseError(message="Search unavailable")))
        with patch("regulatory_poc.ui.regulatory_chat.build_chat_service", return_value=backend):
            app = AppTest.from_function(render_chat).run()
            app.multiselect(key="regulatory-source-filter").set_value(["uk"]).run()
            app.chat_input(key="regulatory-source-input").set_value("UK obligations?").run()
            self.assertFalse(app.exception)
            self.assertEqual(("uk",), backend.answer.call_args.args[0].filters.document_ids)
            self.assertIn("Retry your question", app.error[0].value)
            self.assertEqual([], app.session_state["regulatory-source-messages"])
