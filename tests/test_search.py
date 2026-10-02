from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from regulatory_poc.repo.search import JsonSearchRepository
from regulatory_poc.types.models import DocumentChunk, DocumentMetadata


def _chunk(document_id: str, text: str, ordinal: int = 0) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=f"{document_id}-{ordinal}",
        text=text,
        ordinal=ordinal,
        metadata=DocumentMetadata(
            document_id=document_id,
            title=document_id,
            authority="test",
            source_name=f"{document_id}.txt",
        ),
    )


class SearchTests(unittest.TestCase):
    def test_persists_filters_and_ranks_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.json"
            repository = JsonSearchRepository(path)
            repository.upsert(
                [
                    _chunk("national", "defence in depth reactor design"),
                    _chunk("reference", "defence in depth safety requirement"),
                    _chunk("other", "unrelated financial report"),
                ]
            )

            reopened = JsonSearchRepository(path)
            hits = reopened.search("defence depth safety", top_k=5, document_id="reference")

            self.assertEqual(1, len(hits))
            self.assertEqual("reference", hits[0].chunk.metadata.document_id)
            self.assertEqual(
                ["national", "other", "reference"],
                [document.document_id for document in reopened.list_documents()],
            )


if __name__ == "__main__":
    unittest.main()
