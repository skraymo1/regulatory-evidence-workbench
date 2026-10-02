from __future__ import annotations

import json

import streamlit as st
from azure.core.exceptions import AzureError

from regulatory_poc.runtime.regulatory import build_regulatory_service
from regulatory_poc.service.regulatory_coverage import STATUS_LABELS, national_rows
from regulatory_poc.service.regulatory_mapping import EQUIVALENCE_LABELS
from regulatory_poc.ui.regulatory_chat import evidence_chat
from regulatory_poc.ui.regulatory_columns import (
    ANALYSIS, ANALYSIS_COLUMNS, CITED, EQUIVALENCE, LIMITATIONS, NATIONAL_ONLY, PROPOSED_ANALYSIS,
    REFERENCE_ONLY, TERMINOLOGY, clause_name, equivalence_label, national_location, national_only_text,
    numbered, paragraphs_text, reference_only_text, terminology_text,
)
from regulatory_poc.ui.regulatory_export import xlsx_table
from regulatory_poc.ui.regulatory_sources import comparison_sources
from regulatory_poc.ui.regulatory_detail import render_requirement_detail, source_location
from regulatory_poc.ui.regulatory_setup import comparison_setup
from regulatory_poc.ui.regulatory_processing import (
    DEFAULT_PARALLEL_ROWS, PARALLEL_ROW_CHOICES, process_requirements,
)
from regulatory_poc.ui.regulatory_translation import translation_scope


def regulatory_panel(settings, repository) -> None:
    service = build_regulatory_service(settings, repository)
    st.caption(
        f"Analysis: {'live Foundry model ' + settings.model_deployment_name if service.agent else 'offline candidates only'}"
        f" | Retrieval: {settings.search_mode} | "
        "Create / resume processes documents; viewing saved tables makes no model calls."
    )
    st.caption(
        "All Reference standard requirements plus unmatched national rows. Exact source text is separate from proposed "
        "analysis and translation. qualified expert review is required; no compliance certification."
    )
    work, sources, saved, chat = st.tabs([
        "Comparisons", "Comparison sources", "Saved comparison tables", "Ask evidence",
    ])
    try:
        with sources:
            comparison_sources(service, settings)
        reports = service.list_reports()
        documents = service.library.list_documents()
        with work:
            baselines = [item for item in documents if item["role"] == "Reference baseline"]
            if not baselines:
                st.info("Start in Comparison sources: import the Reference baseline and national documents in their original languages.")
            else:
                labels = {item["metadata"]["document_id"]: item["metadata"]["title"] for item in documents}
                baseline_id = st.selectbox(
                    "Reference baseline", [item["metadata"]["document_id"] for item in baselines],
                    format_func=labels.get,
                )
                baseline = service.library.get(baseline_id)
                st.caption(f"{len(baseline['requirements'])} baseline requirements per comparison table.")
                available = [
                    item for item in sorted(reports, key=lambda item: item["created_at"], reverse=True)
                    if item["inputs"]["baseline"]["document_id"] == baseline_id
                ]
                workspace = st.radio(
                    "Comparison workspace", ["Review saved table", "Set up a comparison"],
                    horizontal=True, key="comparison-workspace",
                ) if available else "Set up a comparison"
                if workspace == "Review saved table":
                    selected_report = st.selectbox(
                        "Saved comparison", [item["report_id"] for item in available],
                        format_func=lambda key: next(
                            f"{item['inputs']['country']} | {item['created_at']} | {key[:8]}"
                            for item in available if item["report_id"] == key
                        ),
                    )
                    st.caption("Viewing saved evidence only. No model calls or changes to source approval.")
                    render_table(service, service.get(selected_report), "comparison")
                else:
                    report_id = comparison_setup(service, baseline_id, documents, labels)
                    if report_id:
                        render_table(service, service.get(report_id), "comparison")
        reports = service.list_reports()
        with saved:
            if not reports:
                st.info("No comparison tables saved yet. Create a table in Comparisons.")
            else:
                report_id = st.selectbox(
                    "Saved comparison table", [item["report_id"] for item in reports],
                    format_func=lambda key: next(
                        f"Reference standard vs {item['inputs']['country']} | {item['created_at']} | {key[:8]}"
                        for item in reports if item["report_id"] == key
                    ),
                )
                render_table(service, service.get(report_id), "saved")
        with chat:
            evidence_chat(settings, repository, service.report_search, documents, reports)
    except (RuntimeError, ValueError, OSError, AzureError) as exc:
        st.error(str(exc))


