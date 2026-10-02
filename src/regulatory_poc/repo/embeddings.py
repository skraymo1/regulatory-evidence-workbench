from __future__ import annotations

from typing import Protocol


class EmbeddingProvider(Protocol):
    def embed(self, text: str) -> list[float]: ...


class FoundryEmbeddingProvider:
    def __init__(self, endpoint: str, model: str) -> None:
        self._openai = None
        if ".openai.azure.com" in endpoint:
            from openai import AzureOpenAI
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider

            self._openai = AzureOpenAI(
                azure_endpoint=endpoint, api_version="2024-02-01",
                azure_ad_token_provider=get_bearer_token_provider(
                    DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
                ),
            )
            self._model = model
            return
        try:
            from azure.ai.inference import EmbeddingsClient
            from azure.identity import DefaultAzureCredential
        except ImportError as exc:
            raise RuntimeError(
                "Embedding generation requires azure-ai-inference and azure-identity."
            ) from exc
        self._client = EmbeddingsClient(
            endpoint=endpoint,
            credential=DefaultAzureCredential(),
            model=model,
        )

    def embed(self, text: str) -> list[float]:
        response = (
            self._openai.embeddings.create(input=[text], model=self._model)
            if self._openai else self._client.embed(input=[text])
        )
        if not response.data:
            raise RuntimeError("The embedding deployment returned no vector.")
        return [float(value) for value in response.data[0].embedding]
