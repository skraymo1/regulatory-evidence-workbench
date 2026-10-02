"""Pinned, document-specific scope and reference associations."""

from collections import Counter

from regulatory_poc.types.requirements import Requirement


def national_requirements(report: dict) -> list[Requirement]:
    scope = report["inputs"]
    selected = scope.get("selected_ids", [])
    legacy = scope.get("schema_version") not in {"regulatory-table-v3", "regulatory-table-v4"}
    return [
        item for source in scope["sources"]
        if source["document_id"] != scope.get("reference_id", "")
        for item in (
            Requirement.from_dict(value) for value in report["inventories"][source["document_id"]]
        )
        if not selected or item.key in selected or (legacy and item.identifier in selected)
    ]


def selected_keys(requirements: list[Requirement], selected: list[str], legacy_documents: set[str]) -> list[str]:
    keys = {item.key for item in requirements}
    resolved = set()
    for value in selected:
        if value in keys:
            resolved.add(value)
            continue
        matches = [item for item in requirements if item.identifier == value]
        if len(matches) > 1:
            raise ValueError("Selected identifiers are ambiguous across documents; use stable requirement keys.")
        if not matches or matches[0].document_id not in legacy_documents:
            raise ValueError("Selected requirement keys do not exist; use keys from the national inventory.")
        resolved.add(matches[0].key)
    return sorted(resolved)


def reference_pairs(
    national: list[Requirement], references: list[Requirement], source_id: str,
) -> dict[str, Requirement]:
    source = [item for item in national if item.document_id == source_id]
    for items in (source, references):
        counts = Counter(item.identifier for item in items)
        if any(count > 1 for count in counts.values()):
            raise ValueError("Ambiguous duplicate reference pairing identifiers; review and disambiguate the source inventories.")
    by_id = {item.identifier: item for item in references}
    pairs = {item.key: by_id[item.identifier] for item in source if item.identifier in by_id}
    if not pairs:
        raise ValueError("No same-identifier reference pairing exists; confirm versions and alignment.")
    return pairs


def report_reference_pairs(report: dict, national: list[Requirement]) -> dict[str, Requirement]:
    inputs = report["inputs"]
    reference_id = inputs.get("reference_id", "")
    if not reference_id:
        return {}
    document_ids = [item["document_id"] for item in inputs["sources"] if item["document_id"] != reference_id]
    source_id = inputs.get("reference_source_id", "")
    if not source_id and len(document_ids) == 1:
        source_id = document_ids[0]
    if source_id not in document_ids:
        raise ValueError("Reference source association is ambiguous; select one national document.")
    # Validate against the full inventory, not a selection hiding duplicate identifiers.
    source = [Requirement.from_dict(item) for item in report["inventories"][source_id]]
    references = [Requirement.from_dict(item) for item in report["inventories"][reference_id]]
    pairs = reference_pairs(source, references, source_id)
    return {item.key: pairs[item.key] for item in national if item.key in pairs}
