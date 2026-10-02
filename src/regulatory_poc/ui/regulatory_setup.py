from __future__ import annotations

import streamlit as st

from regulatory_poc.service.regulatory_library import document_jurisdiction, document_role
from regulatory_poc.ui.regulatory_processing import (
    DEFAULT_PARALLEL_ROWS, PARALLEL_ROW_CHOICES, process_requirements,
)


def comparison_setup(service, baseline_id, documents, labels) -> str | None:
    national = [item for item in documents if document_role(item) == "National regulation"]
    jurisdictions = sorted({document_jurisdiction(item) for item in national})
    if not jurisdictions:
        st.info("Import national regulations in Comparison sources to create a comparison.")
        return None
    jurisdiction = st.selectbox("Jurisdiction to compare", jurisdictions, key="comparison-jurisdiction")
    available = [
        item["metadata"]["document_id"] for item in national
        if document_jurisdiction(item) == jurisdiction
    ]
    selected = st.multiselect(
        "National documents considered collectively", available, default=available,
        format_func=labels.get, key=f"comparison-documents-{jurisdiction}",
    )
    st.caption("All selected documents contribute to one table. Original language is recorded per source.")
    requirements = {
        item.key: item for document_id in selected for item in service.library.requirements(document_id)
    }
    scope_mode = st.radio(
        "Requirement scope", ["All extracted requirements", "Selected requirements only"],
        key=f"comparison-scope-{jurisdiction}", horizontal=True,
    )
    selected_keys = []
    if scope_mode == "Selected requirements only":
        selected_keys = st.multiselect(
            "Requirements in the agreed comparison scope", list(requirements),
            format_func=lambda key: (
                f"{requirements[key].source_name} | {requirements[key].identifier}: {requirements[key].title}"
            ), key=f"comparison-requirements-{jurisdiction}",
        )
    st.caption("Select any reviewer-designated scope explicitly. Highlights do not automatically define or approve scope.")
    references = [
        item["metadata"]["document_id"] for item in documents
        if document_role(item) == "Translation reference"
        and document_jurisdiction(item) == jurisdiction
    ]
    reference_id = st.selectbox(
        "Translation reference (optional)", ["", *references],
        format_func=lambda key: labels.get(key, "No reference"),
        key=f"comparison-reference-{jurisdiction}",
    )
    reference_source_id = ""
    if reference_id and selected:
        reference_source_id = st.selectbox(
            "Original document paired with this reference", selected, format_func=labels.get,
            key=f"comparison-reference-source-{jurisdiction}",
        )
        st.caption(
            "The reference applies only to this original. Verify revisions and matching identifiers; "
            "matching numbers alone are not evidence of translation equivalence."
        )
    if service.agent is None:
        st.warning("Offline mode: Create retrieves candidates only, not AI comparisons. Use run-foundry.ps1 for live analysis.")
    else:
        st.caption(
            "Create starts live AI analysis of pending rows using the selected documents. "
            "This incurs model usage. Each row is saved; completed rows are not regenerated. "
            "A batch starts no new rows after 15 minutes; resume to continue. Processing rows in "
            "parallel is faster but does not change cost."
        )
    count = st.selectbox(
        "Requirements to process on create / resume", ["All pending", 1, 5, 10],
        key="create-process-count",
    )
    parallel = st.selectbox(
        "Rows processed in parallel", PARALLEL_ROW_CHOICES,
        index=PARALLEL_ROW_CHOICES.index(DEFAULT_PARALLEL_ROWS), key="create-parallel-rows",
    )
    acknowledged = st.checkbox(
        "I have checked the document selection and extraction warnings; results remain unreviewed.",
        key="comparison-scope-acknowledged",
    )
    if st.button("Create / resume comparison table", key="create-comparison", disabled=(
        not acknowledged or not selected or
        (scope_mode == "Selected requirements only" and not selected_keys)
    )):
        report = service.create(
            baseline_id, jurisdiction, selected, selected_keys, reference_id,
            reference_source_id=reference_source_id,
        )
        st.session_state["configured-comparison"] = report["report_id"]
        process_requirements(service, report, None if count == "All pending" else count, parallel)
    report_id = st.session_state.get("configured-comparison")
    if not report_id:
        return None
    report = service.get(report_id)
    inputs = report["inputs"]
    source_ids = sorted(
        item["document_id"] for item in inputs["sources"] if item["document_id"] != inputs["reference_id"]
    )
    if (inputs["baseline"]["document_id"] != baseline_id or inputs["country"] != jurisdiction
            or source_ids != sorted(selected) or inputs["selected_ids"] != sorted(selected_keys)
            or inputs["reference_id"] != reference_id
            or inputs.get("reference_source_id", "") != reference_source_id):
        st.warning("Selection changed. Create/resume the table for this scope before processing.")
        return None
    return report_id
