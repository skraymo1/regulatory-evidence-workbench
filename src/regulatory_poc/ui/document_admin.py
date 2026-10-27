from __future__ import annotations

import json
from urllib.parse import unquote, urlsplit

import streamlit as st

from regulatory_poc.service.usage import usage_summary


def document_admin(admin) -> None:
    st.subheader("Document admin")
    if admin.blob_browser is None:
        _document_inventory(admin)
        return
    inventory, storage = st.tabs(["Document inventory", "Blob storage (all containers)"])
    with inventory:
        _document_inventory(admin)
    with storage:
        blob_storage(admin)


def _document_inventory(admin) -> None:
    st.caption(
        "Uploaded originals, extracted inventories and indexing state. Indexed means ready for retrieval, "
        "not expert approved. Comparison progress is shown separately."
    )
    if st.button("Refresh documents", key="admin-refresh"):
        st.rerun()
    documents = admin.documents()
    columns = st.columns(4)
    for column, label, value in zip(columns, ["Documents", "Indexed", "Pending", "Failed"], [
        len(documents), sum(item["indexing_status"] == "indexed" for item in documents),
        sum(item["indexing_status"] == "pending" for item in documents),
        sum(item["indexing_status"] == "failed" for item in documents),
    ]):
        column.metric(label, value)
    if not documents:
        st.info("No documents uploaded yet. Add documents in Comparison sources.")
        return
    status = st.selectbox("Processing state", ["All", "indexed", "pending", "failed"], key="admin-status")
    query = st.text_input("Find document", key="admin-query").strip().casefold()
    selected = [
        item for item in documents
        if (status == "All" or item["indexing_status"] == status)
        and (not query or query in " ".join(str(item["metadata"].get(key, ""))
                                           for key in ("title", "source_name", "document_id", "country")).casefold())
    ]
    st.dataframe([{
        "Document": item["metadata"]["title"], "Document ID": item["metadata"]["document_id"],
        "Role": item["role"], "Language": item["metadata"]["language"],
        "Jurisdiction": item["metadata"].get("country", ""),
        "Status": item["indexing_status"], "Uploaded (UTC)": item["uploaded_at"],
        "Processed (UTC)": item["processed_at"], "Bytes": item["size_bytes"],
        "Requirements": item["requirement_count"], "Chunks": item["chunks_indexed"],
        "Comparisons": item["comparison_count"],
        "Comparison rows processed": item["comparison_rows_processed"],
        "Comparison rows pending": item["comparison_rows_pending"],
        "Comparison rows failed": item["comparison_rows_failed"],
        "Error": item["indexing_error"],
    } for item in selected], hide_index=True)
    if not selected:
        st.info("No documents match these filters.")
        return
    by_id = {item["metadata"]["document_id"]: item for item in selected}
    document_id = st.selectbox(
        "Document details", list(by_id),
        format_func=lambda key: f"{by_id[key]['metadata']['title']} ({key})", key="admin-document",
    )
    record = by_id[document_id]
    st.caption(f"Approval: {record['approval_status']}. Missing historical timestamps/counts are unknown.")
    if record["indexing_error"]:
        st.error(record["indexing_error"])
    for warning in record["warnings"]:
        st.warning(warning)
    if record["extraction_complete"] is False:
        st.warning("Extraction completeness is not established; review the original document.")
    with st.expander("Source metadata"):
        st.json(record["metadata"])
    if record["indexing_status"] != "indexed" and record["original_available"]:
        if st.button("Retry document processing", key="admin-retry"):
            with st.spinner("Retrying extraction / indexing; model calls may incur charges"):
                admin.retry(document_id)
            st.rerun()
    if record["original_available"] and st.button("Prepare original download", key="admin-original"):
        content, metadata = admin.original(document_id)
        st.download_button("Download original", content, metadata.source_name, key="admin-download")
    if st.checkbox("Show processed information", key="admin-show-processed"):
        processed_information(admin.processed(document_id), document_id)


