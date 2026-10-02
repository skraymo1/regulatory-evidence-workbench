"""Derived coverage of pinned evidence, never a certification of regulatory absence."""

from regulatory_poc.service.regulatory_scope import national_requirements


STATUS_LABELS = {
    "pending": "Not processed",
    "error": "Processing failed - retry required",
    "proposed": "Proposed correspondence - expert review required",
    "no_comparable": "No comparable national requirement found - not proof of absence",
    "unresolved": "Insufficient evidence - no absence conclusion",
    "candidates_only": "Candidates only - no technical mapping",
    "national_pending": "National requirement - comparison incomplete",
    "national_unresolved": "Unmatched national requirement - insufficient coverage",
    "country_specific_candidate": "Potential country-specific requirement - no reference match identified",
}


def national_rows(report: dict) -> list[dict]:
    rows = report["rows"]
    matched = {match["source"]["key"] for row in rows for match in row["matches"]}
    reviewed = {
        candidate["key"] for row in rows if row["status"] in {"proposed", "no_comparable"}
        for candidate in row["candidates"]
    }
    incomplete = any(row["status"] in {"pending", "error"} for row in rows)
    sufficient = bool(rows) and all(row["status"] in {"proposed", "no_comparable"} for row in rows)
    exhaustive = sufficient and all(row.get("screening", {}).get("complete") for row in rows)
    result = []
    for source in national_requirements(report):
        if source.key in matched:
            continue
        if incomplete:
            status = "national_pending"
            note = "Baseline processing is incomplete; this national requirement is not yet mapped."
        elif not sufficient or (not exhaustive and source.key not in reviewed):
            status = "national_unresolved"
            note = (
                "No reference match is saved. Some baseline evidence remains unresolved or this national "
                "requirement was not reviewed by the model; country-specific status is not established."
            )
        else:
            status = "country_specific_candidate"
            note = (
                "No reference match was proposed after this clause was screened against every Reference requirement "
                "in the table. This is a potential country-specific requirement, not confirmed national "
                "uniqueness; expert review is required."
            ) if exhaustive else (
                "No reference match was proposed in this table. This is a potential country-specific "
                "requirement, not confirmed national uniqueness. Retrieval was bounded, not an "
                "exhaustive reverse comparison against every Reference requirement; expert review is required."
            )
        result.append({"source": source.to_dict(), "status": status, "note": note})
    return result
