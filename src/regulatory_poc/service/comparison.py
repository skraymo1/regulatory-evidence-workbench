from __future__ import annotations

import re

from regulatory_poc.repo.agents import ComparisonAgent
from regulatory_poc.repo.search import SearchRepository
from regulatory_poc.types.models import (
    Citation,
    ComparisonRequest,
    ComparisonResult,
    SearchHit,
)


class RegulatoryComparisonService:
    def __init__(
        self,
        search_repository: SearchRepository,
        comparison_agent: ComparisonAgent,
        require_revision: bool = False,
    ) -> None:
        self._search = search_repository
        self._agent = comparison_agent
        self._require_revision = require_revision

    async def compare(self, request: ComparisonRequest) -> ComparisonResult:
        question = request.question.strip()
        if not question:
            raise ValueError("A comparison focus is required.")
        if request.document_a_id == request.document_b_id:
            raise ValueError("Choose two different language editions for comparison.")
        documents = {item.document_id: item for item in self._search.list_documents()}
        if request.document_a_id not in documents or request.document_b_id not in documents:
            raise ValueError("Both selected language editions must be indexed.")
        edition_a = documents[request.document_a_id]
        edition_b = documents[request.document_b_id]
        publication_a = edition_a.standard.strip().casefold()
        publication_b = edition_b.standard.strip().casefold()
        if not publication_a or publication_a != publication_b:
            raise ValueError(
                "Choose two editions with the same publication identifier."
            )
        revision_a, revision_b = (item.revision.strip().casefold() for item in (edition_a, edition_b))
        if revision_a != revision_b or (self._require_revision and not revision_a):
            raise ValueError("Both editions must specify the same publication revision.")
        if any(item.language.strip().casefold() in {"", "unknown"} for item in (edition_a, edition_b)):
            raise ValueError("Both editions must specify a known language.")
        if edition_a.language.strip().casefold() == edition_b.language.strip().casefold():
            raise ValueError("Choose editions in two different languages.")

        edition_a_hits = self._search.search(
            question, request.top_k, request.document_a_id
        )
        edition_b_hits = self._search.search(
            question, request.top_k, request.document_b_id
        )
        citations = _citations(edition_a_hits, edition_b_hits)
        if not edition_a_hits or not edition_b_hits:
            missing = []
            if not edition_a_hits:
                missing.append(f"{edition_a.language} edition")
            if not edition_b_hits:
                missing.append(f"{edition_b.language} edition")
            return ComparisonResult(
                answer=(
                    "Insufficient evidence: no relevant passage was retrieved from "
                    f"{' and '.join(missing)}. Refine the focus or verify document ingestion."
                ),
                citations=citations,
                sufficient_evidence=False,
            )

        prompt = _build_prompt(
            question,
            edition_a.language,
            edition_b.language,
            edition_a_hits,
            edition_b_hits,
        )
        answer = await self._agent.compare(prompt)
        if "insufficient evidence" in answer.casefold() and not re.search(
            r"\[([AB]\d+)\]", answer
        ):
            return ComparisonResult(
                answer=answer,
                citations=citations,
                sufficient_evidence=False,
            )
        citation_error = _citation_error(answer, citations)
        if citation_error:
            return ComparisonResult(
                answer=f"Comparison output failed citation validation: {citation_error}",
                citations=citations,
                sufficient_evidence=False,
            )
        return ComparisonResult(
            answer=answer,
            citations=citations,
            sufficient_evidence="insufficient evidence" not in answer.casefold(),
        )

def _citations(
    edition_a_hits: list[SearchHit], edition_b_hits: list[SearchHit]
) -> tuple[Citation, ...]:
    citations: list[Citation] = []
    for prefix, hits in (("A", edition_a_hits), ("B", edition_b_hits)):
        for index, hit in enumerate(hits, start=1):
            chunk = hit.chunk
            citations.append(
                Citation(
                    citation_id=f"{prefix}{index}",
                    document_id=chunk.metadata.document_id,
                    title=chunk.metadata.title,
                    authority=chunk.metadata.authority,
                    chunk_ordinal=chunk.ordinal,
                    excerpt=chunk.text,
                    source_name=chunk.metadata.source_name,
                    source_uri=chunk.metadata.source_uri,
                    source_hash=chunk.metadata.source_version,
                )
            )
    return tuple(citations)


def _build_prompt(
    question: str,
    language_a: str,
    language_b: str,
    edition_a_hits: list[SearchHit],
    edition_b_hits: list[SearchHit],
) -> str:
    return "\n".join(
        [
            "QUESTION",
            question,
            "",
            f"EDITION A EVIDENCE ({language_a})",
            _format_hits("A", edition_a_hits),
            "",
            f"EDITION B EVIDENCE ({language_b})",
            _format_hits("B", edition_b_hits),
            "",
            "RESPONSE REQUIREMENTS",
            "1. Assess whether both language editions communicate equivalent regulatory meaning.",
            "2. Identify preserved meaning, omissions, additions, mistranslations, changed numbers or "
            "units, and differences in obligation strength.",
            "3. Distinguish wording differences from substantive meaning changes.",
            "4. Cite every substantive statement with labels such as [A1] or [B1].",
            "   Every non-heading paragraph and bullet must contain at least one valid A/B label; "
            "do not add an uncited introduction, conclusion, or summary.",
            "5. Quote both editions when precision matters and explain the meaning in the language "
            "used by the question.",
            "6. State 'insufficient evidence' when the retrieved passages are not corresponding or "
            "do not support a conclusion.",
            "7. Flag every potential discrepancy for bilingual regulatory expert review.",
            "8. Unaligned top-k passages cannot prove omissions or additions. A relational "
            "conclusion must cite corresponding A AND B passages; otherwise state insufficient evidence.",
        ]
    )


def _format_hits(prefix: str, hits: list[SearchHit]) -> str:
    return "\n".join(
        f"[{prefix}{index}] {hit.chunk.metadata.title}, "
        f"chunk {hit.chunk.ordinal}: {hit.chunk.text}"
        for index, hit in enumerate(hits, start=1)
    )


def _citation_error(answer: str, citations: tuple[Citation, ...]) -> str:
    labels = set(re.findall(r"\[([AB]\d+)\]", answer))
    if not labels:
        return "the model returned no evidence labels."
    allowed = {citation.citation_id for citation in citations}
    unsupported = sorted(labels - allowed)
    if unsupported:
        return f"the model referenced unknown labels: {', '.join(unsupported)}."
    if "insufficient evidence" not in answer.casefold() and not all(
        any(label.startswith(side) for label in labels) for side in ("A", "B")
    ):
        return "a fidelity conclusion must cite both language editions."
    uncited = _uncited_claims(answer)
    if uncited:
        return f"{len(uncited)} substantive paragraph(s) had no citation."
    return ""


def _uncited_claims(answer: str) -> list[str]:
    claims: list[str] = []
    for paragraph in re.split(r"\n\s*\n", answer):
        text = paragraph.strip()
        if not text or text.startswith("#") or re.search(r"\[[AB]\d+\]", text):
            continue
        if (
            "insufficient evidence" in text.casefold()
            or text.startswith("This offline result")
        ):
            continue
        if len(re.findall(r"\w+", text, flags=re.UNICODE)) >= 4:
            claims.append(text)
    return claims
