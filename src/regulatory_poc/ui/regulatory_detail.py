from __future__ import annotations

import asyncio

import streamlit as st

from regulatory_poc.types.models import DocumentMetadata
from regulatory_poc.service.regulatory_coverage import STATUS_LABELS, national_rows
from regulatory_poc.ui.regulatory_translation import translation_result
from regulatory_poc.ui.regulatory_assessment import render_assessment


def source_location(source: dict) -> str:
    return (
        f"{source['source_name']} | {source['identifier']} | "
        f"PDF pages {source['page']}-{source['end_page']}"
    )


def source_detail(service, source: dict, key: str) -> None:
    st.caption(source_location(source))
    st.caption(f"Source language: {source['language']}")
    st.markdown("**Requirement name - original text**")
    st.code(source["title"], language=None, wrap_lines=True)
    st.markdown("**High-level description - original text**")
    st.code(source["description"], language=None, wrap_lines=True)
    with st.expander("Original document and provenance"):
        st.caption(f"SHA-256: {source['source_hash']}")
        if st.button("Prepare source PDF", key=f"prepare-{key}"):
            metadata = DocumentMetadata.from_dict(service.library.get(source["document_id"])["metadata"])
            content, _ = service.library.sources.download_version(metadata)
            st.download_button(
                "Download original; open at indicated PDF page", content,
                metadata.source_name, mime="application/pdf", key=f"source-{key}",
            )


def render_requirement_detail(service, report: dict, prefix: str) -> None:
    options = {row["baseline"]["key"]: row for row in report["rows"]}
    additional = {"national:" + row["source"]["key"]: row for row in national_rows(report)}
    key = st.selectbox(
        "Open requirement detail (Annex A view)", [*options, *additional],
        format_func=lambda value: (
            options[value]["baseline"]["identifier"] + ": " + options[value]["baseline"]["title"]
            if value in options else "National: " + additional[value]["source"]["identifier"]
            + " | " + additional[value]["source"]["source_name"]
        ), key=f"detail-{prefix}",
    )
    if key in additional:
        row = additional[key]
        st.caption(STATUS_LABELS[row["status"]])
        st.info(row["note"])
        st.markdown("#### National requirement without an identified reference counterpart")
        source_detail(service, row["source"], f"national-only-{prefix}-{key}")
        return
    row = options[key]
    st.caption(STATUS_LABELS.get(row["status"], row["status"]))
    with st.container(border=True):
        st.markdown(f"#### Reference-standard content ({row['baseline']['identifier']})")
        source_detail(service, row["baseline"], f"reference-{prefix}-{key}")
        paragraphs = row.get("paragraphs", [])
        with st.expander(f"Supporting paragraphs - exact text ({len(paragraphs)}); compared with the statement"):
            if not paragraphs:
                st.caption("Reference standard has no numbered supporting paragraphs for this requirement.")
            for paragraph in paragraphs:
                st.caption(f"Paragraph {paragraph['identifier']} | PDF pages {paragraph['page']}-{paragraph['end_page']}")
                st.code(paragraph["text"], language=None, wrap_lines=True)
    render_assessment(row)
    if not row["matches"]:
        st.info(row["note"] or "Not processed. No absence or equivalence conclusion is implied.")
    else:
        st.caption(
            f"{len(row['matches'])} proposed national correspondence(s). "
            "Each source below belongs to this same Reference requirement row."
        )
        if row["note"]:
            st.info(row["note"])
    for index, match in enumerate(row["matches"], start=1):
        source = match["source"]
        item_key = f"{prefix}-{key}-{index}"
        with st.container(border=True):
            st.markdown(f"#### National content {index} ({source['identifier']})")
            source_detail(service, source, f"national-{item_key}")
            if match["english_title"] or match["english_description"]:
                st.markdown("#### English translation")
                st.caption("Machine translation - not original source text; bilingual review required.")
                st.markdown("**Translated requirement name**")
                st.code(match["english_title"], language=None, wrap_lines=True)
                st.markdown("**Translated high-level description**")
                st.code(match["english_description"], language=None, wrap_lines=True)
            reference = source.get("english_reference")
            if reference:
                st.markdown("#### Supplied translation reference")
                st.caption("Reference text is not a certified translation. Check article and revision alignment.")
                source_detail(service, reference, f"reference-{item_key}")
                _translation_check(service, report, row, source, key, item_key)
        with st.expander(f"Proposed analysis for correspondence {index} - expert review required"):
            st.write(match["analysis"])
    if row["candidates"]:
        with st.expander("Analysed shortlist from exhaustive screening - not proof of correspondence"):
            for index, candidate in enumerate(row["candidates"]):
                st.caption(source_location(candidate))
                st.code(candidate["title"] + "\n\n" + candidate["description"], language=None, wrap_lines=True)
                reference = candidate.get("english_reference")
                if reference:
                    st.markdown("**Supplied translation reference (not certified)**")
                    st.caption(f"Reference language: {reference['language']}")
                    st.code(reference["title"] + "\n\n" + reference["description"],
                            language=None, wrap_lines=True)
                    _translation_check(service, report, row, candidate, key, f"candidate-{prefix}-{key}-{index}")


def _translation_check(service, report, row, source, requirement_key, widget_key) -> None:
    if st.button(
        "Verify original name and description against reference",
        key=f"verify-{widget_key}", disabled=service.translation_agent is None,
    ):
        asyncio.run(service.verify_article(report["report_id"], requirement_key, source["key"]))
        st.rerun()
    check = row["translation_checks"].get(source["key"])
    if check:
        st.markdown("**Translation check - separate from technical mapping review**")
        translation_result(check)
