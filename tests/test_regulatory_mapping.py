from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from regulatory_poc.repo.requirement_paragraphs import supporting_paragraphs
from regulatory_poc.service import regulatory_mapping as mapping
from regulatory_poc.service.regulatory_mapping import equivalence, map_requirement


REFERENCE_PROFILE_PDF = os.environ.get("REFERENCE_PROFILE_PDF", "")
RULES = {"screen_instruction": "s", "residual_instruction": "r", "analysis_instruction": "a"}
TOPICS = [{"id": "T1", "topic": "Leak tightness", "source": "statement"},
          {"id": "T2", "topic": "Aircraft crash", "source": "6.20"}]


def clause(key, document="ar1", text="Texto exacto."):
    return {"key": key, "source_name": document + ".pdf", "identifier": key, "title": key,
            "description": text, "language": "Spanish", "document_id": document}


def row():
    return {"baseline": {"identifier": "Requirement 54", "title": "Containment system",
                         "description": "A containment shall be provided."},
            "paragraphs": [{"identifier": "6.20", "text": "6.20. Aircraft crash shall be considered."}]}


class ScriptedAgent:
    """Screen finds clause 'a' for T1; residual finds 'z' (another document) for T2."""

    def __init__(self, residual_key="z"):
        self.requests = []
        self.residual_key = residual_key

    async def compare(self, prompt):
        data = json.loads(prompt)
        self.requests.append(data)
        keys = {item["key"] for item in data.get("clauses", [])}
        if data["task"] == "screen":
            return json.dumps({"topics": TOPICS, "relevant": [
                {"key": "a", "topic_ids": ["T1"]}] if "a" in keys else []})
        if data["task"] == "residual_screen":
            return json.dumps({"relevant": [{"key": self.residual_key, "topic_ids": ["T2"]}]
                               if self.residual_key in keys else []})
        found = [item["key"] for item in data["candidates"]]
        return json.dumps({
            "matches": [{"key": key, "analysis": "x", "english_title": "", "english_description": ""}
                        for key in found],
            "topics": [{"id": "T1", "status": "explicit", "keys": ["a"]},
                       {"id": "T2", "status": "explicit" if "z" in found else "not_explicit",
                        "keys": ["z"] if "z" in found else []}],
            "national_only_topics": [], "terminology": [
                {"term": "confinamiento", "language": "Spanish", "literal_english": "confinement",
                 "reference_term": "containment", "concern": "system vs function"}],
            "cited_instruments": ["AR 10.10.1"], "outcome": "proposed", "note": "Review.",
        })