def _navigate_folder(key_prefix: str, container: str, prefix: str) -> None:
    st.session_state[f"{key_prefix}-prefix-{container}"] = prefix
    st.session_state[f"{key_prefix}-pages-{container}-{prefix}"] = [None]
    st.session_state.pop(f"{key_prefix}-jump-error", None)


def _jump_blob_folder(browser, containers: list[str], container: str, key_prefix: str) -> None:
    location = st.session_state[f"{key_prefix}-jump"].strip()
    error_key = f"{key_prefix}-jump-error"
    if "://" in location:
        try:
            target, account = urlsplit(location), urlsplit(browser.client.url)
        except ValueError:
            st.session_state[error_key] = "Enter a valid Blob folder URL."
            return
        if (target.scheme != account.scheme or target.netloc.casefold() != account.netloc.casefold()
                or target.query or target.fragment):
            st.session_state[error_key] = "Use a Blob URL in this storage account, without query parameters or fragments."
            return
        base = account.path.rstrip("/") + "/"
        if not target.path.startswith(base):
            st.session_state[error_key] = "Include a container and optional folder in the Blob URL."
            return
        location = unquote(target.path[len(base):])
        target_container, _, prefix = location.partition("/")
        if target_container not in containers:
            st.session_state[error_key] = "That container is not in this storage account."
            return
    else:
        location = location.lstrip("/")
        first, _, rest = location.partition("/")
        target_container = first if first in containers else container
        prefix = rest if first in containers else location
    prefix = prefix if not prefix or prefix.endswith("/") else prefix + "/"
    st.session_state.pop(error_key, None)
    st.session_state[f"{key_prefix}-container"] = target_container
    _navigate_folder(key_prefix, target_container, prefix)


def blob_file_picker(browser, *, key_prefix: str = "blob", pdf_only: bool = False) -> tuple[str, str] | None:
    containers = browser.containers()
    if not containers:
        st.info("This storage account has no containers.")
        return
    names = [item["name"] for item in containers]
    remembered = st.session_state.get(f"{key_prefix}-last-container")
    container_key = f"{key_prefix}-container"
    if container_key not in st.session_state:
        st.session_state[container_key] = remembered if remembered in names else names[0]
    container_column, up_column = st.columns([4, 1])
    container = container_column.selectbox(
        "Blob container", names, key=container_key, index=None,
    )
    if container is None:
        st.info("Select a container to browse its folders.")
        return
    st.session_state[f"{key_prefix}-last-container"] = container
    prefix_key = f"{key_prefix}-prefix-{container}"
    prefix = st.session_state.get(prefix_key, "")
    if prefix:
        parent = prefix.rstrip("/").rsplit("/", 1)[0] + "/" if "/" in prefix.rstrip("/") else ""
        up_column.button(
            "Up one folder", on_click=_navigate_folder, args=(key_prefix, container, parent),
            key=f"{key_prefix}-up", icon=":material/arrow_upward:",
        )
    with st.container(horizontal=True):
        st.button(
            container, on_click=_navigate_folder, args=(key_prefix, container, ""), key=f"{key_prefix}-root",
            icon=":material/home:", disabled=not prefix,
        )
        path = ""
        for index, part in enumerate(prefix.rstrip("/").split("/") if prefix else []):
            path += part + "/"
            st.button(
                part or "/", on_click=_navigate_folder, args=(key_prefix, container, path),
                key=f"{key_prefix}-crumb-{container}-{index}", disabled=path == prefix,
                icon=":material/chevron_right:",
            )
    st.caption(f"{container}/{prefix}")
    with st.expander("Jump to a folder"):
        with st.form(f"{key_prefix}-jump-form"):
            st.text_input(
                "Folder path or Blob URL", key=f"{key_prefix}-jump",
                placeholder=f"{container}/folder/subfolder/",
                help="Paste a container/folder path, a full Blob folder URL, or a path relative to the container root.",
            )
            st.form_submit_button(
                "Go to folder", on_click=_jump_blob_folder, args=(browser, names, container, key_prefix),
            )
    if st.session_state.get(f"{key_prefix}-jump-error"):
        st.error(st.session_state[f"{key_prefix}-jump-error"])
    page_key = f"{key_prefix}-pages-{container}-{prefix}"
    pages = st.session_state.setdefault(page_key, [None])
    page = browser.entries(container, prefix, pages[-1])
    entries = page["entries"]
    folders = [item["name"] for item in entries if item["kind"] == "folder"]
    if folders:
        st.caption("Folders - click to open")
        columns = st.columns(2)
        for index, folder in enumerate(folders):
            columns[index % 2].button(
                folder.removeprefix(prefix), on_click=_navigate_folder,
                args=(key_prefix, container, folder), key=f"{key_prefix}-folder-{container}-{folder}",
                icon=":material/folder:", width="stretch",
            )
    files = [item for item in entries if item["kind"] == "file"
             and (not pdf_only or item["name"].lower().endswith(".pdf"))]
    if files and not pdf_only:
        st.dataframe([{
            "Name": item["name"].removeprefix(prefix), "Bytes": item["size_bytes"],
            "Last modified (UTC)": item["last_modified"],
        } for item in files], hide_index=True)
    if len(pages) > 1 or page["continuation_token"]:
        st.caption("100 entries per page. Folder and file lists show the current page.")
        previous, next_page = st.columns(2)
        if previous.button("Previous page", disabled=len(pages) == 1, key=f"{key_prefix}-previous"):
            pages.pop()
            st.rerun()
        if next_page.button("Next page", disabled=not page["continuation_token"], key=f"{key_prefix}-next"):
            pages.append(page["continuation_token"])
            st.rerun()
    if not files:
        st.info(
            f"No {'PDF ' if pdf_only else ''}files on this page. "
            "Click a folder above or use Jump to a folder."
        )
        return
    name = st.selectbox(
        "PDF file" if pdf_only else "File", [item["name"] for item in files],
        format_func=lambda value: value.removeprefix(prefix), key=f"{key_prefix}-file-{container}-{prefix}",
    )
    return container, name