def table_rows(report: dict) -> list[dict]:
    """The reviewers' comparison layout; Proposed analysis is expanded into ANALYSIS_COLUMNS."""
    values = []
    for row in report["rows"]:
        values.append({
            "Row type": "Reference baseline",
            "Jurisdiction": report["inputs"]["country"],
            "Comparison outcome": STATUS_LABELS.get(row["status"], row["status"]),
            "Reference requirement": row["baseline"]["identifier"],
            "Reference exact name": row["baseline"]["title"],
            "Reference exact high-level description": row["baseline"]["description"],
            "Reference exact supporting paragraphs": paragraphs_text(row),
            "National requirement(s)": numbered(row, lambda match: clause_name(match["source"]))
            or STATUS_LABELS.get(row["status"], row["status"]),
            "National exact description(s)": numbered(row, lambda match: match["source"]["description"]),
            "National English translation (machine-generated)": numbered(row, _translation),
            "Reference source reference": source_location(row["baseline"]),
            "National source reference(s)": numbered(row, lambda match: national_location(match["source"])),
            EQUIVALENCE: equivalence_label(row),
            REFERENCE_ONLY: reference_only_text(row),
            NATIONAL_ONLY: national_only_text(row),
            ANALYSIS: numbered(row, lambda match: match["analysis"]),
            LIMITATIONS: row["note"],
            TERMINOLOGY: terminology_text(row),
            CITED: "\n".join(row.get("cited_instruments", [])),
            "Status": row["status"], "expert review": report["review_status"],
        })
    for row in national_rows(report):
        source = row["source"]
        item = dict.fromkeys(values[0], "") if values else {}
        item.update({
            "Row type": "Unmatched national requirement",
            "Jurisdiction": report["inputs"]["country"],
            "Comparison outcome": STATUS_LABELS[row["status"]],
            "Reference requirement": "No reference counterpart identified",
            "National requirement(s)": clause_name(source),
            "National exact description(s)": source["description"],
            "National source reference(s)": national_location(source),
            EQUIVALENCE: "Not applicable - no reference counterpart identified",
            NATIONAL_ONLY: "Entire clause",
            LIMITATIONS: row["note"],
            "Status": row["status"], "expert review": report["review_status"],
        })
        values.append(item)
    return values


def _translation(match: dict) -> str:
    title = match["english_title"].strip()
    identifier = match["source"]["identifier"].rstrip(".")
    parts = [title] if title and title.rstrip(".") != identifier else []
    return "\n".join(parts + ([match["english_description"]] if match["english_description"] else []))


SUMMARY_COLUMNS = ("Reference requirement", "National requirement(s)", EQUIVALENCE, REFERENCE_ONLY, NATIONAL_ONLY)


def summary_rows(report: dict) -> list[dict]:
    """The reviewer three-column format, with only the row labels needed to identify each row."""
    rows = []
    for row in table_rows(report):
        item = {column: row[column] for column in SUMMARY_COLUMNS}
        if row["Row type"] == "Reference baseline":
            item["Reference requirement"] = f"{row['Reference requirement']} - {row['Reference exact name']}"
        item["National requirement(s)"] = "\n".join(
            block.split("\n", 1)[0] for block in row["National requirement(s)"].split("\n\n")
        )
        rows.append(item)
    return rows


