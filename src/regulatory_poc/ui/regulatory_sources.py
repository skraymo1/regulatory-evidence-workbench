import streamlit as st

from regulatory_poc.service.regulatory_library import document_role, document_jurisdiction
from regulatory_poc.types.models import DocumentMetadata


def comparison_sources(service, settings) -> None:
    st.subheader("Comparison sources and requirement inventories")
    st.caption(
        "Import the supplied sources by role. Extraction is not expert approval. "
        "Review requirement boundaries and source text before interpreting results."
    )
    with st.expander("Upload comparison documents"):
        files = st.file_uploader(
            "Source documents", type=["pdf"], accept_multiple_files=True, key="regulatory-files"
        )
        role = st.selectbox("Comparison role", ["Reference baseline", "National regulation", "Translation reference"])
        language = st.text_input("Source language", help="Enter the language as named in the original document.")
        jurisdiction = st.text_input(
            "Jurisdiction", disabled=role == "Reference baseline",
            help="A country, regulator or jurisdiction label. Use the same label for documents compared collectively.",
        )
        revision = st.text_input(
            "Source revision", value="Rev. 1" if role == "Reference baseline" else "",
            disabled=role == "Reference baseline",
            help="Record the publication revision or amendment date from the original.",
        )
        st.caption("The current baseline extractor supports the configured reference profile.")
        st.caption(
            "Upload one role, jurisdiction and language per batch. Text-based PDFs only; "
            "automatic heading detection is provisional and must be checked against the original."
        )
        if st.button("Extract requirement inventory and index", disabled=(
            not files or not language.strip() or (role != "Reference baseline" and not jurisdiction.strip())
        )):
            for file in files:
                with st.spinner(f"Saving, extracting and indexing {file.name}"):
                    service.library.import_document(
                        file.getvalue(), file.name, role, language,
                        "Reference standard" if role == "Reference baseline" else "",
                        "Rev. 1" if role == "Reference baseline" else revision.strip(),
                        jurisdiction=jurisdiction.strip() if role != "Reference baseline" else "",
                    )
            st.session_state["source-import-complete"] = True
            st.rerun()
    if st.session_state.get("source-import-complete"):
        st.info("Import finished. Clear previously selected files before changing the role, language or jurisdiction.")
    documents = service.library.list_documents()
    if not documents:
        st.info("Import comparison sources to create comparison tables.")
        return
    for record in documents:
        metadata = DocumentMetadata.from_dict(record["metadata"])
        with st.expander(
            f"{document_role(record)} | {document_jurisdiction(record)} | {metadata.title} | "
            f"{len(record['requirements'])} extracted units"
        ):
            st.caption(f"Source language: {metadata.language}")
            st.caption(f"{record['indexing_status']} | unapproved source | SHA-256 {metadata.source_version}")
            for warning in record["warnings"]:
                st.warning(warning)
            if not record["complete"]:
                st.warning("Extraction completeness is not established. Review this inventory against the original.")
            if record["indexing_status"] != "indexed" and st.button(
                "Retry source indexing", key=f"inventory-retry-{metadata.document_id}"
            ):
                service.library.retry_index(metadata.document_id)
                st.rerun()
            st.dataframe([
                {"Identifier": item["identifier"], "Exact title": item["title"],
                 "Exact description": item["description"], "PDF page": item["page"]}
                for item in record["requirements"]
            ], hide_index=True)
            if st.button("Prepare original PDF download", key=f"inventory-source-{metadata.document_id}"):
                content, _ = service.library.sources.download_version(metadata)
                st.download_button("Download verified original", content, metadata.source_name,
                                   key=f"inventory-download-{metadata.document_id}")