def blob_storage(admin) -> None:
    browser = admin.blob_browser
    st.caption(
        "Read-only access to every container and folder in the configured storage account, "
        "including files not imported into the workbench, reports and application state. "
        "Browsing does not process files or make model calls."
    )
    selected = blob_file_picker(browser)
    if selected is None:
        return
    container, name = selected
    if st.checkbox("Inspect selected Blob file", key="blob-inspect"):
        preview = browser.preview(container, name)
        st.json({key: value for key, value in preview.items() if key != "text"})
        if preview["preview_error"]:
            st.warning(preview["preview_error"])
        elif preview["text"] is not None:
            if preview["truncated"]:
                st.warning("Preview is limited to the first 256 KiB. Download the file for the complete contents.")
            if name.lower().endswith(".json") and not preview["truncated"]:
                try:
                    value = json.loads(preview["text"])
                except json.JSONDecodeError:
                    st.warning("Stored JSON is invalid; showing its saved text without modifying it.")
                    st.code(preview["text"], language=None)
                else:
                    st.json(value)
            else:
                st.code(preview["text"], language=None)
        else:
            st.info("Binary file: download the original to view its contents.")
        matches = admin.blob_document_ids(container, name)
        if matches:
            document_id = st.selectbox("Associated processed document", matches, key="blob-document")
            processed_information(admin.processed(document_id), f"blob-{document_id}")
        else:
            st.info(
                "This Blob has no matching document processing record. Its saved file contents are shown "
                "above when previewable; browsing does not import it."
            )
        if st.button("Prepare Blob download", key="blob-prepare-download"):
            content = browser.download(container, name, ui=True).readall()
            st.download_button(
                "Download Blob file", content, name.rsplit("/", 1)[-1],
                mime="application/octet-stream", key="blob-download",
            )