def render_table(service, report, context) -> None:
    report_id = report["report_id"]
    prefix = f"{context}-{report_id}"
    st.markdown(f"### Reference standard vs {report['inputs']['country']}")
    processed = sum(row["status"] not in {"pending", "error"} for row in report["rows"])
    proposed = sum(row["status"] == "proposed" for row in report["rows"])
    remaining = sum(row["status"] in {"pending", "error"} for row in report["rows"])
    total_column, proposed_column, unresolved_column, pending_column = st.columns(4)
    total_column.metric("Reference requirements", len(report["rows"]))
    proposed_column.metric("Proposed mappings", proposed)
    unresolved_column.metric("Unresolved / candidates only", sum(
        row["status"] in {"unresolved", "candidates_only"} for row in report["rows"]
    ))
    pending_column.metric("Pending / retry", remaining)
    st.caption(
        f"All results are unreviewed. {processed}/{len(report['rows'])} rows processed; "
        "processing does not mean technical approval."
    )
    counts = {label: sum(row.get("equivalence") == value for row in report["rows"])
              for value, label in EQUIVALENCE_LABELS.items() if value != "not_assessed"}
    st.caption("Equivalence (code-calculated from AI topic checklists; partial topics count as "
               "different): " + " | ".join(f"{label}: {count}" for label, count in counts.items()))
    st.caption(
        f"{sum(row['status'] == 'no_comparable' for row in report['rows'])} baseline rows: "
        f"no comparable national requirement found. {len(national_rows(report))} "
        "additional national rows have no saved reference match; incomplete coverage is labeled separately."
    )
    with st.expander("Source scope, versions and extraction warnings"):
        st.caption(f"Report {report_id} | report index: {report['indexing_status']}")
        st.dataframe([
            {"Role": "Reference baseline" if index == 0 else (
                "Translation reference" if source["document_id"] == report["inputs"]["reference_id"]
                else report["inputs"]["country"]
            ),
             "Document": source["title"], "Language": source["language"],
             "Revision": source.get("revision", "")}
            for index, source in enumerate([report["inputs"]["baseline"], *report["inputs"]["sources"]])
        ], hide_index=True)
        for warning in report["warnings"]:
            st.warning(warning)
    if report["inputs"]["reference_id"]:
        translation_scope(service, report, prefix)
    table_tab, summary_tab, detail_tab = st.tabs(
        ["Comparison table", "Reviewer summary (3 columns)", "Requirement detail"]
    )
    with table_tab:
        st.caption(
            f"All {len(report['rows'])} Reference requirements, one row each, plus unmatched national rows. "
            f"'{PROPOSED_ANALYSIS}' is expanded into the equivalence category, the two one-sided topic "
            "columns and a clause-by-clause analysis. National clauses are numbered [1], [2]... identically "
            "in every column. The category is calculated by code from the AI topic checklist (partial = "
            "different) and requires expert review. No-match results mean none found in the screened "
            "clauses, not proof of regulatory absence."
        )
        st.dataframe(
            table_rows(report), hide_index=True, height=540,
            column_config={column: st.column_config.TextColumn(column, width="large")
                           for column in (*ANALYSIS_COLUMNS, "National exact description(s)")},
        )
    with summary_tab:
        st.caption(
            "Short reviewer view: equivalence category plus topics present in only one side. "
            "Exact source text is in the comparison table and Requirement detail."
        )
        st.dataframe(
            summary_rows(report), hide_index=True, height=540,
            column_config={column: st.column_config.TextColumn(column, width="large")
                           for column in (REFERENCE_ONLY, NATIONAL_ONLY)},
        )
    with detail_tab:
        render_requirement_detail(service, report, prefix)
    with st.expander("Process more requirements"):
        if service.agent is None:
            st.info("Offline mode retrieves candidates only. It does not perform technical comparison or translation.")
        else:
            st.caption("Each pending row screens every selected national clause, analyses the shortlist and "
                       "re-screens for partial or missing topics: typically 2-4 model calls. Rows are saved "
                       "individually; resume does not repeat completed rows.")
        count = st.selectbox("Rows to process now", [1, 5, 10, 82], index=1, key=f"count-{prefix}")
        parallel = st.selectbox(
            "Rows processed in parallel", PARALLEL_ROW_CHOICES,
            index=PARALLEL_ROW_CHOICES.index(DEFAULT_PARALLEL_ROWS), key=f"parallel-{prefix}",
        )
        if st.button("Process pending requirements / retry errors", key=f"process-{prefix}",
                     type="primary", disabled=remaining == 0):
            updated = process_requirements(service, report, count, parallel)
            if not any(row["status"] == "error" for row in updated["rows"]):
                st.rerun()
    st.download_button(
        "Download complete table and original-source provenance (JSON)",
        json.dumps(report, ensure_ascii=False, indent=2), f"{report_id}.json",
        mime="application/json", key=f"export-json-{prefix}",
    )
    st.download_button(
        "Download comparison table (Excel)",
        xlsx_table(table_rows(report), f"Reference standard vs {report['inputs']['country']}", summary_rows(report),
                   {PROPOSED_ANALYSIS: list(ANALYSIS_COLUMNS)}),
        f"reference profile-2-1-vs-{report['inputs']['country']}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"export-xlsx-{prefix}",
    )
    st.download_button(
        "Download comparison table (Markdown)", markdown_table(report),
        f"reference profile-2-1-vs-{report['inputs']['country']}.md",
        mime="text/markdown", key=f"export-md-{prefix}",
    )
    if st.button("Save table to report search / retry indexing", key=f"index-{prefix}"):
        service.index_report(report_id)
        st.rerun()


def markdown_table(report: dict) -> str:
    rows = table_rows(report)
    headers = list(rows[0]) if rows else []
    def cell(value: str) -> str:
        return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")
    def section(items: list[dict], columns: list[str]) -> list[str]:
        return [
            "| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |",
            *("| " + " | ".join(cell(item[column]) for column in columns) + " |" for item in items),
        ]
    lines = [
        f"# Reference standard vs {report['inputs']['country']}",
        "", "Unreviewed proposed mapping. Source quotations are not AI summaries.",
        "Original line breaks render as breaks; download JSON for lossless strings and provenance.",
        "", "## Comparison table", "",
        f"'{PROPOSED_ANALYSIS}' is expanded into: " + "; ".join(ANALYSIS_COLUMNS) + ".",
        "", *section(rows, headers),
        "", "## Reviewer summary (3 columns)", "", *section(summary_rows(report), list(SUMMARY_COLUMNS)),
    ]
    return "\n".join(lines)
