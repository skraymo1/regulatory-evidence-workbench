from __future__ import annotations

import asyncio
from dataclasses import asdict

import streamlit as st
from azure.core.exceptions import AzureError

from regulatory_poc.runtime.container import build_chat_service
from regulatory_poc.types.models import ChatRequest, SearchFilters
from regulatory_poc.ui.evidence import render_citations


def evidence_chat(settings, repository, report_repository, documents, reports) -> None:
    scope = st.radio(
        "Evidence to search", ["Original regulations", "Saved comparison tables"],
        horizontal=True, key="evidence-scope",
    )
    report_mode = scope == "Saved comparison tables"
    prefix = "regulatory-report" if report_mode else "regulatory-source"
    if report_mode:
        st.warning(
            "Saved tables contain unreviewed generated analysis, not authoritative regulations. "
            "Answers here do not replace original-source evidence or qualified expert review."
        )
        options = {
            item["report_id"]: f"Reference standard vs {item['inputs']['country']} | {item['created_at']}"
            for item in reports if item["indexing_status"] == "indexed"
        }
        if len(options) < len(reports):
            st.info("Some tables are not search-ready. Save/retry their indexing in Saved comparison tables.")
    else:
        st.caption("Answers cite the imported comparison sources. Importing a source does not approve it.")
        options = {
            item["metadata"]["document_id"]:
                f"{item['role']} | {item['metadata']['title']} | {item['metadata']['language']}"
            for item in documents if item["indexing_status"] == "indexed"
        }
    if not options:
        st.info(
            "No search-ready comparison tables. Index a saved comparison table first."
            if report_mode else "Import and index documents in Comparison sources first."
        )
        return
    selected = st.multiselect(
        "Limit evidence (optional)", list(options), format_func=options.get,
        key=f"{prefix}-filter",
    )
    st.caption("Leaving this selection empty searches all listed evidence, not unrelated legacy documents.")
    messages = st.session_state.setdefault(f"{prefix}-messages", [])
    for message in messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            render_citations(message.get("citations", []))
    question = st.chat_input("Ask about this evidence", key=f"{prefix}-input")
    if not question:
        return
    with st.chat_message("user"):
        st.markdown(question)
    try:
        with st.chat_message("assistant"), st.spinner("Retrieving evidence..."):
            result = asyncio.run(build_chat_service(
                settings, report_repository if report_mode else repository,
                reports=report_mode and bool(settings.source_knowledge_base),
            ).answer(ChatRequest(
                question=question,
                conversation_id=st.session_state.get(f"{prefix}-conversation", ""),
                filters=SearchFilters(document_ids=tuple(selected or options)),
            )))
    except (RuntimeError, ValueError, OSError, AzureError) as exc:
        st.error(f"Could not answer from the selected evidence. Retry your question. {exc}")
        return
    st.session_state[f"{prefix}-conversation"] = result.conversation_id
    messages.extend([
        {"role": "user", "content": question},
        {"role": "assistant", "content": result.answer,
         "citations": [asdict(item) for item in result.citations]},
    ])
    st.rerun()
