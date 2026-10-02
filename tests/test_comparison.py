from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from regulatory_poc.repo.search import JsonSearchRepository
from regulatory_poc.service.comparison import RegulatoryComparisonService
from regulatory_poc.types.models import ComparisonRequest, DocumentChunk, DocumentMetadata


class RecordingAgent:
    def __init__(
        self, answer: str = "Both editions communicate the requirement [A1][B1]."
    ) -> None:
        self.prompt = ""
        self.answer = answer

    async def compare(self, prompt: str) -> str:
        self.prompt = prompt
        return self.answer


def _chunk(
    document_id: str,
    language: str,
    text: str,
    *,
    publication: str = "Reference standard",
) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=f"{document_id}-0",
        text=text,
        ordinal=0,
        metadata=DocumentMetadata(
            document_id=document_id,
            title=document_id,
            authority="reference",
            language=language,
            standard=publication,
            source_name=f"{document_id}.txt",
        ),
    )


class ComparisonTests(unittest.IsolatedAsyncioTestCase):
    async def test_builds_grounded_prompt_and_citations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = JsonSearchRepository(Path(directory) / "index.json")
            repository.upsert(
                [
                    _chunk("english", "English", "defence in depth reactor requirement"),
                    _chunk("french", "French", "defence in depth safety requirement"),
                ]
            )
            agent = RecordingAgent()
            service = RegulatoryComparisonService(repository, agent)

            result = await service.compare(
                ComparisonRequest(
                    question="Compare defence in depth requirements",
                    document_a_id="english",
                    document_b_id="french",
                )
            )

            self.assertTrue(result.sufficient_evidence)
            self.assertEqual(["A1", "B1"], [value.citation_id for value in result.citations])
            self.assertIn("[A1]", agent.prompt)
            self.assertIn("[B1]", agent.prompt)
            self.assertIn("equivalent regulatory meaning", agent.prompt)
            self.assertIn("insufficient evidence", agent.prompt)

    async def test_fails_closed_when_one_document_has_no_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = JsonSearchRepository(Path(directory) / "index.json")
            repository.upsert(
                [
                    _chunk("english", "English", "defence in depth reactor requirement"),
                    _chunk("french", "French", "probabilistic assessment"),
                ]
            )
            service = RegulatoryComparisonService(repository, RecordingAgent())

            result = await service.compare(
                ComparisonRequest(
                    question="defence depth",
                    document_a_id="english",
                    document_b_id="french",
                )
            )

            self.assertFalse(result.sufficient_evidence)
            self.assertIn("Insufficient evidence", result.answer)

    async def test_rejects_answer_without_valid_citations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = JsonSearchRepository(Path(directory) / "index.json")
            repository.upsert(
                [
                    _chunk("english", "English", "defence in depth reactor requirement"),
                    _chunk("french", "French", "defence in depth safety requirement"),
                ]
            )
            service = RegulatoryComparisonService(
                repository,
                RecordingAgent("Unsupported uncited conclusion."),
            )

            result = await service.compare(
                ComparisonRequest(
                    question="Compare defence in depth",
                    document_a_id="english",
                    document_b_id="french",
                )
            )

            self.assertFalse(result.sufficient_evidence)
            self.assertIn("no evidence labels", result.answer)

    async def test_preserves_explicit_insufficient_evidence_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = JsonSearchRepository(Path(directory) / "index.json")
            repository.upsert(
                [
                    _chunk("english", "English", "defence in depth reactor requirement"),
                    _chunk("french", "French", "defence in depth safety requirement"),
                ]
            )
            service = RegulatoryComparisonService(
                repository,
                RecordingAgent("Insufficient evidence to establish convergence."),
            )

            result = await service.compare(
                ComparisonRequest(
                    question="Compare defence in depth",
                    document_a_id="english",
                    document_b_id="french",
                )
            )

            self.assertFalse(result.sufficient_evidence)
            self.assertEqual("Insufficient evidence to establish convergence.", result.answer)

    async def test_requires_same_publication_in_different_languages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = JsonSearchRepository(Path(directory) / "index.json")
            repository.upsert(
                [
                    _chunk("english", "English", "safety requirement"),
                    _chunk("same-language", "English", "safety requirement"),
                    _chunk(
                        "different-publication",
                        "French",
                        "exigence de sûreté",
                        publication="Alternative standard",
                    ),
                ]
            )
            service = RegulatoryComparisonService(repository, RecordingAgent())

            with self.assertRaisesRegex(ValueError, "different languages"):
                await service.compare(
                    ComparisonRequest(
                        question="safety requirement",
                        document_a_id="english",
                        document_b_id="same-language",
                    )
                )
            with self.assertRaisesRegex(ValueError, "same publication identifier"):
                await service.compare(
                    ComparisonRequest(
                        question="safety requirement",
                        document_a_id="english",
                        document_b_id="different-publication",
                    )
                )


if __name__ == "__main__":
    unittest.main()