def processed_information(processed: dict, document_id: str) -> None:
    st.markdown("### Processed information")
    st.caption(
        "Saved content only: no extraction, embeddings, model calls or reprocessing. "
        "Extracted source text is separate from generated analysis; all outputs require expert review."
    )
    requirements_tab, chunks_tab, comparisons_tab = st.tabs([
        "Extracted requirements", "Indexed passages", "Saved comparison results",
    ])
    with requirements_tab:
        requirements = processed["requirements"]
        if not requirements:
            st.info("No requirement inventory is saved for this document. General uploads may have indexed passages only.")
        else:
            st.caption(f"{len(requirements)} saved requirements. Quotations preserve the extracted source text.")
            st.dataframe([{
                "Identifier": item["identifier"], "Exact title": item["title"],
                "Exact description": item["description"], "PDF page": item["page"],
                "End page": item["end_page"], "Language": item["language"],
            } for item in requirements], hide_index=True)
            by_key = {item["key"]: item for item in requirements}
            key = st.selectbox(
                "Inspect extracted requirement", list(by_key),
                format_func=lambda value: f"{by_key[value]['identifier']} - {by_key[value]['title']}",
                key=f"admin-requirement-{document_id}",
            )
            item = by_key[key]
            st.caption(
                f"{item['source_name']} | PDF pages {item['page']}-{item['end_page']} | "
                f"Source SHA-256: {item['source_hash']}"
            )
            st.code(item["title"], language=None)
            st.code(item["description"], language=None)
    with chunks_tab:
        chunks = processed["chunks"]
        if not chunks:
            st.info("No saved passages were found in the index. Check the document processing state and errors.")
        else:
            st.caption(
                f"{len(chunks)} indexed passages. These are stored retrieval units, not AI summaries. "
                "Chunking may normalize whitespace or add requirement keys."
            )
            st.dataframe([{
                "Chunk ID": item["chunk_id"], "Ordinal": item["ordinal"],
                "Characters": len(item["text"]), "Indexed text": item["text"],
            } for item in chunks], hide_index=True)
            by_key = {item["chunk_id"]: item for item in chunks}
            key = st.selectbox(
                "Inspect indexed passage", list(by_key),
                format_func=lambda value: f"{by_key[value]['ordinal']} | {value[:16]}",
                key=f"admin-chunk-{document_id}",
            )
            st.code(by_key[key]["text"], language=None)
            with st.expander("Indexed passage provenance"):
                st.json(by_key[key]["metadata"])
    with comparisons_tab:
        reports = processed["comparisons"]
        if not reports:
            st.info("No saved comparison tables use this document yet.")
        else:
            from regulatory_poc.ui.regulatory import table_rows

            by_id = {item["report_id"]: item for item in reports}
            report_id = st.selectbox(
                "Comparison using this document", list(by_id),
                format_func=lambda value: (
                    f"{by_id[value]['inputs']['country']} | {by_id[value]['created_at']} | {value[:12]}"
                ),
                key=f"admin-comparison-{document_id}",
            )
            report = by_id[report_id]
            st.caption(
                "Full saved comparison scope, including other selected documents. Pending and failed "
                "rows are retained; absence of a match is not proof of regulatory absence."
            )
            st.caption(
                f"Review status: {report['review_status']} | Indexing status: {report['indexing_status']}"
            )
            for warning in report["warnings"]:
                st.warning(warning)
            st.dataframe(table_rows(report), hide_index=True, height=500)
            with st.expander("Saved translation checks"):
                st.json({
                    "article_checks": report.get("article_checks", {}),
                    "row_checks": {
                        row["baseline"]["key"]: row["translation_checks"]
                        for row in report["rows"] if row.get("translation_checks")
                    },
                })
            st.download_button(
                "Download saved comparison (JSON)", json.dumps(report, ensure_ascii=False, indent=2),
                f"{report_id}.json", mime="application/json", key=f"admin-report-export-{document_id}",
            )
    st.download_button(
        "Download processed information (JSON)", json.dumps(processed, ensure_ascii=False, indent=2),
        f"{document_id}-processed.json", mime="application/json", key=f"admin-processed-export-{document_id}",
    )


