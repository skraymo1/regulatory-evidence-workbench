from __future__ import annotations

import hashlib
import re
import unittest
from dataclasses import FrozenInstanceError
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from regulatory_poc.repo.requirement_extraction import extract_requirements, highlighted_article_ids
from regulatory_poc.repo.requirement_source import Source
from regulatory_poc.types.models import DocumentMetadata
from regulatory_poc.types.requirements import Requirement


SUPPORT = Path(__file__).resolve().parents[1] / "support_docs"
METADATA = DocumentMetadata("source-1", "Source", "Authority", language="en", source_name="source.pdf")


def pdf_bytes(*pages):
    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        })
        commands = ["BT /F1 12 Tf 50 740 Td 14 TL"]
        for line in text.split("\n"):
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            commands.append(f"({escaped}) Tj T*")
        commands.append("ET")
        stream = DecodedStreamObject()
        stream.set_data("\n".join(commands).encode("latin-1"))
        page[NameObject("/Contents")] = stream
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


class RequirementExtractionTests(unittest.TestCase):
    def test_literal_pdf_text_round_trip(self):
        content = pdf_bytes("Requirement 1: Exact  title\nA system shall retain  spacing.\n3.1. Explanation.")
        result = extract_requirements(content, METADATA, kind="reference")
        row = result.requirements[0]
        self.assertEqual("Exact  title", row.title)
        self.assertEqual("A system shall retain  spacing.", row.description)
        self.assertEqual((1, 1), (row.page, row.end_page))
        self.assertEqual(hashlib.sha256(content).hexdigest(), row.source_hash)
        self.assertEqual(row, Requirement.from_dict(row.to_dict()))
        enriched = {**row.to_dict(), "english_reference": {"identifier": "Article 1"}}
        self.assertEqual(row, Requirement.from_dict(enriched))
        self.assertNotIn("english_reference", Requirement.from_dict(enriched).to_dict())
        with self.assertRaises(FrozenInstanceError):
            row.title = "changed"
        with self.assertRaises(FrozenInstanceError):
            result.complete = True
        self.assertFalse(result.complete)
        self.assertIn("Missing reference-profile headings", " ".join(result.warnings))

    def test_multiline_toc_does_not_become_requirement(self):
        content = pdf_bytes(
            "Requirement 1: Responsibilities in\nplant design (3.1) ............ 10",
            "Requirement 1: Responsibilities in\nplant design\nThe plant shall be safe.\n3.1. Explanation.",
        )
        result = extract_requirements(content, METADATA, kind="reference")
        self.assertEqual(1, len(result.requirements))
        self.assertEqual("Responsibilities in\nplant design", result.requirements[0].title)
        self.assertEqual(2, result.requirements[0].page)

    def test_cross_page_statement_retains_literal_page_marker(self):
        content = pdf_bytes(
            "Requirement 31: Ageing management\nThe plant shall retain",
            "31\nits safety.\n5.51. Explanation.",
        )
        row = extract_requirements(content, METADATA, kind="reference").requirements[0]
        stream = "\n".join(page.extract_text() for page in PdfReader(BytesIO(content)).pages)
        self.assertIn(row.description, stream)
        self.assertTrue(row.description.endswith("its safety."))
        self.assertIn("\n31\n", row.description)
        self.assertEqual((1, 2), (row.page, row.end_page))

    def test_footnote_and_next_page_number_are_not_statement(self):
        content = pdf_bytes(
            "Requirement 12: Waste\nThe plant shall manage waste.\n10 A footnote.",
            "18\n4.20. Explanation.",
        )
        row = extract_requirements(content, METADATA, kind="reference").requirements[0]
        self.assertEqual("The plant shall manage waste.", row.description)
        self.assertEqual(1, row.end_page)

    def test_duplicate_body_headings_are_retained_and_keys_are_unique(self):
        content = pdf_bytes("Requirement 1: Name\nThe plant shall be safe.\n3.1. Explanation.\n" * 2)
        result = extract_requirements(content, METADATA, kind="reference")
        self.assertEqual(2, len(result.requirements))
        self.assertEqual(2, len({row.key for row in result.requirements}))
        self.assertIn("Duplicate reference-profile body", " ".join(result.warnings))
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            extract_requirements(content, METADATA, kind="reference", selected_ids=("Requirement 1",))

    def test_empty_title_cannot_certify_an_82_heading_inventory(self):
        text = "\n".join(f"Requirement {number}: \nThe plant shall be safe.\n3.1. Explanation."
                         for number in range(1, 83))
        result = extract_requirements(pdf_bytes(text), METADATA, kind="reference")
        self.assertEqual(82, len(result.requirements))
        self.assertFalse(result.complete)
        self.assertIn("missing literal title", " ".join(result.warnings))

    def test_selection_is_strict_and_applied_after_full_parsing(self):
        content = pdf_bytes("Requirement 1: Name\nThe plant shall be safe.\n3.1. Explanation.")
        for selected in [("missing",), ("Requirement 1", "Requirement 1"), ("",), ["Requirement 1"]]:
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                extract_requirements(content, METADATA, kind="reference", selected_ids=selected)
        result = extract_requirements(content, METADATA, kind="reference", selected_ids=("Requirement 1",))
        self.assertEqual(1, len(result.requirements))
        self.assertIn("Missing reference-profile headings", " ".join(result.warnings))
        self.assertFalse(result.complete)

    def test_korean_unicode_and_subarticles_are_literal(self):
        text = "제85조의2(정확한 제목)\n① 원문을 유지한다.\n제86조(다음)\n다음 내용.\n부칙\n제1조(시행일) 오늘."
        source = Source(text, (0,), (text,), ())
        with patch("regulatory_poc.repo.requirement_extraction.read_source", return_value=source):
            result = extract_requirements(b"source", METADATA, kind="korea")
        self.assertEqual(["Article 85-2", "Article 86", "Addendum Article 1"],
                         [row.identifier for row in result.requirements])
        self.assertEqual("정확한 제목", result.requirements[0].title)
        self.assertEqual("① 원문을 유지한다.", result.requirements[0].description)
        for row in result.requirements:
            self.assertIn(row.title, text)
            self.assertIn(row.description, text)

    def test_english_korean_cross_references_are_not_headings(self):
        content = pdf_bytes(
            "Article 1 (Purpose) The text cites Article 12 (5) and Article 21 (including Article 30 (3))."
            "Article 2 (Definitions) The original definitions.",
        )
        result = extract_requirements(content, METADATA, kind="korea")
        self.assertEqual(["Article 1", "Article 2"], [row.identifier for row in result.requirements])
        self.assertIn("Article 12 (5)", result.requirements[0].description)
        self.assertEqual((), highlighted_article_ids(content))

    def test_unsupported_empty_and_invalid_inputs_are_explicit(self):
        for content in [b"PK DOCX", b"not a PDF"]:
            with self.assertRaisesRegex(ValueError, "Unsupported"):
                extract_requirements(content, METADATA, kind="generic")
        with self.assertRaisesRegex(ValueError, "Unknown requirement kind"):
            extract_requirements(b"", METADATA, kind="other")
        with self.assertRaisesRegex(ValueError, "Cannot read PDF"):
            extract_requirements(b"%PDF-invalid", METADATA, kind="generic")
        result = extract_requirements(pdf_bytes(""), METADATA, kind="argentina")
        self.assertFalse(result.complete)
        self.assertFalse(result.requirements)
        self.assertIn("OCR", " ".join(result.warnings))
        result = extract_requirements(pdf_bytes("Some unnumbered prose"), METADATA, kind="generic")
        self.assertFalse(result.requirements)
        self.assertIn("unsupported", " ".join(result.warnings))


