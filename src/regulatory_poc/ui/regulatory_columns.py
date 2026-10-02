"""Reviewer-facing text for the three-column equivalence format and terminology checks."""

from __future__ import annotations

from regulatory_poc.service.regulatory_coverage import STATUS_LABELS
from regulatory_poc.service.regulatory_mapping import EQUIVALENCE_LABELS


EQUIVALENCE = "Are the requirements equivalent?"
REFERENCE_ONLY = ("Topics that are explicitly present in Reference standard requirement but are not explicit "
            "in the national requirement")
NATIONAL_ONLY = ("Topics that are explicitly present in the national requirement but not "
                 "explicit in the Reference standard")
TERMINOLOGY = "Key terminology to validate (AI-flagged)"
CITED = "Cited instruments to confirm are in the collection"
ANALYSIS = "Clause-by-clause analysis (English)"
LIMITATIONS = "Evidence limitations"
PROPOSED_ANALYSIS = "Proposed analysis (English)"
ANALYSIS_COLUMNS = (EQUIVALENCE, REFERENCE_ONLY, NATIONAL_ONLY, ANALYSIS)
STATUS_TEXT = {"partial": "partially explicit", "not_explicit": "not explicit"}


def equivalence_label(row: dict) -> str:
    value = row.get("equivalence")
    if value is None:
        return "Not assessed - " + STATUS_LABELS.get(row["status"], row["status"])
    label = EQUIVALENCE_LABELS[value]
    topics = row.get("topics", [])
    if topics and value != "not_assessed":
        different = sum(topic["status"] != "explicit" for topic in topics)
        label += f" ({different} of {len(topics)} reference topics differ; unreviewed)"
    return label


def _clause(identifier: str) -> str:
    return f"clause {identifier}" if identifier[:1].isdigit() else identifier


def clause_name(source: dict) -> str:
    """'Document, clause 5' plus the clause heading only when it adds more than the number."""
    name = source["source_name"].rsplit(".", 1)[0] if source["source_name"].lower().endswith(".pdf") \
        else source["source_name"]
    label = f"{name}, {_clause(source['identifier'])}"
    title = source["title"].strip()
    return label if title.rstrip(".") == source["identifier"].rstrip(".") else f"{label} - {title}"


def national_location(source: dict) -> str:
    return (f"{source['source_name']} | {_clause(source['identifier'])} | "
            f"PDF pages {source['page']}-{source['end_page']}")


def numbered(row: dict, render) -> str:
    """One block per national correspondence, numbered [1], [2] identically in every column."""
    return "\n\n".join(
        f"[{index}] {text}" for index, match in enumerate(row["matches"], start=1)
        if (text := render(match))
    )


def _identifiers(row: dict, keys: list[str]) -> str:
    numbers = {match["source"]["key"]: f"[{index}]" for index, match in enumerate(row["matches"], start=1)}
    names = {item["key"]: clause_name(item) for item in row.get("candidates", [])}
    return ", ".join(numbers.get(key) or names.get(key, key) for key in keys)


def reference_only_text(row: dict) -> str:
    lines = []
    for topic in row.get("topics", []):
        if topic["status"] == "explicit":
            continue
        source = f"[{topic['source']}] " if topic.get("source") else ""
        line = f"- {source}{topic['topic']} ({STATUS_TEXT[topic['status']]})"
        if topic["status"] == "partial" and topic["keys"]:
            line += f" - partly in {_identifiers(row, topic['keys'])}"
        lines.append(line)
    if lines:
        return "\n".join(lines)
    return "None identified" if row.get("topics") else ""


def national_only_text(row: dict) -> str:
    lines = [
        f"- {item['topic']}" + (f" (in {_identifiers(row, item['keys'])})" if item["keys"] else "")
        for item in row.get("national_only_topics", [])
    ]
    if lines:
        return "\n".join(lines)
    return "None identified" if row.get("topics") and row.get("matches") else ""


def terminology_text(row: dict) -> str:
    return "\n".join(
        f"- {item['term']} ({item['language']}): literal '{item['literal_english']}'"
        + (f"; reference term '{item['reference_term']}'" if item["reference_term"] else "")
        + (f" - {item['concern']}" if item["concern"] else "")
        for item in row.get("terminology", [])
    )


def paragraphs_text(row: dict) -> str:
    return "\n\n".join(item["text"] for item in row.get("paragraphs", []))
