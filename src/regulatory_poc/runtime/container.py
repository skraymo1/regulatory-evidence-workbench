from __future__ import annotations

from regulatory_poc.config.settings import Settings
from regulatory_poc.repo.agents import ExtractiveComparisonAgent, FoundryComparisonAgent
from regulatory_poc.repo.conversations import JsonConversationRepository
from regulatory_poc.repo.embeddings import FoundryEmbeddingProvider
from regulatory_poc.repo.search import AzureAISearchRepository, JsonSearchRepository, SearchRepository
from regulatory_poc.repo.blob import BlobConversationRepository, BlobDocumentStore, BlobJsonStore
from regulatory_poc.repo.iq import FoundryIQRepository
from regulatory_poc.repo.prompt_agents import PersistedFoundryAgent
from regulatory_poc.service.chat import RegulatoryChatService
from regulatory_poc.service.comparison import RegulatoryComparisonService
from regulatory_poc.service.reports import SavedComparisonService
from regulatory_poc.service.ingestion import SourceSyncService
from regulatory_poc.repo.regulatory_store import LocalRecordStore, LocalSourceStore
from regulatory_poc.service.usage import UsageTracker
from regulatory_poc.repo.blob_browser import BlobStorageBrowser


def build_blob_browser(settings: Settings) -> BlobStorageBrowser | None:
    return BlobStorageBrowser(settings.blob_account_url) if settings.blob_account_url else None


def build_usage_tracker(settings: Settings) -> UsageTracker:
    state = build_state_store(settings) if settings.source_knowledge_base else LocalRecordStore(
        settings.index_path.parent / "regulatory"
    )
    return UsageTracker(state, settings.model_pricing)


def build_search_repository(settings: Settings) -> SearchRepository:
    usage = build_usage_tracker(settings)
    if settings.source_knowledge_base:
        return FoundryIQRepository(
            endpoint=settings.search_endpoint, index_name=settings.search_index,
            semantic_configuration=settings.semantic_configuration,
            embedding_provider=FoundryEmbeddingProvider(settings.embedding_endpoint, settings.embedding_model, usage),
            knowledge_base=settings.source_knowledge_base,
            document_store=BlobDocumentStore(
                settings.blob_account_url, settings.blob_container, build_state_store(settings)
            ),
        )
    if settings.search_mode == "azure":
        repository = AzureAISearchRepository(
            endpoint=settings.search_endpoint,
            index_name=settings.search_index,
            semantic_configuration=settings.semantic_configuration,
            embedding_provider=FoundryEmbeddingProvider(
                settings.embedding_endpoint, settings.embedding_model, usage
            ),
        )
    else:
        repository = JsonSearchRepository(settings.index_path)
    root = settings.index_path.parent / "regulatory"
    repository.document_store = LocalSourceStore(root / "sources", LocalRecordStore(root))
    return repository


def build_comparison_service(
    settings: Settings,
    search_repository: SearchRepository | None = None,
) -> RegulatoryComparisonService:
    settings.validate()
    repository = search_repository or build_search_repository(settings)
    agent = PersistedFoundryAgent(
        settings.foundry_project_endpoint, settings.agent_name, settings.agent_version,
        build_usage_tracker(settings), settings.model_deployment_name,
    ) if settings.source_knowledge_base else (
        FoundryComparisonAgent(
            project_endpoint=settings.foundry_project_endpoint,
            model_deployment_name=settings.model_deployment_name,
            usage=build_usage_tracker(settings),
        )
        if settings.agent_mode == "foundry"
        else ExtractiveComparisonAgent()
    )
    comparison = RegulatoryComparisonService(
        repository, agent, require_revision=bool(settings.source_knowledge_base)
    )
    if settings.source_knowledge_base:
        return SavedComparisonService(
            comparison, repository,
            BlobJsonStore(settings.blob_account_url, settings.report_container),
            build_report_search(settings), settings,
        )
    return comparison


def build_chat_service(
    settings: Settings,
    search_repository: SearchRepository | None = None,
    reports: bool = False,
) -> RegulatoryChatService:
    settings.validate()
    repository = build_report_search(settings) if reports else (
        search_repository or build_search_repository(settings)
    )
    agent = PersistedFoundryAgent(
        settings.foundry_project_endpoint, settings.chat_agent_name, settings.chat_agent_version,
        build_usage_tracker(settings), settings.model_deployment_name,
    ) if settings.source_knowledge_base else (
        FoundryComparisonAgent(
            project_endpoint=settings.foundry_project_endpoint,
            model_deployment_name=settings.model_deployment_name,
            usage=build_usage_tracker(settings),
        )
        if settings.agent_mode == "foundry"
        else ExtractiveComparisonAgent()
    )
    return RegulatoryChatService(
        repository,
        BlobConversationRepository(
            build_state_store(settings), "report-conversations" if reports else "conversations"
        ) if settings.source_knowledge_base else JsonConversationRepository(settings.conversation_path),
        agent,
    )


def build_state_store(settings):
    return BlobJsonStore(settings.blob_account_url, settings.state_container)


def build_report_search(settings):
    return FoundryIQRepository(
        endpoint=settings.search_endpoint, index_name=settings.report_index,
        semantic_configuration=settings.semantic_configuration,
        embedding_provider=FoundryEmbeddingProvider(
            settings.embedding_endpoint, settings.embedding_model, build_usage_tracker(settings),
        ),
        knowledge_base=settings.report_knowledge_base,
    )


def build_sync_service(settings, repository):
    return SourceSyncService(
        repository, settings.manifest_path,
        build_state_store(settings) if settings.source_knowledge_base else None,
    )
