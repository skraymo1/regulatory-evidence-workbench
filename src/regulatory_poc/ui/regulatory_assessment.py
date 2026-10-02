"""Detail-view panel for the three-column equivalence assessment of one reference row."""

from __future__ import annotations

import streamlit as st

from regulatory_poc.ui.regulatory_columns import (
    CITED, EQUIVALENCE, NATIONAL_ONLY, REFERENCE_ONLY, TERMINOLOGY, equivalence_label,
    national_only_text, reference_only_text, terminology_text,
)


def render_assessment(row: dict) -> None:
    if "topics" not in row:
        return
    with st.container(border=True):
        st.markdown("#### Proposed equivalence assessment - expert review required")
        st.markdown(f"**{EQUIVALENCE}** {equivalence_label(row)}")
        st.caption("Category calculated by code from the topic checklist: 0 differing topics = largely "
                   "equivalent; up to one third = small number of differences; more = more than a third "
                   "different. Partial topics count as different.")
        left, right = st.columns(2)
        left.markdown(f"**{REFERENCE_ONLY}**")
        left.text(reference_only_text(row) or "-")
        right.markdown(f"**{NATIONAL_ONLY}**")
        right.text(national_only_text(row) or "-")
        if row["topics"]:
            with st.expander("reference topic checklist (statement and supporting paragraphs)"):
                st.dataframe([
                    {"Topic": topic["id"], "Source": topic.get("source", ""), "reference topic": topic["topic"],
                     "Status": topic["status"], "National clauses": "; ".join(topic["keys"])}
                    for topic in row["topics"]
                ], hide_index=True)
        if row.get("terminology"):
            st.markdown(f"**{TERMINOLOGY}**")
            st.caption("Validate these terms with a bilingual technical reviewer before relying on the mapping.")
            st.text(terminology_text(row))
        if row.get("cited_instruments"):
            st.markdown(f"**{CITED}**")
            st.caption("Cross-referenced instruments: confirm they were supplied; missing documents cannot be matched.")
            st.text("\n".join(row["cited_instruments"]))
        screening = row.get("screening")
        if screening:
            st.caption(
                f"Screening: {screening['clauses']} national clauses in {screening['batches']} batch(es), "
                f"{len(screening['passes'])} pass(es) including any residual re-screen; "
                + ("complete." if screening["complete"] else "INCOMPLETE - coverage conclusions withheld.")
            )
