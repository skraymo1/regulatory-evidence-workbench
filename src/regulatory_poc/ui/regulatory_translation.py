from __future__ import annotations

import asyncio

import streamlit as st


def translation_result(check: dict) -> None:
    st.write(check["assessment"])
    st.write(check["explanation"])
    # Historical saved checks retain their original schema and report identity.
    source_quote = check["source_quote"] if "source_quote" in check else check["korean_quote"]
    reference_quote = check["reference_quote"] if "reference_quote" in check else check["english_quote"]
    st.markdown("**Original-source quotation**")
    st.code(source_quote, language=None, wrap_lines=True)
    st.markdown("**Reference quotation**")
    st.code(reference_quote, language=None, wrap_lines=True)


def translation_scope(service, report: dict, prefix: str) -> None:
    inputs = report["inputs"]
    selected = inputs["selected_ids"]
    original_ids = [
        source["document_id"] for source in inputs["sources"]
        if source["document_id"] != inputs["reference_id"]
    ]
    paired_id = inputs.get("reference_source_id") or (original_ids[0] if len(original_ids) == 1 else "")
    with st.expander("Verify translations independently of reference mapping"):
        if not paired_id:
            st.warning("Reference pairing is ambiguous. Set up a comparison with an explicitly paired original document.")
            return
        sources = {
            item["key"]: item for item in report["inventories"][paired_id]
            if not selected or item["key"] in selected or item["identifier"] in selected
        }
        if not sources:
            st.info("No requirements from the paired original are included in this comparison scope.")
            return
        article_key = st.selectbox(
            "Original requirement to verify", list(sources),
            format_func=lambda key: sources[key]["identifier"] + ": " + sources[key]["title"],
            key=f"translation-article-{prefix}",
        )
        source = sources[article_key]
        st.caption(f"Original language: {source['language']}")
        st.code(source["title"] + "\n\n" + source["description"], language=None, wrap_lines=True)
        references = [
            item for item in report["inventories"][inputs["reference_id"]]
            if item["identifier"] == source["identifier"]
        ]
        originals = [
            item for item in report["inventories"][paired_id] if item["identifier"] == source["identifier"]
        ]
        aligned = len(references) == 1 and len(originals) == 1
        if aligned:
            reference = references[0]
            st.markdown("**Supplied reference - verify source revision**")
            st.caption(f"Reference language: {reference['language']}")
            st.code(reference["title"] + "\n\n" + reference["description"], language=None, wrap_lines=True)
        else:
            st.warning("No unique same-identifier pairing. Check document versions and requirement alignment.")
        if st.button("Verify this requirement's translation", key=f"translation-run-{prefix}",
                     disabled=not aligned or service.translation_agent is None):
            asyncio.run(service.verify_scope_article(report["report_id"], article_key))
            st.rerun()
        check = report.get("article_checks", {}).get(article_key)
        if check:
            translation_result(check)
        st.caption("Verification can run before any reference mapping. Both source versions require expert review.")
