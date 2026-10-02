from __future__ import annotations

import asyncio
import json
from pathlib import Path


def fidelity_rules() -> dict:
    return json.loads(
        (Path(__file__).parents[1] / "config" / "fidelity-rules.json").read_text("utf-8")
    )


class PersistedFoundryAgent:
    def __init__(self, endpoint: str, name: str, version: str) -> None:
        self.endpoint, self.name, self.version = endpoint, name, version

    async def compare(self, prompt: str) -> str:
        return await asyncio.to_thread(self._run, prompt)

    async def answer(self, prompt: str) -> str:
        return await asyncio.to_thread(self._run, prompt)

    def _run(self, prompt: str) -> str:
        from azure.ai.projects import AIProjectClient
        from azure.identity import DefaultAzureCredential

        with DefaultAzureCredential() as credential:
            with AIProjectClient(endpoint=self.endpoint, credential=credential) as project:
                with project.get_openai_client() as client:
                    response = client.responses.create(
                        input=prompt,
                        extra_body={"agent_reference": {
                            "name": self.name, "version": self.version, "type": "agent_reference",
                        }},
                    )
                    if response.status != "completed":
                        raise RuntimeError("Foundry agent did not complete; retry the request.")
                    return response.output_text
