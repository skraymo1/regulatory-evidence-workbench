from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    agent_mode: str
    search_mode: str
    index_path: Path
    foundry_project_endpoint: str
    model_deployment_name: str
    search_endpoint: str
    search_index: str
    semantic_configuration: str
    embedding_endpoint: str
    embedding_model: str
    manifest_path: Path
    conversation_path: Path
    blob_account_url: str
    blob_container: str
    blob_prefix: str
    sharepoint_site_id: str
    sharepoint_drive_id: str
    sharepoint_folder_path: str
    source_knowledge_base: str = ""
    report_index: str = "fidelity-reports"
    report_knowledge_base: str = ""
    report_container: str = "comparison-reports"
    state_container: str = "poc-state"
    agent_name: str = "security-requirements-analysis"
    agent_version: str = ""
    chat_agent_name: str = "fidelity-evidence-chat"
    chat_agent_version: str = ""
    model_version: str = ""
    allowed_oid: str = ""
    tenant_id: str = ""
    auth_enabled: bool = True

    @classmethod
    def from_env(cls) -> Settings:
        auth_enabled = os.getenv("POC_AUTH_ENABLED", "true").strip().lower()
        if auth_enabled not in ("true", "false"):
            raise ValueError("POC_AUTH_ENABLED must be true or false.")
        return cls(
            agent_mode=os.getenv("POC_AGENT_MODE", "offline").strip().lower(),
            search_mode=os.getenv("POC_SEARCH_MODE", "local").strip().lower(),
            index_path=Path(os.getenv("POC_INDEX_PATH", ".data/index.json")),
            foundry_project_endpoint=os.getenv("FOUNDRY_PROJECT_ENDPOINT", "").strip(),
            model_deployment_name=os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", "").strip(),
            search_endpoint=os.getenv("AZURE_AI_SEARCH_ENDPOINT", "").strip(),
            search_index=os.getenv("AZURE_AI_SEARCH_INDEX", "regulatory-chunks").strip(),
            semantic_configuration=os.getenv(
                "AZURE_AI_SEARCH_SEMANTIC_CONFIGURATION", "regulatory-semantic"
            ).strip(),
            embedding_endpoint=os.getenv("AZURE_AI_EMBEDDING_ENDPOINT", "").strip(),
            embedding_model=os.getenv(
                "AZURE_AI_EMBEDDING_MODEL", "text-embedding-3-large"
            ).strip(),
            manifest_path=Path(os.getenv("POC_SYNC_MANIFEST_PATH", ".data/sync.json")),
            conversation_path=Path(
                os.getenv("POC_CONVERSATION_PATH", ".data/conversations.json")
            ),
            blob_account_url=os.getenv("POC_BLOB_ACCOUNT_URL", "").strip(),
            blob_container=os.getenv("POC_BLOB_CONTAINER", "").strip(),
            blob_prefix=os.getenv("POC_BLOB_PREFIX", "").strip(),
            sharepoint_site_id=os.getenv("POC_SHAREPOINT_SITE_ID", "").strip(),
            sharepoint_drive_id=os.getenv("POC_SHAREPOINT_DRIVE_ID", "").strip(),
            sharepoint_folder_path=os.getenv("POC_SHAREPOINT_FOLDER_PATH", "").strip(),
            source_knowledge_base=os.getenv("POC_SOURCE_KB", ""),
            report_index=os.getenv("POC_REPORT_INDEX", "fidelity-reports"),
            report_knowledge_base=os.getenv("POC_REPORT_KB", ""),
            report_container=os.getenv("POC_REPORT_CONTAINER", "comparison-reports"),
            state_container=os.getenv("POC_STATE_CONTAINER", "poc-state"),
            agent_name=os.getenv("POC_AGENT_NAME", "security-requirements-analysis"),
            agent_version=os.getenv("POC_AGENT_VERSION", ""),
            chat_agent_name=os.getenv("POC_CHAT_AGENT_NAME", "fidelity-evidence-chat"),
            chat_agent_version=os.getenv("POC_CHAT_AGENT_VERSION", ""),
            model_version=os.getenv("POC_MODEL_VERSION", ""),
            allowed_oid=os.getenv("POC_ALLOWED_OID", ""),
            tenant_id=os.getenv("AZURE_TENANT_ID", ""),
            auth_enabled=auth_enabled == "true",
        )

    def validate(self) -> None:
        if os.getenv("CONTAINER_APP_NAME") and (
            (self.auth_enabled and not self.allowed_oid) or not self.tenant_id or self.search_mode != "azure"
            or not self.source_knowledge_base
        ):
            raise ValueError(
                "Hosted runtime requires Azure storage and tenant configuration, and an Entra "
                "account restriction unless POC_AUTH_ENABLED=false."
            )
        if self.source_knowledge_base and not all((
            self.blob_account_url, self.blob_container, self.report_knowledge_base,
            self.agent_version, self.chat_agent_version, self.model_version,
        )):
            raise ValueError("IQ mode requires Blob, report KB, and pinned agent/model versions.")
        if self.agent_mode not in {"offline", "foundry"}:
            raise ValueError("POC_AGENT_MODE must be 'offline' or 'foundry'.")
        if self.search_mode not in {"local", "azure"}:
            raise ValueError("POC_SEARCH_MODE must be 'local' or 'azure'.")
        if self.agent_mode == "foundry":
            missing = [
                name
                for name, value in {
                    "FOUNDRY_PROJECT_ENDPOINT": self.foundry_project_endpoint,
                    "AZURE_AI_MODEL_DEPLOYMENT_NAME": self.model_deployment_name,
                }.items()
                if not value
            ]
            if missing:
                raise ValueError(
                    f"Foundry mode requires {', '.join(missing)}. "
                    "Set them in the environment or switch POC_AGENT_MODE=offline."
                )
        if self.search_mode == "azure" and not self.search_endpoint:
            raise ValueError(
                "Azure search mode requires AZURE_AI_SEARCH_ENDPOINT. "
                "Set it or switch POC_SEARCH_MODE=local."
            )
        if self.search_mode == "azure" and (
            not self.embedding_endpoint or not self.embedding_model
        ):
            raise ValueError(
                "Azure search mode requires AZURE_AI_EMBEDDING_ENDPOINT and "
                "AZURE_AI_EMBEDDING_MODEL for cross-lingual hybrid retrieval."
            )
