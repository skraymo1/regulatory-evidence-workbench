from __future__ import annotations

import json
import unittest
from dataclasses import replace
from unittest.mock import patch

import test_regulatory_workflow as workflow
from test_regulatory_workflow import requirement
from test_requirement_extraction import METADATA, pdf_bytes
from regulatory_poc.repo.requirement_extraction import extract_requirements
from regulatory_poc.repo.requirement_source import Source
from regulatory_poc.service.regulatory_library import ROLES, document_jurisdiction, document_role
from regulatory_poc.service.translation import verify_translation
from regulatory_poc.ui.regulatory_api import CountryTableBody, create_table, import_document
from fastapi import UploadFile
from io import BytesIO


class MultilingualBackendTests(unittest.IsolatedAsyncioTestCase):
    setUp = workflow.WorkflowTests.setUp
    add_document = workflow.WorkflowTests.add_document

    def national(self, identifier, jurisdiction="Canton experimental", language="Français"):
        self.add_document(identifier, "National regulation")
        record = self.library.get(identifier)
        record["jurisdiction"] = jurisdiction
        record["metadata"]["country"] = jurisdiction
        record["metadata"]["language"] = language
        record["requirements"][0]["language"] = language
        self.state.put(f"regulatory-documents/{identifier}.json", record)
        return identifier

    def test_roles_normalize_without_mutating_legacy_records(self):
        self.assertEqual(("Reference baseline", "National regulation", "Translation reference"), ROLES)
        for identifier, jurisdiction in ((self.uk, "UK"), (self.ko, "Korea"), (self.ar1, "Argentina")):
            before = self.library.get(identifier)
            self.assertEqual("National regulation", document_role(before))
            self.assertEqual(jurisdiction, document_jurisdiction(before))
            self.assertEqual(before, self.library.get(identifier))
        self.assertEqual("Translation reference", document_role(self.library.get(self.en)))
        self.assertEqual("Korea", document_jurisdiction(self.library.get(self.en)))

    async def test_arbitrary_jurisdiction_multidocument_keyed_scope(self):
        fr, de = self.national("fr"), self.national("de", language="Deutsch")
        report = self.service.create(self.baseline, "Canton experimental", [fr, de], ["de-0"])
        self.assertEqual(["de-0"], report["inputs"]["selected_ids"])
        self.assertTrue(any("screens every selected national clause" in warning for warning in report["warnings"]))
        report = await self.service.process_row(report["report_id"], report["rows"][0]["baseline"]["key"])
        self.assertEqual(["de"], [item["source"]["document_id"] for item in report["rows"][0]["matches"]])
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.service.create(self.baseline, "Canton experimental", [fr, de], ["Article 1"])
        with self.assertRaisesRegex(ValueError, "keys"):
            self.service.create(self.baseline, "Canton experimental", [fr], ["Article 1"])

    async def test_reference_association_scopes_candidates_and_verification(self):
        fr, de = self.national("fr"), self.national("de", language="Deutsch")
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.service.create(self.baseline, "Canton experimental", [fr, de], reference_id=self.en)
        report = self.service.create(self.baseline, "Canton experimental", [fr, de],
                                     reference_id=self.en, reference_source_id=fr)
        self.assertEqual(fr, report["inputs"]["reference_source_id"])
        candidates = self.service._clauses(report)
        self.assertIsNotNone(next(item for item in candidates if item["key"] == "fr-0")["english_reference"])
        self.assertIsNone(next(item for item in candidates if item["key"] == "de-0")["english_reference"])
        with self.assertRaisesRegex(ValueError, "associated"):
            await self.service.verify_scope_article(report["report_id"], "de-0")
        self.service.translation_agent = self.agent
        payload = {"assessment": "insufficient_evidence", "explanation": "Review versions.",
                   "source_quote": "Safety responsibility", "reference_quote": "Safety responsibility"}
        with patch.object(self.agent, "compare", return_value=json.dumps(payload)):
            report = await self.service.verify_scope_article(report["report_id"], "fr-0")
        self.assertEqual("Français", report["article_checks"]["fr-0"]["source"]["language"])

    def test_reference_validation_duplicate_and_absent_identifiers(self):
        fr = self.national("fr")
        with self.assertRaisesRegex(ValueError, "Translation reference"):
            self.service.create(self.baseline, "Canton experimental", [fr], reference_id=self.uk)
        for duplicate_document in (fr, self.en):
            record = self.library.get(duplicate_document)
            original = list(record["requirements"])
            record["requirements"].append({**original[0], "key": "duplicate"})
            self.state.put(f"regulatory-documents/{duplicate_document}.json", record)
            with self.assertRaisesRegex(ValueError, "Ambiguous duplicate"):
                self.service.create(self.baseline, "Canton experimental", [fr], reference_id=self.en)
            record["requirements"] = original
            self.state.put(f"regulatory-documents/{duplicate_document}.json", record)
        record = self.library.get(self.en)
        record["requirements"][0]["identifier"] = "Article 99"
        self.state.put(f"regulatory-documents/{self.en}.json", record)
        with self.assertRaisesRegex(ValueError, "No same-identifier"):
            self.service.create(self.baseline, "Canton experimental", [fr], reference_id=self.en)

    def test_legacy_saved_report_preserves_id_and_translation_fields(self):
        report = self.service.create(self.baseline, "Korea", [self.ko], ["Article 1"], self.en)
        report["report_id"] = "b" * 64
        report["inputs"]["schema_version"] = "regulatory-table-v2"
        report["inputs"]["selected_ids"] = ["Article 1"]
        report["inputs"].pop("reference_source_id")
        report["rows"][0]["translation_checks"] = {"ko-0": {"korean_quote": "old", "english_quote": "saved"}}
        self.service._save(report)
        saved = self.service.get("b" * 64)
        self.assertEqual("b" * 64, saved["report_id"])
        self.assertEqual("old", saved["rows"][0]["translation_checks"]["ko-0"]["korean_quote"])
        self.assertEqual(["ko-0"], [item.key for item in self.service._national(saved)])
        fresh = self.service.create(self.baseline, "Korea", [self.ko], ["Article 1"], self.en)
        self.assertNotEqual(saved["report_id"], fresh["report_id"])
        self.assertEqual("regulatory-table-v5", fresh["inputs"]["schema_version"])

    async def test_multilingual_payload_and_exact_quote_validation(self):
        source = replace(requirement("fr", "fr"), title="Sûreté exacte", description="Préserver  le texte.", language="Français")
        reference = replace(requirement("de", "de"), title="Genaue Sicherheit", description="Text  bewahren.", language="Deutsch")
        payload = {"assessment": "no_difference_identified", "explanation": "Unreviewed comparison.",
                   "source_quote": source.title, "reference_quote": reference.description}
        with patch.object(self.agent, "compare", return_value=json.dumps(payload)) as compare:
            result = await verify_translation(self.agent, source, reference)
        sent = json.loads(compare.call_args.args[0])
        self.assertEqual("Français", sent["source"]["language"])
        self.assertEqual("Deutsch", sent["reference"]["language"])
        self.assertEqual("English", result["output_language"])
        self.assertNotIn("korean_quote", result)
        for field in ("source_quote", "reference_quote"):
            invalid = {**payload, field: "rewritten text"}
            with patch.object(self.agent, "compare", return_value=json.dumps(invalid)):
                with self.assertRaisesRegex(ValueError, "exact source"):
                    await verify_translation(self.agent, source, reference)

    async def test_import_and_api_contract(self):
        content = pdf_bytes("1. Responsabilite\nLe texte exact.\n2. Controle\nUne autre obligation.")
        for jurisdiction, language in (("", "French"), ("Province", " ")):
            with self.assertRaises(ValueError):
                self.library.import_document(content, "document.pdf", "National regulation", language,
                                             jurisdiction=jurisdiction)
        with patch("regulatory_poc.ui.regulatory_api._service", return_value=self.service):
            record = await import_document(UploadFile(filename="document.pdf", file=BytesIO(content)),
                                           "National regulation", "Français", jurisdiction="Province libre")
            reference = await import_document(UploadFile(filename="reference.pdf", file=BytesIO(content)),
                                              "Translation reference", "Deutsch")
            report = create_table(CountryTableBody(
                baseline_id=self.baseline, country="Province libre",
                document_ids=[record["metadata"]["document_id"]],
                reference_id=reference["metadata"]["document_id"],
                reference_source_id=record["metadata"]["document_id"],
            ))
        self.assertEqual("Province libre", document_jurisdiction(record))
        self.assertFalse(record["complete"])
        self.assertEqual("auto", record["extraction_profile"])
        self.assertEqual(record["metadata"]["document_id"], report["inputs"]["reference_source_id"])
        with self.assertRaisesRegex(ValueError, "No requirements"):
            self.library.import_document(pdf_bytes("Unnumbered prose"), "plain.pdf",
                                         "National regulation", "Any language", jurisdiction="Anywhere")

    def test_reimport_never_silently_replaces_metadata_role_or_inventory(self):
        content = pdf_bytes("1. Exact name\nExact source text.")
        original = self.library.import_document(content, "saved.pdf", "National regulation",
                                                "Français", jurisdiction="Province")
        with patch("regulatory_poc.service.regulatory_library.extract_requirements") as extract:
            same = self.library.import_document(content, "saved.pdf", "National regulation",
                                                "Français", jurisdiction="Province")
            self.assertEqual(original, same)
            extract.assert_not_called()
            for role, language, jurisdiction, profile in (
                ("Translation reference", "Français", "Province", "auto"),
                ("National regulation", "Deutsch", "Province", "auto"),
                ("National regulation", "Français", "Another province", "auto"),
                ("National regulation", "Français", "Province", "numbered"),
            ):
                with self.assertRaisesRegex(ValueError, "already imported"):
                    self.library.import_document(content, "saved.pdf", role, language,
                                                 jurisdiction=jurisdiction, extraction_profile=profile)
            extract.assert_not_called()
        self.assertEqual(original, self.library.get(original["metadata"]["document_id"]))

    def test_legacy_same_file_role_migration_requires_explicit_review(self):
        content = pdf_bytes("Article 1 (Purpose) Exact text.")
        original = self.library.import_document(content, "legacy.pdf", "Korea", "Korean")
        with self.assertRaisesRegex(ValueError, "already imported"):
            self.library.import_document(content, "legacy.pdf", "National regulation",
                                         "Korean", jurisdiction="Korea")
        self.assertEqual(original, self.library.get(original["metadata"]["document_id"]))


