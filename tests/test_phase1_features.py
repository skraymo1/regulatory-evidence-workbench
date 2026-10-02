from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from regulatory_poc.repo.agents import ExtractiveComparisonAgent
from regulatory_poc.repo.conversations import JsonConversationRepository
from regulatory_poc.repo.search import JsonSearchRepository
from regulatory_poc.service.chat import RegulatoryChatService
from regulatory_poc.service.comparison import RegulatoryComparisonService
from regulatory_poc.service.ingestion import SourceSyncService
from regulatory_poc.service.samples import SampleCatalogService
from regulatory_poc.types.models import (
    ChatRequest,
    ComparisonRequest,
    DocumentChunk,
    DocumentMetadata,
    SearchFilters,
    SourceDocument,
)


class FakeConnector:
    def __init__(
        self, documents: list[SourceDocument], name: str = "blob:approved"
    ) -> None:
        self.documents = documents
        self.name = name

    def list_documents(self) -> list[SourceDocument]:
        return self.documents


def _chunk(
    document_id: str,
    text: str,
    *,
    country: str = "",
    reactor_type: str = "",
    standard: str = "",
    language: str = "unknown",
) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=f"{document_id}-0",
        text=text,
        ordinal=0,
        metadata=DocumentMetadata(
            document_id=document_id,
            title=document_id,
            authority="reference" if standard else "National regulator",
            country=country,
            reactor_type=reactor_type,
            standard=standard,
            language=language,
            source_name=f"{document_id}.txt",
        ),
    )


