"""Post-provision data-plane setup. No resource keys or client secrets are written."""
from __future__ import annotations

import json
import os
import argparse
import time
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition
from azure.identity import DefaultAzureCredential
from azure.core.exceptions import HttpResponseError
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    SearchIndex, SearchField, SimpleField, SearchableField, SearchFieldDataType,
    VectorSearch, HnswAlgorithmConfiguration, VectorSearchProfile,
    AzureOpenAIVectorizer, AzureOpenAIVectorizerParameters,
    SemanticSearch, SemanticConfiguration, SemanticPrioritizedFields, SemanticField,
    SearchIndexKnowledgeSource, SearchIndexKnowledgeSourceParameters, SearchIndexFieldReference,
    KnowledgeBase, KnowledgeSourceReference,
)
from azure.search.documents.knowledgebases.models import KnowledgeRetrievalMinimalReasoningEffort

from regulatory_poc.config.settings import Settings
from regulatory_poc.repo.prompt_agents import fidelity_rules
from regulatory_poc.types.models import DocumentMetadata


def create_index(client, name, *, embedding_endpoint, embedding_deployment):
    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True),
        SearchableField(name="content", type=SearchFieldDataType.String),
        SimpleField(name="ordinal", type=SearchFieldDataType.Int32, filterable=True),
    ]
    fields.extend([
        SearchableField(name=key, type=SearchFieldDataType.String, filterable=True)
        for key in DocumentMetadata.__dataclass_fields__
    ])
    fields.append(SearchField(
        name="content_vector", type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
        searchable=True, vector_search_dimensions=1536, vector_search_profile_name="multilingual",
    ))
    client.create_or_update_index(SearchIndex(
        name=name, fields=fields,
        vector_search=VectorSearch(
            algorithms=[HnswAlgorithmConfiguration(name="hnsw")],
            profiles=[VectorSearchProfile(
                name="multilingual", algorithm_configuration_name="hnsw", vectorizer_name="openai",
            )],
            vectorizers=[AzureOpenAIVectorizer(
                vectorizer_name="openai", parameters=AzureOpenAIVectorizerParameters(
                    resource_url=embedding_endpoint,
                    deployment_name=embedding_deployment,
                    model_name="text-embedding-3-small",
                ),
            )],
        ),
        semantic_search=SemanticSearch(
            default_configuration_name="regulatory-semantic",
            configurations=[SemanticConfiguration(
                name="regulatory-semantic", prioritized_fields=SemanticPrioritizedFields(
                    title_field=SemanticField(field_name="title"),
                    content_fields=[SemanticField(field_name="content")],
                ),
            )],
        ),
    ))
    client.create_or_update_knowledge_source(SearchIndexKnowledgeSource(
        name=f"{name}-source",
        description="Original sample document passages" if name.endswith("sources") else
        "Unreviewed generated comparison reports; never original authority",
        search_index_parameters=SearchIndexKnowledgeSourceParameters(
            search_index_name=name, semantic_configuration_name="regulatory-semantic",
            source_data_fields=[SearchIndexFieldReference(name=field.name)
                                for field in fields if field.name != "content_vector"],
        ),
    ))
    client.create_or_update_knowledge_base(KnowledgeBase(
        name=f"{name}-kb",
        knowledge_sources=[KnowledgeSourceReference(name=f"{name}-source")],
        retrieval_reasoning_effort=KnowledgeRetrievalMinimalReasoningEffort(),
        output_mode="extractiveData",
    ))


def wait_for_access(index_client, project, seconds=300):
    deadline = time.monotonic() + seconds
    while True:
        try:
            list(index_client.list_index_names())
            next(iter(project.agents.list()), None)
            return
        except HttpResponseError as error:
            if error.status_code != 403 or time.monotonic() >= deadline:
                raise
            print("Waiting for Search/Foundry role propagation (read-only access probe).", flush=True)
            time.sleep(10)


def main(output_path=None, *, wait_for_rbac=False):
    settings = Settings.from_env()
    settings.validate()
    required = {
        "FOUNDRY_PROJECT_ENDPOINT": settings.foundry_project_endpoint,
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": settings.model_deployment_name,
        "AZURE_AI_SEARCH_ENDPOINT": settings.search_endpoint,
        "AZURE_AI_EMBEDDING_ENDPOINT": settings.embedding_endpoint,
        "AZURE_AI_EMBEDDING_MODEL": os.getenv("AZURE_AI_EMBEDDING_MODEL", "").strip(),
        "POC_MODEL_VERSION": settings.model_version,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError(f"Bootstrap requires {', '.join(missing)}.")
    credential = DefaultAzureCredential()
    index_client = SearchIndexClient(
        settings.search_endpoint, credential,
        api_version="2026-08-01-preview",
    )
    project = AIProjectClient(
        endpoint=settings.foundry_project_endpoint, credential=credential
    )
    if wait_for_rbac:
        wait_for_access(index_client, project)
    for name in ("fidelity-sources", "fidelity-reports"):
        create_index(
            index_client, name, embedding_endpoint=settings.embedding_endpoint,
            embedding_deployment=settings.embedding_model,
        )
    rules = fidelity_rules()
    created = {}
    for name, instructions, key in [
        (settings.agent_name, rules["instructions"], "POC_AGENT_VERSION"),
        (settings.chat_agent_name, rules["chat_instructions"], "POC_CHAT_AGENT_VERSION"),
    ]:
        agent = project.agents.create_version(
            agent_name=name, definition=PromptAgentDefinition(
                model=settings.model_deployment_name, instructions=instructions,
            ), metadata={"rules_version": rules["version"], "status": "provisional-poc"},
        )
        created[key] = str(agent.version)
        print(json.dumps({"event": "agent_version_created", "name": name, "version": agent.version}))
    output = Path(output_path or ".azure/agent-versions.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(created, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="Environment-specific agent-version output path.")
    parser.add_argument("--wait-for-rbac", action="store_true")
    args = parser.parse_args()
    main(args.output, wait_for_rbac=args.wait_for_rbac)
