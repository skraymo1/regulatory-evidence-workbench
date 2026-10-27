import streamlit as st
from azure.core.exceptions import AzureError

from regulatory_poc.service.regulatory_library import document_role, document_jurisdiction
from regulatory_poc.runtime.container import build_blob_browser
from regulatory_poc.types.models import DocumentMetadata
from regulatory_poc.ui.document_admin import blob_file_picker


def _existing_blob_source(service, settings) -> None:
    browser = build_blob_browser(settings) if settings is not None else None
    if browser is None:
        return
    with st.expander("Use an existing Blob document", expanded=True):
        st.caption(
            "Use a PDF already in storage; no re-upload is needed and the original Blob is not changed. "
            "Choose its comparison metadata, then extract the inventory and index it. "
            "Browsing makes no model calls; indexing embeddings may incur token costs."
        )
        known = {}
        for item in service.library.state.list("documents/"):
            metadata = item["metadata"]
            location = browser.source_location(metadata.get("source_uri", ""))
            if location and metadata.get("source_name", "").lower().endswith(".pdf"):
                known[metadata["document_id"]] = (metadata, location)
        mode = st.radio(
            "Find a stored PDF", ["Uploaded documents", "Browse all containers and folders"],
            key="source-blob-mode",
        ) if known else "Browse all containers and folders"
        selected = None
        if mode == "Uploaded documents":
            document_id = st.selectbox(
                "Existing uploaded PDF", list(known),
                format_func=lambda key: f"{known[key][0]['title']} ({key})", key="source-blob-document",
            )
            selected = known[document_id][1]
        else:
            selected = blob_file_picker(browser, key_prefix="source-blob", pdf_only=True)
        if selected is None:
            return
        container, name = selected
        st.caption(f"Selected source: {container}/{name}")
        with st.form("source-blob-import"):
            role = st.selectbox(
                "Comparison role", ["Reference baseline", "National regulation", "Translation reference"],
                key="source-blob-role",
            )
            language = st.text_input("Source language", key="source-blob-language")
            jurisdiction = st.text_input(
                "Jurisdiction", key="source-blob-jurisdiction",
                help="Required for national regulations. Leave empty for the reference baseline.",
            )
            revision = st.text_input(
                "Source revision", key="source-blob-revision",
                help="The reference baseline uses Reference standard Rev. 1; other revisions use this label.",
            )
            st.caption("Text-based PDFs up to 200 MiB. The baseline uses the configured reference profile.")
            submitted = st.form_submit_button("Use selected Blob PDF: extract and index")
        if submitted:
            if not language.strip() or (role != "Reference baseline" and not jurisdiction.strip()):
                st.error("Supply the source language and, for national/translation sources, the jurisdiction.")
                return
            try:
                with st.spinner("Reading the saved PDF, extracting requirements and indexing"):
                    service.library.import_blob_document(
                        browser, container, name, role, language,
                        "Reference standard" if role == "Reference baseline" else "",
                        "Rev. 1" if role == "Reference baseline" else revision.strip(),
                        jurisdiction="" if role == "Reference baseline" else jurisdiction.strip(),
                    )
            except (RuntimeError, ValueError, OSError, AzureError) as exc:
                st.error(f"{container}/{name}: {exc}")
            else:
                st.session_state["source-blob-import-complete"] = f"{container}/{name}"
                st.rerun()


def comparison_sources(service, settings) -> None:
    st.subheader("Comparison sources and requirement inventories")
    st.caption(
        "Import the supplied sources by role. Extraction is not expert approval. "
        "Review requirement boundaries and source text before interpreting results."
    )
    _existing_blob_source(service, settings)
    if st.session_state.get("source-blob-import-complete"):
        st.success(
            f"Stored PDF imported for comparison: {st.session_state['source-blob-import-complete']}. "
            "It is listed below and available in Comparisons."
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
            failed = False
            for file in files:
                try:
                    with st.spinner(f"Saving, extracting and indexing {file.name}"):
                        service.library.import_document(
                            file.getvalue(), file.name, role, language,
                            "Reference standard" if role == "Reference baseline" else "",
                            "Rev. 1" if role == "Reference baseline" else revision.strip(),
                            jurisdiction=jurisdiction.strip() if role != "Reference baseline" else "",
                        )
                except (RuntimeError, ValueError, OSError, AzureError) as exc:
                    failed = True
                    st.error(f"{file.name}: {exc}")
            st.session_state["source-import-complete"] = not failed
            if not failed:
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
                service.library.retry_processing(metadata.document_id)
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