class Phase1FeatureTests(unittest.IsolatedAsyncioTestCase):
    def test_sync_is_idempotent_and_replaces_changed_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = JsonSearchRepository(root / "index.json")
            service = SourceSyncService(repository, root / "sync.json")
            connector = FakeConnector(
                [
                    SourceDocument(
                        source_id="regulation.txt",
                        source_kind="blob",
                        source_uri="https://storage/regulation.txt",
                        source_name="regulation.txt",
                        version="v1",
                        content=b"Initial reactor safety requirement",
                        metadata={"country": "France"},
                    )
                ]
            )

            first = service.sync(connector)
            second = service.sync(connector)
            connector.documents[0] = SourceDocument(
                **{
                    **connector.documents[0].__dict__,
                    "version": "v2",
                    "content": b"Updated emergency reactor requirement",
                }
            )
            third = service.sync(connector)

            self.assertEqual((1, 0), (first.indexed, first.skipped))
            self.assertEqual((0, 1), (second.indexed, second.skipped))
            self.assertEqual(1, third.indexed)
            self.assertFalse(repository.search("Initial", 5))
            self.assertTrue(repository.search("Updated", 5))

    def test_same_source_id_in_two_repositories_does_not_collide(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = JsonSearchRepository(root / "index.json")
            service = SourceSyncService(repository, root / "sync.json")
            source = SourceDocument(
                source_id="regulation.txt",
                source_kind="blob",
                source_uri="https://storage/regulation.txt",
                source_name="regulation.txt",
                version="v1",
                content=b"reactor safety requirement",
            )

            service.sync(FakeConnector([source], "blob:container-a"))
            service.sync(FakeConnector([source], "blob:container-b"))

            self.assertEqual(2, len(repository.list_documents()))

    def test_cross_lingual_retrieval_and_domain_filters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = JsonSearchRepository(Path(directory) / "index.json")
            repository.upsert(
                [
                    _chunk(
                        "fr",
                        "Les exigences de sûreté du réacteur sont obligatoires.",
                        country="France",
                        reactor_type="PWR",
                    ),
                    _chunk(
                        "kr",
                        "원자로 안전 요건을 적용한다.",
                        country="Korea",
                        reactor_type="SMR",
                    ),
                ]
            )

            english_to_french = repository.search(
                "reactor safety requirements",
                5,
                filters=SearchFilters(countries=("France",)),
            )
            french_to_korean = repository.search(
                "exigences de sûreté",
                5,
                filters=SearchFilters(reactor_types=("SMR",)),
            )

            self.assertEqual("fr", english_to_french[0].chunk.metadata.document_id)
            self.assertEqual("kr", french_to_korean[0].chunk.metadata.document_id)

    def test_english_query_retrieves_russian_regulatory_terms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = JsonSearchRepository(Path(directory) / "index.json")
            repository.upsert(
                [_chunk("ru", "Требования безопасности ядерного реактора обязательны.")]
            )

            hits = repository.search("reactor safety requirements", 5)

            self.assertEqual("ru", hits[0].chunk.metadata.document_id)

    def test_sample_catalog_verifies_and_indexes_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            content = b"reactor safety requirement"
            (root / "sample.txt").write_bytes(content)
            (root / "manifest.json").write_text(
                json.dumps(
                    {
                        "authority": "reference",
                        "documents": [
                            {
                                "file": "sample.txt",
                                "title": "Sample",
                                "language": "English",
                                "standard": "Reference standard",
                                "publication_page": "https://example.test/sample",
                                "sha256": hashlib.sha256(content).hexdigest(),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            repository = JsonSearchRepository(root / "index.json")
            service = SampleCatalogService(repository, root)

            first = service.seed()
            second = service.seed()

            self.assertEqual(first, second)
            self.assertEqual(1, len(repository.list_documents()))
            self.assertEqual("sample", first[0].source_kind)

    def test_sample_catalog_rejects_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample.txt").write_text("changed", encoding="utf-8")
            (root / "manifest.json").write_text(
                json.dumps(
                    {
                        "documents": [
                            {
                                "file": "sample.txt",
                                "title": "Sample",
                                "language": "English",
                                "standard": "Reference standard",
                                "publication_page": "https://example.test/sample",
                                "sha256": "0" * 64,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "SHA-256"):
                SampleCatalogService(
                    JsonSearchRepository(root / "index.json"), root
                ).seed()

    async def test_language_edition_comparison_requires_same_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = JsonSearchRepository(Path(directory) / "index.json")
            repository.upsert(
                [
                    _chunk(
                        "reference-en",
                        "reactor defence in depth safety requirement",
                        standard="Reference standard",
                        language="English",
                    ),
                    _chunk(
                        "reference-fr",
                        "réacteur défense en profondeur exigence de sûreté",
                        standard="Reference standard",
                        language="French",
                    ),
                ]
            )
            service = RegulatoryComparisonService(
                repository, ExtractiveComparisonAgent()
            )

            result = await service.compare(
                ComparisonRequest(
                    question="Compare defence in depth requirements",
                    document_a_id="reference-en",
                    document_b_id="reference-fr",
                )
            )

            self.assertTrue(result.sufficient_evidence)
            self.assertIn("[A1][B1]", result.answer)

    async def test_chat_persists_history_and_returns_citations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = JsonSearchRepository(root / "index.json")
            repository.upsert([_chunk("target", "reactor emergency safety requirement")])
            conversations = JsonConversationRepository(root / "chat.json")
            service = RegulatoryChatService(
                repository, conversations, ExtractiveComparisonAgent()
            )

            first = await service.answer(ChatRequest(question="reactor safety"))
            second = await service.answer(
                ChatRequest(
                    question="what about emergency requirements",
                    conversation_id=first.conversation_id,
                )
            )

            self.assertTrue(first.sufficient_evidence)
            self.assertTrue(second.sufficient_evidence)
            self.assertIn("[C1]", second.answer)
            self.assertEqual(4, len(conversations.get(first.conversation_id)))

    async def test_chat_rejects_uncited_substantive_paragraph(self) -> None:
        class PartiallyCitedAgent(ExtractiveComparisonAgent):
            async def answer(self, prompt: str) -> str:
                return (
                    "The requirement is explicit [C1].\n\n"
                    "A second unsupported substantive conclusion is also presented."
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = JsonSearchRepository(root / "index.json")
            repository.upsert([_chunk("target", "reactor safety requirement")])
            service = RegulatoryChatService(
                repository,
                JsonConversationRepository(root / "chat.json"),
                PartiallyCitedAgent(),
            )

            result = await service.answer(ChatRequest(question="reactor safety"))

            self.assertFalse(result.sufficient_evidence)
            self.assertIn("substantive paragraph", result.answer)


if __name__ == "__main__":
    unittest.main()