class MappingTests(unittest.IsolatedAsyncioTestCase):
    def test_equivalence_thresholds_count_partial_as_different(self):
        def topics(*statuses):
            return [{"status": status} for status in statuses]
        self.assertEqual("largely_equivalent", equivalence(topics("explicit", "explicit"), "proposed"))
        self.assertEqual("small_differences", equivalence(topics("explicit", "explicit", "partial"), "proposed"))
        self.assertEqual("major_differences", equivalence(topics("explicit", "partial", "not_explicit"), "proposed"))
        self.assertEqual("major_differences", equivalence(topics("not_explicit"), "no_comparable"))
        self.assertEqual("not_assessed", equivalence(topics("not_explicit"), "insufficient_evidence"))
        self.assertEqual("not_assessed", equivalence([], "proposed"))

    async def test_residual_pass_adds_dispersed_clause_from_another_document(self):
        agent = ScriptedAgent()
        clauses = [clause("a"), clause("b"), clause("z", "ar2")]
        result = await map_requirement(agent, RULES, row(), clauses, ["ar1.pdf", "ar2.pdf"], 5)
        self.assertEqual(["screen", "analyse", "residual_screen", "analyse"],
                         [item["task"] for item in agent.requests])
        self.assertEqual(["b", "z"], [item["key"] for item in agent.requests[2]["clauses"]])
        self.assertEqual(["T2"], agent.requests[2]["focus_topic_ids"])
        self.assertEqual(["6.20"], [p["identifier"] for p in agent.requests[0]["reference_requirement"]["supporting_paragraphs"]])
        self.assertEqual({"ar1", "ar2"}, {m["source"]["document_id"] for m in result["matches"]})
        self.assertEqual("largely_equivalent", result["equivalence"])
        self.assertEqual(["a", "z"], [item["key"] for item in result["candidates"]])
        self.assertEqual(["AR 10.10.1"], result["cited_instruments"])
        self.assertEqual("confinamiento", result["terminology"][0]["term"])
        self.assertTrue(result["screening"]["complete"])

    async def test_unresolved_topic_after_residual_keeps_difference(self):
        agent = ScriptedAgent(residual_key="missing")
        result = await map_requirement(agent, RULES, row(), [clause("a"), clause("b")], ["ar1.pdf"], 5)
        self.assertEqual(["screen", "analyse", "residual_screen"], [item["task"] for item in agent.requests])
        self.assertEqual("major_differences", result["equivalence"])
        self.assertEqual("not_explicit", result["topics"][1]["status"])

    async def test_large_collections_are_screened_in_batches_and_topics_stay_fixed(self):
        agent = ScriptedAgent()
        clauses = [clause("a")] + [clause(f"c{index}", text="x" * 500) for index in range(6)] + [clause("z")]
        with patch.object(mapping, "SCREEN_BATCH_CHARS", 1500):
            result = await map_requirement(agent, RULES, row(), clauses, ["ar1.pdf"], 5)
        screens = [item for item in agent.requests if item["task"] == "screen"]
        self.assertGreater(len(screens), 1)
        self.assertEqual(len(clauses), sum(len(item["clauses"]) for item in screens))
        self.assertEqual(TOPICS, screens[1]["topics"])
        self.assertEqual(len(screens), result["screening"]["batches"])

    async def test_one_corrective_retry_then_failure(self):
        replies = iter(["not json", json.dumps({"topics": TOPICS, "relevant": []})])
        class Flaky:
            requests = []
            async def compare(self, prompt):
                self.requests.append(json.loads(prompt))
                return next(replies, json.dumps({"relevant": []}))
        agent = Flaky()
        result = await map_requirement(agent, RULES, row(), [clause("a")], ["ar1.pdf"], 5)
        self.assertIn("not valid JSON", agent.requests[1]["previous_response_error"])
        self.assertEqual("no_comparable", result["outcome"])
        class Broken:
            async def compare(self, prompt):
                return "still not json"
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            await map_requirement(Broken(), RULES, row(), [clause("a")], ["ar1.pdf"], 5)

    async def test_screen_rejects_unknown_keys_and_topics(self):
        with self.assertRaisesRegex(ValueError, "unknown clause"):
            mapping.validate_screen(json.dumps({"topics": TOPICS, "relevant": [{"key": "q", "topic_ids": ["T1"]}]}),
                                    [clause("a")], None)
        with self.assertRaisesRegex(ValueError, "enumerated topics"):
            mapping.validate_screen(json.dumps({"relevant": [{"key": "a", "topic_ids": ["T9"]}]}),
                                    [clause("a")], TOPICS)
        with self.assertRaisesRegex(ValueError, "no extracted clauses"):
            await map_requirement(ScriptedAgent(), RULES, row(), [], [], 5)

    def test_rows_keep_only_clauses_that_a_topic_relies_on(self):
        payload = json.dumps({
            "matches": [{"key": key, "analysis": "x", "english_title": "", "english_description": ""}
                        for key in ("a", "shutdown", "z")],
            "topics": [{"id": "T1", "status": "explicit", "keys": ["a"]},
                       {"id": "T2", "status": "partial", "keys": ["z"]}],
            "national_only_topics": [{"topic": "Reactor shutdown", "keys": ["shutdown"]},
                                     {"topic": "Reporting", "keys": ["z", "shutdown"]},
                                     {"topic": "General", "keys": []}],
            "terminology": [], "outcome": "proposed", "note": "Review.",
        })
        result = mapping.validate_mapping(payload, [clause("a"), clause("shutdown"), clause("z")], TOPICS)
        self.assertEqual(["a", "z"], [item["source"]["key"] for item in result["matches"]])
        self.assertEqual([{"topic": "Reporting", "keys": ["z"]}, {"topic": "General", "keys": []}],
                         result["national_only_topics"])

    def test_topic_citing_unmatched_key_names_the_key(self):
        payload = json.dumps({
            "matches": [{"key": "a", "analysis": "x", "english_title": "", "english_description": ""}],
            "topics": [{"id": "T1", "status": "explicit", "keys": ["a"]},
                       {"id": "T2", "status": "partial", "keys": ["z"]}],
            "national_only_topics": [], "terminology": [], "outcome": "proposed", "note": "Review.",
        })
        with self.assertRaisesRegex(ValueError, "Topic T2 cites z, which is not in matches"):
            mapping.validate_mapping(payload, [clause("a"), clause("z")], TOPICS)
        result = mapping.validate_mapping(payload, [clause("a"), clause("z")], TOPICS, repair=True)
        self.assertEqual(["explicit", "not_explicit"], [t["status"] for t in result["topics"]])
        self.assertEqual([], result["topics"][1]["keys"])
        self.assertEqual("proposed", result["outcome"])
        self.assertIn("Automatic consistency repairs", result["note"])
        self.assertIn("topic T2 cited z", result["repairs"][0])

    def test_repair_recalculates_outcome_and_adds_missing_topics(self):
        payload = json.dumps({
            "matches": [], "topics": [{"id": "T1", "status": "Partially explicit", "keys": ["q"]}],
            "outcome": "proposed", "note": "",
        })
        result = mapping.validate_mapping(payload, [clause("a")], TOPICS, repair=True)
        self.assertEqual(["not_explicit", "not_explicit"], [t["status"] for t in result["topics"]])
        self.assertEqual("no_comparable", result["outcome"])
        self.assertTrue(any("T2 was not assessed" in item for item in result["repairs"]))
        self.assertEqual("partial", mapping._status("Partially explicit"))

    async def test_analysis_repairs_after_two_inconsistent_responses(self):
        bad = json.dumps({
            "matches": [{"key": "a", "analysis": "x", "english_title": "", "english_description": ""}],
            "topics": [{"id": "T1", "status": "explicit", "keys": ["a", "b"]},
                       {"id": "T2", "status": "not_explicit", "keys": []}],
            "national_only_topics": [], "terminology": [], "outcome": "proposed", "note": "Review.",
        })
        class Inconsistent(ScriptedAgent):
            async def compare(self, prompt):
                data = json.loads(prompt)
                if data["task"] == "analyse":
                    self.requests.append(data)
                    return bad
                return await super().compare(prompt)
        agent = Inconsistent(residual_key="missing")
        result = await map_requirement(agent, RULES, row(), [clause("a"), clause("b")], ["ar1.pdf"], 5)
        analyses = [item for item in agent.requests if item["task"] == "analyse"]
        self.assertEqual(bad, analyses[1]["previous_response"])
        self.assertIn("Topic T1 cites b", analyses[1]["previous_response_error"])
        self.assertEqual(["a"], result["topics"][0]["keys"])
        self.assertEqual("proposed", result["outcome"])
        self.assertIn("Automatic consistency repairs", result["note"])

    def test_prompts_require_direct_correspondence_not_shared_safety_goals(self):
        rules = json.loads((Path(mapping.__file__).parents[1] / "config" / "regulatory-rules.json")
                           .read_text(encoding="utf-8"))
        self.assertNotIn("favour recall", rules["screen_instruction"])
        self.assertIn("purpose or rationale", rules["screen_instruction"])
        self.assertIn("directly addresses the subject matter", rules["screen_instruction"])
        self.assertIn("directly addresses the topic's subject matter", rules["residual_instruction"])
        self.assertIn("cited by at least one explicit or partial topic", rules["analysis_instruction"])


@unittest.skipUnless(
    REFERENCE_PROFILE_PDF and Path(REFERENCE_PROFILE_PDF).is_file(),
    "Reference profile sample PDF not available",
)
class SupportingParagraphTests(unittest.TestCase):
    def test_contents_ranges_assign_full_requirement_paragraphs(self):
        paragraphs, warnings = supporting_paragraphs(Path(REFERENCE_PROFILE_PDF).read_bytes())
        self.assertEqual([], warnings)
        self.assertEqual(82, len(paragraphs))
        self.assertEqual([], paragraphs["Requirement 54"])
        identifiers = [item["identifier"] for item in paragraphs["Requirement 55"]]
        self.assertTrue(identifiers)
        self.assertTrue(all(item["text"].startswith(item["identifier"] + ".") for item in paragraphs["Requirement 55"]))
        self.assertTrue(all(item["page"] <= item["end_page"] for items in paragraphs.values() for item in items))