class MultilingualExtractionTests(unittest.TestCase):
    def test_language_neutral_exact_spans_two_languages(self):
        for language, text in (
            ("Français", "1. Sûreté\nL’exploitant doit préserver  la sûreté.\n2. Contrôle\nVérifier les systèmes."),
            ("العربية", "١. السلامة\nيجب الحفاظ على  السلامة.\n٢. المراقبة\nيجب التحقق."),
        ):
            source = Source(text, (0,), (text,), ())
            with self.subTest(language=language), patch(
                "regulatory_poc.repo.requirement_extraction.read_source", return_value=source
            ):
                inventory = extract_requirements(b"source", replace(METADATA, language=language))
            self.assertEqual(2, len(inventory.requirements))
            self.assertFalse(inventory.complete)
            self.assertIn("provisional", " ".join(inventory.warnings))
            for item in inventory.requirements:
                self.assertIn(item.title, text)
                self.assertIn(item.description, text)
                self.assertEqual(language, item.language)

    def test_auto_detects_article_structure_without_language_or_country_selection(self):
        content = pdf_bytes("Article 1 (Purpose) Original description.\nArticle 2 (Scope) Exact scope.")
        inventory = extract_requirements(content, METADATA)
        self.assertEqual(["Article 1", "Article 2"], [item.identifier for item in inventory.requirements])
        self.assertFalse(inventory.complete)

    def test_auto_numbered_fallback_does_not_invent_inline_titles(self):
        inventory = extract_requirements(pdf_bytes("1. Exact inline clause.\n2. Another clause."), METADATA)
        self.assertEqual("1.", inventory.requirements[0].title)
        self.assertEqual("Exact inline clause.", inventory.requirements[0].description)
