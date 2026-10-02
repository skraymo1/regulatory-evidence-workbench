"""PDF source stream: unmodified pypdf page text joined by one newline.

Offsets reference that stream, including running headers and footers. We never
remove internal whitespace or repair hyphenation inside a quoted span.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from io import BytesIO

from pypdf import PdfReader


@dataclass(frozen=True)
class Source:
    text: str
    starts: tuple[int, ...]
    pages: tuple[str, ...]
    bold: tuple[tuple[int, int], ...]

    def page_at(self, offset: int) -> int:
        return bisect_right(self.starts, offset)


def read_source(content: bytes) -> Source:
    if not content.lstrip().startswith(b"%PDF-"):
        raise ValueError("Unsupported document format; provide a text-bearing PDF (DOCX is not supported).")
    try:
        reader = PdfReader(BytesIO(content))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("Encrypted PDF; provide an unlocked PDF.")
        pages, starts, bold = [], [], []
        offset = 0
        for page in reader.pages:
            fragments = []

            def visit(text, _cm, _tm, font, _size):
                fragments.append((text, "bold" in str((font or {}).get("/BaseFont", "")).lower()))

            text = page.extract_text(visitor_text=visit) or ""
            starts.append(offset)
            pages.append(text)
            cursor = 0
            for fragment, is_bold in fragments:
                if not fragment:
                    continue
                found = text.find(fragment, cursor)
                if found < 0:
                    continue
                end = found + len(fragment)
                if is_bold:
                    if bold and not text[max(0, bold[-1][1] - offset):found].strip() and bold[-1][1] >= offset:
                        bold[-1] = (bold[-1][0], offset + end)
                    else:
                        bold.append((offset + found, offset + end))
                cursor = end
            offset += len(text) + 1
        return Source("\n".join(pages), tuple(starts), tuple(pages), tuple(bold))
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Cannot read PDF; provide a valid, unlocked text-bearing PDF.") from exc
