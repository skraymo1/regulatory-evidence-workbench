from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from azure.core.credentials import AccessToken

from regulatory_poc.repo.agents import _SHARED_CREDENTIAL, CachedAsyncCredential, FoundryComparisonAgent
from regulatory_poc.repo.search import AzureAISearchRepository


class CloudAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_foundry_agent_uses_model_parameter(self) -> None:
        framework_agent = MagicMock()
        framework_agent.run = AsyncMock(return_value="Grounded response [A1][B1]")

        with (
            patch("agent_framework.foundry.FoundryChatClient") as client_type,
            patch("agent_framework.Agent", return_value=framework_agent),
        ):
            result = await FoundryComparisonAgent(
                "https://example.test/api/projects/project",
                "comparison-model",
            ).compare("prompt")

        self.assertEqual("Grounded response [A1][B1]", result)
        client_type.assert_called_once_with(
            project_endpoint="https://example.test/api/projects/project",
            model="comparison-model",
            credential=_SHARED_CREDENTIAL,
        )

    async def test_foundry_agent_retries_transient_timeout(self) -> None:
        from agent_framework.exceptions import ChatClientException

        class APITimeoutError(Exception):
            pass

        timeout = ChatClientException("Request timed out.")
        timeout.__cause__ = APITimeoutError("Request timed out.")
        framework_agent = MagicMock()
        framework_agent.run = AsyncMock(side_effect=[timeout, "Recovered"])

        with (
            patch("agent_framework.foundry.FoundryChatClient"),
            patch("agent_framework.Agent", return_value=framework_agent),
            patch("regulatory_poc.repo.agents.asyncio.sleep", new=AsyncMock()) as sleep,
        ):
            result = await FoundryComparisonAgent("https://example.test", "model").compare("prompt")

        self.assertEqual("Recovered", result)
        sleep.assert_awaited_once_with(5)

    async def test_foundry_agent_reports_model_failure_as_runtime_error(self) -> None:
        from agent_framework.exceptions import ChatClientException

        class APITimeoutError(Exception):
            pass

        timeout = ChatClientException("Request timed out.")
        timeout.__cause__ = APITimeoutError("Request timed out.")
        framework_agent = MagicMock()
        framework_agent.run = AsyncMock(side_effect=timeout)

        with (
            patch("agent_framework.foundry.FoundryChatClient"),
            patch("agent_framework.Agent", return_value=framework_agent),
            patch("regulatory_poc.repo.agents.asyncio.sleep", new=AsyncMock()),
            self.assertRaisesRegex(RuntimeError, "after 3 attempts: the Foundry model request timed out"),
        ):
            await FoundryComparisonAgent("https://example.test", "model").compare("prompt")
        self.assertEqual(3, framework_agent.run.await_count)

    async def test_cached_credential_reuses_token_until_near_expiry(self) -> None:
        inner = MagicMock()
        inner.get_token.side_effect = [
            AccessToken("first", int(time.time()) + 3600),
            AccessToken("second", int(time.time()) + 3600),
        ]
        credential = CachedAsyncCredential(lambda: inner)
        scope = "https://ai.azure.com/.default"

        self.assertEqual("first", (await credential.get_token(scope)).token)
        self.assertEqual("first", (await credential.get_token(scope)).token)
        self.assertEqual(1, inner.get_token.call_count)

        credential._tokens[(scope,)] = AccessToken("stale", int(time.time()) + 60)
        self.assertEqual("second", (await credential.get_token(scope)).token)
        self.assertEqual(2, inner.get_token.call_count)

    def test_cached_credential_works_across_event_loops(self) -> None:
        inner = MagicMock()
        inner.get_token.return_value = AccessToken("token", int(time.time()) + 3600)
        credential = CachedAsyncCredential(lambda: inner)

        for _ in range(2):
            self.assertEqual("token", asyncio.run(credential.get_token("scope")).token)
        self.assertEqual(1, inner.get_token.call_count)

    def test_search_escapes_document_filter_and_maps_result(self) -> None:
        search_client = MagicMock()
        search_client.search.return_value = [
            {
                "id": "chunk-1",
                "content": "defence in depth",
                "ordinal": 3,
                "document_id": "doc'one",
                "title": "National regulation",
                "authority": "NRC",
                "language": "English",
                "country": "US",
                "reactor_type": "",
                "standard": "",
                "project_number": "P-1",
                "source_name": "source.pdf",
                "@search.score": 2.5,
            }
        ]

        with patch("azure.search.documents.SearchClient", return_value=search_client):
            repository = AzureAISearchRepository(
                endpoint="https://search.example.test",
                index_name="regulatory-chunks",
                semantic_configuration="regulatory-semantic",
            )
            hits = repository.search("defence", top_k=4, document_id="doc'one")

        self.assertEqual("doc'one", hits[0].chunk.metadata.document_id)
        self.assertEqual(2.5, hits[0].score)
        search_client.search.assert_called_once_with(
            search_text="defence",
            filter="document_id eq 'doc''one'",
            query_type="semantic",
            semantic_configuration_name="regulatory-semantic",
            top=4,
        )

    def test_search_delete_surfaces_indexing_failure(self) -> None:
        search_client = MagicMock()
        search_client.search.return_value = [{"id": "chunk-1"}]
        failed = MagicMock(key="chunk-1", succeeded=False)
        search_client.delete_documents.return_value = [failed]

        with patch("azure.search.documents.SearchClient", return_value=search_client):
            repository = AzureAISearchRepository(
                endpoint="https://search.example.test",
                index_name="regulatory-chunks",
                semantic_configuration="regulatory-semantic",
            )
            with self.assertRaisesRegex(RuntimeError, "failed to delete"):
                repository.delete_document("document-1")
