from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st
from azure.core.exceptions import AzureError

SRC_ROOT = Path(__file__).resolve().parents[2]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from regulatory_poc.config.settings import Settings
from regulatory_poc.runtime.container import build_search_repository
from regulatory_poc.service.authorization import authorize_principal
from regulatory_poc.ui.regulatory import regulatory_panel


@st.cache_resource
def _resources() -> tuple[Settings, object]:
    settings = Settings.from_env()
    settings.validate()
    return settings, build_search_repository(settings)


def main() -> None:
    st.set_page_config(page_title="Regulatory Evidence Workbench", layout="wide")
    auth_settings = Settings.from_env()
    if auth_settings.allowed_oid and not authorize_principal(
        st.context.headers, auth_settings.allowed_oid, auth_settings.tenant_id
    ):
        st.error("Access denied. Sign in with the approved Entra account.")
        st.stop()
    st.title("Regulatory Evidence Workbench")
    try:
        settings, repository = _resources()
    except (RuntimeError, ValueError, OSError, AzureError) as exc:
        st.error(str(exc))
        st.stop()
    regulatory_panel(settings, repository)


main()