def model_usage(admin) -> None:
    st.subheader("Model usage & estimated costs")
    st.caption(
        "Provider-reported input/output tokens, recorded from this release onward. "
        "Viewing saved documents and tables makes no model calls. Costs are estimates, not Azure invoices."
    )
    if st.button("Refresh usage", key="usage-refresh"):
        st.rerun()
    events = admin.usage.events()
    documents = {item["metadata"]["document_id"]: item["metadata"]["title"] for item in admin.documents()}
    ids = sorted({key for item in events for key in item.get("document_ids", [])} | documents.keys())
    document_id = st.selectbox(
        "Usage for document", ["", *ids], key="usage-document",
        format_func=lambda key: f"{documents.get(key, key)} ({key})" if key else "All documents / calls",
    )
    model = st.selectbox("Model deployment", ["", *sorted({item["model"] for item in events})],
                         format_func=lambda value: value or "All models", key="usage-model")
    operation = st.selectbox("Operation", ["", *sorted({item["operation"] for item in events})],
                             format_func=lambda value: value or "All operations", key="usage-operation")
    dates = st.date_input("Date range (UTC)", value=(), key="usage-dates")
    if len(dates) == 1:
        st.info("Select an end date to apply the date range.")
    filtered = admin.usage_report(
        document_id, model, operation,
        dates[0].isoformat() if len(dates) == 2 else "",
        dates[1].isoformat() if len(dates) == 2 else "",
    )
    summary, selected = filtered["summary"], filtered["events"]
    columns = st.columns(4)
    columns[0].metric("Model calls", summary["calls"])
    columns[1].metric("Reported input tokens", summary["input_tokens"])
    columns[2].metric("Reported output tokens", summary["output_tokens"])
    known_cost = summary["estimated_cost_usd"]
    unknown = summary["unpriced_or_unknown_calls"]
    columns[3].metric("Estimated USD (known calls)", f"${known_cost:.6f}" if known_cost is not None else "Unknown")
    if unknown:
        st.warning(
            f"{unknown} call(s) have unknown cost (missing pricing or token usage, failed or in flight). "
            f"{summary['unknown_usage_calls']} call(s) lack complete token usage. Totals are partial."
        )
    st.caption(
        "Calls using multiple documents are shared: a document filter shows the full call cost, "
        "not an allocated share. Do not add document-filter totals together. Input is priced at the "
        "standard rate (no cache/batch discounts); infrastructure and managed Search/IQ charges are excluded. "
        "SDK-internal retries and requests without returned usage may incur additional, unmeasured charges."
    )
    models = sorted({item["model"] for item in selected})
    st.dataframe([{"Model": key, **usage_summary([item for item in selected if item["model"] == key])}
                  for key in models], hide_index=True)
    st.dataframe([{
        "Started (UTC)": item["started_at"], "Model": item["model"], "Operation": item["operation"],
        "Documents": ", ".join(item.get("document_ids", [])), "Status": item["status"],
        "Input tokens": item["input_tokens"], "Output tokens": item["output_tokens"],
        "Estimated USD": item["estimated_cost_usd"], "Report": item.get("report_id", ""),
    } for item in selected], hide_index=True)
    st.download_button(
        "Download usage ledger (JSON)", json.dumps(filtered, indent=2),
        "model-usage.json", mime="application/json", key="usage-export",
    )
    with st.expander("Configured USD prices per million tokens"):
        st.caption(
            "Use deployment names and your contracted input/output rates. Saved prices apply to subsequent "
            "calls across API and UI, overriding POC_MODEL_PRICING_USD_PER_MILLION. Historical calls keep "
            "their original rates. No default retail price is assumed."
        )
        with st.form("model-pricing"):
            value = st.text_area(
                "Pricing JSON", json.dumps(admin.usage.pricing, indent=2),
                help='Example shape: {"your-deployment": {"input": 1.0, "output": 2.0}}. '
                     "These example numbers are not actual model prices.",
            )
            if st.form_submit_button("Save model prices"):
                admin.usage.set_pricing(value)
                st.success("Prices saved for future calls.")
