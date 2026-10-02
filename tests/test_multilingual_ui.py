from __future__ import annotations

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from streamlit.testing.v1 import AppTest

from test_regulatory_ui import example_report, render_saved_table


def render_setup(service, documents):
    from regulatory_poc.ui.regulatory_setup import comparison_setup
    labels = {item["metadata"]["document_id"]: item["metadata"]["title"] for item in documents}
    comparison_setup(service, "baseline", documents, labels)


def render_sources(service):
    from regulatory_poc.ui.regulatory_sources import comparison_sources
    comparison_sources(service, None)


class MultilingualUiTests(unittest.TestCase):
    def test_source_language_and_jurisdiction_are_free_text_not_fixed_options(self):
        service = Mock()
        service.library.list_documents.return_value = []
        app = AppTest.from_function(render_sources, args=(service,), default_timeout=30).run()
        self.assertFalse(app.exception)
        self.assertEqual(["Reference baseline", "National regulation", "Translation reference"], app.selectbox[0].options)
        app.selectbox[0].select("National regulation").run()
        next(item for item in app.text_input if item.label == "Source language").set_value("Thai").run()
        next(item for item in app.text_input if item.label == "Jurisdiction").set_value("Example authority").run()
        self.assertFalse(app.exception)
        self.assertEqual("Thai", next(item for item in app.text_input if item.label == "Source language").value)
        self.assertTrue(app.button[0].disabled)
        service.library.import_document.assert_not_called()

    def test_arbitrary_jurisdiction_collection_keyed_scope_and_reference_pairing(self):
        documents = [
            {"role": role, "metadata": {
                "document_id": key, "title": key, "country": "Example authority", "language": language,
            }}
            for key, role, language in [
                ("source-a", "National regulation", "Portuguese"),
                ("source-b", "National regulation", "Japanese"),
                ("reference", "Translation reference", "French"),
            ]
        ]
        service = Mock()
        service.library.requirements.side_effect = lambda key: [
            SimpleNamespace(key=key + "-1", identifier="1", title="Original title", source_name=key)
        ]
        app = AppTest.from_function(render_setup, args=(service, documents), default_timeout=30).run()
        self.assertFalse(app.exception)
        self.assertEqual(["Example authority"], app.selectbox(key="comparison-jurisdiction").options)
        self.assertEqual(["source-a", "source-b"],
                         app.multiselect(key="comparison-documents-Example authority").value)
        app.radio(key="comparison-scope-Example authority").set_value("Selected requirements only").run()
        app.checkbox(key="comparison-scope-acknowledged").check().run()
        self.assertTrue(app.button(key="create-comparison").disabled)
        app.multiselect(key="comparison-requirements-Example authority").set_value(["source-b-1"]).run()
        app.selectbox(key="comparison-reference-Example authority").select("reference").run()
        app.selectbox(key="comparison-reference-source-Example authority").select("source-b").run()
        report = {
            "report_id": "a" * 64, "rows": [], "inputs": {
                "baseline": {"document_id": "baseline"}, "country": "Example authority",
                "sources": [item["metadata"] for item in documents], "selected_ids": ["source-b-1"],
                "reference_id": "reference", "reference_source_id": "source-b",
            },
        }
        service.create.return_value = report
        service.get.return_value = report
        app.button(key="create-comparison").click().run()
        self.assertFalse(app.exception)
        service.create.assert_called_once_with(
            "baseline", "Example authority", ["source-a", "source-b"], ["source-b-1"], "reference",
            reference_source_id="source-b",
        )
        app.multiselect(key="comparison-documents-Example authority").set_value(["source-a"]).run()
        self.assertTrue(any("Selection changed" in item.value for item in app.warning))

    def test_non_english_reference_and_generic_check_render_without_country_gate(self):
        report = example_report("Example authority")
        original = report["inventories"]["national"][0]
        original["language"] = "Portuguese"
        reference = copy.deepcopy(original)
        reference.update(key="reference-1", document_id="reference", source_name="reference.pdf",
                         language="French", title="Reference title", description="Reference description")
        report["inputs"].update(reference_id="reference", reference_source_id="national", selected_ids=[])
        report["inputs"]["sources"].append(
            {"document_id": "reference", "title": "Reference", "language": "French"}
        )
        report["inventories"]["reference"] = [reference]
        report["article_checks"] = {
            original["key"]: {
                "assessment": "potential_difference", "explanation": "Expert review needed.",
                "source_quote": original["title"], "reference_quote": reference["title"],
            }
        }
        original["english_reference"] = reference
        app = AppTest.from_function(render_saved_table, args=(report,), default_timeout=30).run()
        self.assertFalse(app.exception)
        self.assertTrue(any("Original language: Portuguese" in item.value for item in app.caption))
        self.assertTrue(any("Reference language: French" in item.value for item in app.caption))
        self.assertTrue(any(item.label == "Verify this requirement's translation" for item in app.button))
        self.assertTrue(any(item.label == "Verify original name and description against reference"
                            for item in app.button))
        self.assertTrue(any(item.value == "Reference title" for item in app.code))
        self.assertFalse(any("Korean" in item.label for item in app.button))

    def test_ambiguous_reference_is_not_presented_as_aligned(self):
        report = example_report("Example authority")
        report["inputs"]["reference_id"] = "reference"
        report["inventories"]["reference"] = []
        app = AppTest.from_function(render_saved_table, args=(report,), default_timeout=30).run()
        self.assertFalse(app.exception)
        self.assertTrue(any("pairing is ambiguous" in item.value for item in app.warning))
