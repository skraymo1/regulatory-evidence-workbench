"""Deterministic extraction of literal requirements from local PDF bytes.

`complete` is an enumeration claim, not a regulatory/fidelity judgement. Only
the reference profile's known 1..82 high-level enumeration is certified here. National inventories
are provisional and must be reviewed, especially scanned files and amendments.
Selected identifiers define scope only; highlights never imply approval.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import replace

from regulatory_poc.repo.requirement_source import Source, read_source
from regulatory_poc.types.models import DocumentMetadata
from regulatory_poc.types.requirements import Requirement, RequirementInventory


def _row(source, metadata, digest, identifier, title_start, title_end, body_start, body_end):
    text = source.text
    title = text[title_start:title_end].strip()
    description = text[body_start:body_end].strip()
    end = body_start + len(text[body_start:body_end].rstrip())
    return Requirement(
        key=f"{metadata.document_id}:{digest}:{identifier}",
        identifier=identifier, title=title, description=description,
        document_id=metadata.document_id, source_name=metadata.source_name,
        source_hash=digest, page=source.page_at(title_start),
        end_page=source.page_at(max(title_start, end - 1)), language=metadata.language,
    )


def _reference_profile(source, metadata, digest):
    text, rows, warnings = source.text, [], []
    headings = list(re.finditer(r"(?m)^Requirement ([1-9]\d*):[ \t]*", text))
    for index, heading in enumerate(headings):
        limit = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        tail = text[heading.end():limit]
        # The publication's headings continue in lower case; statements start a
        # new capitalized line. TOC entries instead contain dotted page leaders.
        split = re.search(r"\n(?=[A-Z])", tail)
        if re.search(r"\.{3,}", tail[:400]):
            continue
        if split is None:
            warnings.append(f"Requirement {heading[1]}: cannot separate heading and statement.")
            continue
        body_start = heading.end() + split.end()
        boundary = re.search(
            r"(?m)^(?:\d+\.\d+[A-Z]?\.[ \t]|[A-Z][A-Z ,:()–/-]{5,}$)",
            text[body_start:limit],
        )
        body_end = body_start + boundary.start() if boundary else limit
        for margin in re.finditer(r"(?m)^\d+(?:[ \t]+[A-Z]|[ \t]*$)", text[body_start:body_end]):
            # A continued sentence may cross the printed page number. Keep that
            # number in the literal quote, but exclude footnotes after a full stop.
            if text[body_start:body_start + margin.start()].rstrip().endswith("."):
                body_end = body_start + margin.start()
                break
        row = _row(source, metadata, digest, f"Requirement {heading[1]}",
                   heading.end(), body_start, body_start, body_end)
        if not row.description or "shall" not in row.description:
            warnings.append(f"{row.identifier}: high-level statement could not be verified.")
        rows.append(row)
    expected = {f"Requirement {n}" for n in range(1, 83)}
    counts = Counter(row.identifier for row in rows)
    missing = sorted(expected - counts.keys(), key=lambda x: int(x.split()[-1]))
    duplicates = sorted(key for key, count in counts.items() if count > 1)
    unexpected = sorted(counts.keys() - expected)
    if missing:
        warnings.append("Missing reference-profile headings: " + ", ".join(missing))
    if duplicates:
        warnings.append("Duplicate reference-profile body headings: " + ", ".join(duplicates))
    if unexpected:
        warnings.append("Unexpected reference-profile headings: " + ", ".join(unexpected))
    return rows, warnings


def _korea(source, metadata, digest):
    text = source.text
    pattern = r"(?m)^제(?P<ko>\d+)조(?:의(?P<sub>\d+))?\((?P<kt>[^)]+)\)|Article (?P<en>\d+)(?:-(?P<esub>\d+))?[ \t]*\((?P<et>[A-Z][^()]+)\)"
    headings = list(re.finditer(pattern, text))
    rows = []
    supplement = re.search(r"(?m)^[ \t]*부칙\b|ADDENDA|Addenda", text)
    for index, heading in enumerate(headings):
        identifier = f"Article {heading['ko'] or heading['en']}"
        suffix = heading["sub"] or heading["esub"]
        if suffix:
            identifier += f"-{suffix}"
        if supplement and heading.start() > supplement.start():
            identifier = f"Addendum {identifier}"
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        boundary = re.search(
            r"(?m)^[ \t]*(?:제\d+[장절][ \t]|부칙\b)|Chapter [ⅠⅡⅢⅣⅤIVX]+|Section \d+|ADDENDA|Disclaimer",
            text[heading.end():end],
        )
        if boundary:
            end = heading.end() + boundary.start()
        title_group = "kt" if heading["ko"] else "et"
        rows.append(_row(source, metadata, digest, identifier,
                         heading.start(title_group), heading.end(title_group), heading.end(), end))
    warnings = [
        "Article inventory is provisional; cross-page quotations may include running headers.",
        "Source and reference files may represent different revisions; matching identifiers do not establish fidelity.",
        "No highlighted article is automatically approved; optional selected_ids define comparison scope only.",
    ]
    numbers = {int(row.identifier.split()[1].split("-")[0]) for row in rows
               if row.identifier.startswith("Article ")}
    missing = sorted(set(range(1, max(numbers, default=0) + 1)) - numbers)
    if missing:
        warnings.append("Article numbers not found (verify omissions/deletions): " + ", ".join(map(str, missing)))
    return rows, warnings


def _argentina(source, metadata, digest):
    text, rows = source.text, []
    criteria = re.search(r"(?m)^[A-Z]\.[ \t]*CRITERIOS[ \t]*$", text)
    if not criteria:
        return rows, ["No readable CRITERIOS section; obtain a text-bearing PDF or reviewed local OCR."]
    headings = list(re.finditer(r"(?m)^[ \t]*(\d+)\.[ \t]+", text[criteria.end():]))
    for index, heading in enumerate(headings):
        start = criteria.end() + heading.start()
        body_start = criteria.end() + heading.end()
        end = criteria.end() + headings[index + 1].start() if index + 1 < len(headings) else len(text)
        # Criteria have no separate names: retain the literal numbered marker,
        # rather than manufacture a descriptive title.
        rows.append(_row(source, metadata, digest, heading[1],
                         start, body_start, body_start, end))
    return rows, [
        "Argentine numbered CRITERIOS clauses are provisional; titles are literal clause numbers, not invented names.",
        "Cross-page clauses retain source headers/footers; review clause boundaries and completeness.",
    ]


_UK_CATEGORY = re.compile(
    r"(?im)^(?:Fundamental principles|Leadership and management for|The regulatory "
    r"assessment of safety cases|Safety cases|Siting|Engineering principles?|Radiation protection|"
    r"Fault analysis|Numerical targets|Accident management|Emergency preparedness|"
    r"Radioactive waste management|Decommissioning|Land quality management|"
    r"Control and remediation of radioactively contaminated land)"
)


def _uk(source, metadata, digest):
    text, headings = source.text, []
    categories = {text[a:b].strip() for a, b in source.bold if _UK_CATEGORY.search(text[a:b])}
    for code in re.finditer(r"(?m)\b([A-Z]{1,6}\.\d+)[ \t]*$", text):
        preceding = [(a, b) for a, b in source.bold
                     if b <= code.start() and code.start() - b < 240 and _UK_CATEGORY.search(text[a:b])]
        # Some table rows lose bold styling. Reuse only a literal category span
        # attested by another table in this very PDF, never a fabricated title.
        if not preceding:
            for category in categories:
                position = text.rfind(category, max(0, code.start() - 240), code.start())
                if position >= 0 and text[position + len(category):code.start()].strip():
                    preceding.append((position, position + len(category)))
        if not preceding:
            continue
        a, b = max(preceding, key=lambda span: span[1])
        if not _UK_CATEGORY.search(text[a:b]):
            continue
        if re.search(r"(?m)^\d+\.|\b[A-Z]{1,6}\.\d+", text[b:code.start()]):
            continue
        headings.append((a, b, code))
    rows = []
    for index, (a, title_start, code) in enumerate(headings):
        limit = headings[index + 1][0] if index + 1 < len(headings) else len(text)
        page = source.page_at(code.start())
        limit = min(limit, source.starts[page] if page < len(source.pages) else len(text))
        body_start = code.end()
        boundary = re.search(r"(?m)^[ \t]*(?:\d+\.[ \t]|UNCONTROLLED COPY WHEN PRINTED)", text[body_start:limit])
        end = body_start + boundary.start() if boundary else limit
        rows.append(_row(source, metadata, digest, code[1], title_start, code.start(), body_start, end))
    return rows, [
        "UK SAP table extraction is provisional; font/layout changes or cross-page statements require review.",
        "Only detected principle tables are included, not numbered explanatory paragraphs or numerical-target tables.",
    ]


def highlighted_article_ids(content: bytes) -> tuple[str, ...]:
    """Return no approvals: annotation geometry cannot establish article scope.

    A yellow annotation can mark only a phrase, not an approved entire article.
    This conservative helper validates the PDF but requires human selected_ids.
    """
    read_source(content)
    return ()


def _numbered(source, metadata, digest):
    """Conservative, language-neutral line markers, not semantic extraction."""
    text = source.text
    headings = list(re.finditer(
        r"(?m)^[ \t]*(?P<id>\d+(?:\.\d+)*)(?P<marker>[.)、．:]?)[ \t]+(?P<title>[^\n]+)", text
    ))
    headings = [heading for heading in headings if not re.search(r"\.{3,}|…{2,}", heading["title"])]
    rows = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        if text[heading.end():end].strip():
            title_start, title_end, body_start = heading.start("title"), heading.end("title"), heading.end()
        else:
            # Inline clauses have no reliably separable title: quote the marker.
            title_start, title_end, body_start = heading.start("id"), heading.end("marker"), heading.start("title")
        rows.append(_row(source, metadata, digest, heading["id"],
                         title_start, title_end, body_start, end))
    return rows, [
        "Language-neutral numbered-heading extraction is provisional, not complete or semantically validated.",
        "Review headings, numbered lists, running headers, tables and clause boundaries against the original PDF.",
    ]


def _auto(source, metadata, digest):
    text = source.text
    if re.search(r"(?m)^Requirement [1-9]\d*:", text):
        rows, warnings = _reference_profile(source, metadata, digest)
    elif re.search(r"(?m)^제\d+조(?:의\d+)?\(|^Article \d+(?:-\d+)?[ \t]*\([A-Z]", text):
        rows, warnings = _korea(source, metadata, digest)
    elif re.search(r"(?m)^[A-Z]\.[ \t]*CRITERIOS[ \t]*$", text):
        rows, warnings = _argentina(source, metadata, digest)
    elif _UK_CATEGORY.search(text) and re.search(r"(?m)\b[A-Z]{1,6}\.\d+[ \t]*$", text):
        rows, warnings = _uk(source, metadata, digest)
    else:
        return _numbered(source, metadata, digest)
    if not rows:
        return _numbered(source, metadata, digest)
    return rows, [*warnings, "Automatically detected structure is provisional; review extraction boundaries and completeness."]


def extract_requirements(
    content: bytes, metadata: DocumentMetadata, *, kind: str = "auto", selected_ids: tuple[str, ...] = (),
) -> RequirementInventory:
    """Extract literal spans, then strictly filter explicit source identifiers.

    An empty selection returns the full *unapproved* inventory. Unknown or
    duplicate selected identifiers raise ValueError. National results always
    report complete=False, including explicitly selected subsets.
    """
    parsers = {"reference": _reference_profile, "uk": _uk, "korea": _korea, "argentina": _argentina,
               "auto": _auto, "numbered": _numbered}
    if kind not in {*parsers, "generic"}:
        raise ValueError(f"Unknown requirement kind {kind!r}; choose auto or a supported structural profile.")
    if not isinstance(selected_ids, tuple) or any(not isinstance(item, str) or not item for item in selected_ids):
        raise ValueError("selected_ids must be a tuple of nonempty exact identifiers.")
    if len(set(selected_ids)) != len(selected_ids):
        raise ValueError("selected_ids contains duplicates; select each identifier only once.")
    source = read_source(content)
    digest = hashlib.sha256(content).hexdigest()
    if kind == "generic":
        rows, warnings = [], ["Generic PDF structure is unsupported for reliable enumeration; choose a known kind or supply reviewed requirements."]
    else:
        rows, warnings = parsers[kind](source, metadata, digest)
    body_pages = range(min((r.page for r in rows), default=1),
                       max((r.end_page for r in rows), default=len(source.pages)) + 1)
    empty = [str(index + 1) for index, page in enumerate(source.pages)
             if not page.strip() and (kind != "reference" or index + 1 in body_pages)]
    if empty:
        warnings.append("Pages without extractable text (blank or scanned; OCR/review required): " + ", ".join(empty))
    if "\ufffd" in source.text:
        warnings.append("PDF text contains replacement characters; verify source encoding or obtain reviewed OCR.")
    if not rows:
        warnings.append("No requirements extracted; do not treat this inventory as coverage.")
    for row in rows:
        if not row.title or not row.description:
            warnings.append(f"{row.identifier}: missing literal title or description; review the source boundary.")
    counts = Counter(row.identifier for row in rows)
    if any(count > 1 for count in counts.values()):
        warnings.append("Repeated identifiers retained with occurrence-specific keys; resolve ambiguous selection manually.")
        occurrences = Counter()
        unique = []
        for row in rows:
            occurrences[row.identifier] += 1
            unique.append(replace(row, key=f"{row.key}:occurrence-{occurrences[row.identifier]}"))
        rows = unique
    complete = kind == "reference" and not warnings
    if selected_ids:
        unknown = set(selected_ids) - counts.keys()
        ambiguous = {item for item in selected_ids if counts[item] > 1}
        if unknown or ambiguous:
            raise ValueError(f"Invalid selected_ids: unknown={sorted(unknown)}, ambiguous={sorted(ambiguous)}; use exact unambiguous inventory identifiers.")
        rows = [row for row in rows if row.identifier in selected_ids]
        warnings.append("Explicit selected_ids subset applied after full inventory parsing.")
        complete = False
    return RequirementInventory(metadata, tuple(rows), tuple(warnings), complete)
