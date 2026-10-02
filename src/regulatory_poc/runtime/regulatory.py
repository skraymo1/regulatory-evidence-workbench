from __future__ import annotations

import json
from pathlib import Path

from regulatory_poc.config.settings import Settings
from regulatory_poc.repo.agents import FoundryComparisonAgent
from regulatory_poc.repo.blob import BlobJsonStore
from regulatory_poc.repo.regulatory_store import LocalRecordStore, LocalSourceStore
from regulatory_poc.repo.search import JsonSearchRepository
from regulatory_poc.runtime.container import build_report_search, build_search_repository, build_state_store
from regulatory_poc.service.regulatory_comparison import RegulatoryTableService
from regulatory_poc.service.regulatory_library import RegulatoryLibrary
from regulatory_poc.service.translation import TRANSLATION_RULES


def build_regulatory_service(settings: Settings, repository=None) -> RegulatoryTableService:
    repository = repository or build_search_repository(settings)
    root = settings.index_path.parent / "regulatory"
    if settings.source_knowledge_base:
        state = build_state_store(settings)
        sources = repository.document_store
        reports = BlobJsonStore(settings.blob_account_url, settings.report_container)
        report_search = build_report_search(settings)
    else:
        state = LocalRecordStore(root)
        sources = LocalSourceStore(root / "sources")
        reports = state
        report_search = JsonSearchRepository(root / "report-index.json")
    rules = json.loads(
        (Path(__file__).parents[1] / "config" / "regulatory-rules.json").read_text("utf-8")
    )
    agent = FoundryComparisonAgent(
        settings.foundry_project_endpoint, settings.model_deployment_name,
        instructions=rules["instructions"],
    ) if settings.agent_mode == "foundry" else None
    return RegulatoryTableService(
        RegulatoryLibrary(state, sources, repository), reports, agent, rules,
        {"mode": settings.agent_mode, "model": settings.model_deployment_name,
         "version": settings.model_version, "agent": "application-defined-regulatory-mapping"},
        report_search,
        FoundryComparisonAgent(
            settings.foundry_project_endpoint, settings.model_deployment_name,
            instructions=TRANSLATION_RULES,
        ) if settings.agent_mode == "foundry" else None,
    )