@unittest.skipUnless(SUPPORT.is_dir(), "Local support_docs PDFs not present")
class RealRequirementExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inventories = {}
        for path in SUPPORT.glob("*.pdf"):
            kind = ("uk" if path.name.startswith("UK")
                    else "korea" if "Korea" in path.name else "argentina")
            content = path.read_bytes()
            metadata = DocumentMetadata(path.stem, path.stem, "Authority", source_name=path.name)
            result = extract_requirements(content, metadata, kind="auto" if kind == "argentina" else kind)
            if result.complete:
                kind = "reference"
            pages = tuple(page.extract_text() or "" for page in PdfReader(BytesIO(content)).pages)
            cls.inventories[path.name] = (kind, content, result, pages)

    def test_every_quote_is_literal_and_page_addressable_in_every_source(self):
        self.assertEqual(15, len(self.inventories))
        for name, (_, content, result, pages) in self.inventories.items():
            with self.subTest(source=name):
                self.assertTrue(result.requirements, result.warnings)
                self.assertEqual(len(result.requirements), len({row.key for row in result.requirements}))
                for row in result.requirements:
                    stream = "\n".join(pages[row.page - 1:row.end_page])
                    self.assertTrue(row.title, row.identifier)
                    self.assertTrue(row.description, row.identifier)
                    self.assertIn(row.title, stream, row.identifier)
                    self.assertIn(row.description, stream, row.identifier)
                    self.assertEqual(hashlib.sha256(content).hexdigest(), row.source_hash)
                    self.assertEqual(name, row.source_name)
                    self.assertLessEqual(row.page, row.end_page)

    def test_reference_profile_all_82_exact_headings_once_and_only_high_level_statements(self):
        _, _, result, pages = next(
            inventory for inventory in self.inventories.values() if inventory[0] == "reference"
        )
        self.assertTrue(result.complete, result.warnings)
        self.assertEqual((), result.warnings)
        self.assertEqual([f"Requirement {n}" for n in range(1, 83)],
                         [row.identifier for row in result.requirements])
        stream = "\n".join(pages)
        for row in result.requirements:
            with self.subTest(requirement=row.identifier):
                self.assertIn(f"{row.identifier}: {row.title}", stream)
                self.assertNotRegex(row.description, r"(?m)^\d+\.\d+[A-Z]?\.")
                self.assertIn("shall", row.description)
                self.assertTrue(row.description.endswith("."))
                self.assertLess(len(row.description), 1600)
                self.assertGreaterEqual(row.page, 34)
                self.assertLessEqual(row.end_page, 84)
        first = result.requirements[0]
        self.assertEqual("Responsibilities in the management of safety in plant design", first.title)
        self.assertEqual(
            "An applicant for a licence to construct and/or operate a nuclear power plant \n"
            "shall be responsible for ensuring that the design submitted to the regulatory \n"
            "body meets all applicable safety requirements.", first.description,
        )
        self.assertEqual([(54, 55), (62, 63)],
                         [(r.page, r.end_page) for r in result.requirements if r.page != r.end_page])

    def test_national_inventories_are_nonempty_and_provisional(self):
        expected_argentina = {
            "Argentian part 5": 21, "Argentina criterios": 15, "Argentina part 10": 17,
            "Argentina part 2": 14, "Argentina part 3": 11, "Argentina part 4": 19,
            "Argentina part 6": 5, "Argentina part 7": 30, "Argentina part 8": 10,
            "Argentina part 9": 10, "Argentinal part 11": 5,
        }
        for name, (kind, _, result, _) in self.inventories.items():
            if kind == "reference":
                continue
            with self.subTest(source=name):
                self.assertFalse(result.complete)
                self.assertTrue(result.warnings)
                if kind == "argentina":
                    count = next(count for prefix, count in expected_argentina.items() if name.startswith(prefix))
                    self.assertEqual(count, len(result.requirements))
                    self.assertTrue(all(re.fullmatch(r"\d+\.", r.title) for r in result.requirements))
                elif kind == "uk":
                    self.assertEqual(317, len(result.requirements))
                    titles = {row.identifier: row.title for row in result.requirements}
                    self.assertEqual("Inherent safety", titles["EKP.1"])
                    self.assertEqual("Confirmation to operating personnel", titles["ESS.13"])
                else:
                    self.assertEqual(129 if name.startswith("Korean") else 101, len(result.requirements))
                    self.assertEqual("Article 1", result.requirements[0].identifier)

    def test_auto_preserves_known_national_structural_parsers(self):
        for name, (kind, content, original, _) in self.inventories.items():
            if kind == "reference":
                continue
            with self.subTest(source=name):
                detected = extract_requirements(content, original.document, kind="auto")
                self.assertEqual(original.requirements, detected.requirements)
                self.assertFalse(detected.complete)
                self.assertIn("provisional", " ".join(detected.warnings))

    def test_korean_explicit_selection_does_not_approve_highlights(self):
        _, content, inventory, _ = next(value for name, value in self.inventories.items()
                                        if name.startswith("Korean"))
        self.assertEqual((), highlighted_article_ids(content))
        result = extract_requirements(content, inventory.document, kind="korea",
                                      selected_ids=("Article 13", "Article 1"))
        self.assertEqual(["Article 1", "Article 13"], [r.identifier for r in result.requirements])
        self.assertFalse(result.complete)
        self.assertIn("No highlighted article is automatically approved", " ".join(result.warnings))


if __name__ == "__main__":
    unittest.main()
