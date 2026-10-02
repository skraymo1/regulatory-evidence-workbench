"""Reference standard supporting paragraphs, assigned by the publication's own contents ranges.

Each requirement's contents entry lists its numbered paragraphs, e.g. "(6.20–6.21)".
That list, not layout heuristics, decides which paragraphs belong to a requirement.
Paragraph text is literal except that page-number lines and page-bottom footnotes
are removed; both removals are recorded so reviewers can check the original page.
"""

from __future__ import annotations

import re

from regulatory_poc.repo.requirement_source import Source, read_source


_MARKER = re.compile(r"(?m)^(\d+\.\d+[A-Z]?)\.[ \t]")
_HEADING = re.compile(r"(?m)^Requirement ([1-9]\d*):")
_RANGE = re.compile(r"(\d+\.\d+[A-Z]?)(?:\s*[–\-\u2013\ufffd]\s*(\d+\.\d+[A-Z]?))?")


def _contents(text: str) -> tuple[dict[str, list[tuple[str, str]]], int]:
    """Return requirement -> [(first, last)] ranges and the end offset of the contents."""
    headings = list(_HEADING.finditer(text))
    ranges, end = {}, 0
    for index, heading in enumerate(headings):
        limit = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        entry = re.match(r"(.*?)\.{3,}", text[heading.end():min(limit, heading.end() + 300)], re.S)
        if entry is None or f"Requirement {heading[1]}" in ranges:
            continue
        spans = re.findall(r"\(([^()]*\d+\.\d+[^()]*)\)", entry[1])
        ranges[f"Requirement {heading[1]}"] = [
            (first, last or first) for span in spans for first, last in _RANGE.findall(span)
        ]
        end = heading.end() + entry.end()
    return ranges, end


def _clean(source: Source, start: int, end: int) -> tuple[str, list[str]]:
    kept, removed, cursor = [], [], start
    text = source.text
    while cursor < end:
        line_end = text.find("\n", cursor, end)
        line_end = end if line_end < 0 else line_end + 1
        line = text[cursor:line_end]
        stripped = line.strip()
        if re.fullmatch(r"\d{1,3}", stripped):
            removed.append(f"page number {stripped}")
        elif (re.match(r"\d{1,3}[ \t]+[A-Z]", stripped)
              and "".join(kept).rstrip().endswith((".", ":", ";"))):
            page = source.page_at(cursor)
            page_end = source.starts[page] if page < len(source.starts) else len(text)
            removed.append(f"footnote {stripped.split()[0]} on PDF page {page}")
            line_end = max(line_end, min(page_end, end))
        else:
            kept.append(line)
        cursor = line_end
    lines = "".join(kept).rstrip().split("\n")
    # A section heading printed just before the next requirement has no terminal punctuation.
    while len(lines) > 1 and lines[-1].strip() and not re.search(r"[.;:)\]]\s*$", lines[-1]) \
            and len(lines[-1].strip()) < 90:
        removed.append(f"heading '{lines.pop().strip()}'")
    return "\n".join(lines).strip(), removed


def supporting_paragraphs(content: bytes) -> tuple[dict[str, list[dict]], list[str]]:
    """Return requirement identifier -> literal numbered paragraphs, plus warnings."""
    source = read_source(content)
    text = source.text
    ranges, body_start = _contents(text)
    stops = sorted({m.start() for m in _HEADING.finditer(text) if m.start() >= body_start}
                   | {m.start() for m in re.finditer(r"(?m)^[A-Z][A-Z ,:()–/-]{5,}$", text)
                      if m.start() >= body_start})
    markers = [m for m in _MARKER.finditer(text) if m.start() >= body_start]
    order, paragraphs = [], {}
    for index, marker in enumerate(markers):
        limit = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        limit = min([stop for stop in stops if marker.start() < stop < limit] or [limit])
        if marker[1] in paragraphs:
            continue
        literal, removed = _clean(source, marker.start(), limit)
        order.append(marker[1])
        paragraphs[marker[1]] = {
            "identifier": marker[1], "text": literal,
            "page": source.page_at(marker.start()),
            "end_page": source.page_at(max(marker.start(), limit - 1)),
            "removed": removed,
        }
    position = {identifier: index for index, identifier in enumerate(order)}
    result, warnings = {}, []
    for requirement, spans in ranges.items():
        selected = []
        for first, last in spans:
            if first not in position or last not in position:
                warnings.append(f"{requirement}: paragraphs {first}–{last} listed in contents were not found.")
                continue
            selected.extend(order[position[first]:position[last] + 1])
        result[requirement] = [paragraphs[identifier] for identifier in dict.fromkeys(selected)]
    missing = sorted(set(f"Requirement {n}" for n in range(1, 83)) - ranges.keys(),
                     key=lambda value: int(value.split()[-1]))
    if missing:
        warnings.append("Contents entries not found: " + ", ".join(missing))
    return result, warnings
