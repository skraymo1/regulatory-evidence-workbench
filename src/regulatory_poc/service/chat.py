from __future__ import annotations

import re
import uuid

from regulatory_poc.repo.agents import ComparisonAgent
from regulatory_poc.repo.conversations import JsonConversationRepository
from regulatory_poc.repo.search import SearchRepository
from regulatory_poc.types.models import (
    ChatMessage,
    ChatRequest,
    ChatResult,
    Citation,
    SearchHit,
)


class RegulatoryChatService:
    def __init__(
        self,
        search_repository: SearchRepository,
        conversation_repository: JsonConversationRepository,
        agent: ComparisonAgent,
    ) -> None:
        self._search = search_repository
        self._conversations = conversation_repository
        self._agent = agent

    async def answer(self, request: ChatRequest) -> ChatResult:
        question = request.question.strip()
        if not question:
            raise ValueError("A question is required.")
        conversation_id = request.conversation_id.strip() or str(uuid.uuid4())
        history = self._conversations.get(conversation_id)
        retrieval_query = " ".join(
            [*(item.content for item in history[-4:] if item.role == "user"), question]
        )
        hits = self._search.search(
            retrieval_query,
            request.top_k,
            filters=request.filters,
        )
        citations = _chat_citations(hits)
        if not hits:
            answer = (
                "Insufficient evidence: no relevant passage was retrieved from the approved "
                "document set. Refine the question or filters."
            )
            sufficient = False
        else:
            answer = await self._agent.answer(_chat_prompt(question, history, hits))
            error = _chat_citation_error(answer, citations)
            sufficient = not error and "insufficient evidence" not in answer.casefold()
            if error:
                answer = f"Chat output failed citation validation: {error}"
        self._conversations.append(
            conversation_id,
            ChatMessage(role="user", content=question),
            ChatMessage(role="assistant", content=answer),
        )
        return ChatResult(
            conversation_id=conversation_id,
            answer=answer,
            citations=citations,
            sufficient_evidence=sufficient,
        )


def _chat_citations(hits: list[SearchHit]) -> tuple[Citation, ...]:
    return tuple(
        Citation(
            citation_id=f"C{index}",
            document_id=hit.chunk.metadata.document_id,
            title=hit.chunk.metadata.title,
            authority=hit.chunk.metadata.authority,
            chunk_ordinal=hit.chunk.ordinal,
            excerpt=hit.chunk.text,
            source_name=hit.chunk.metadata.source_name,
            source_uri=hit.chunk.metadata.source_uri,
            source_hash=hit.chunk.metadata.source_version,
        )
        for index, hit in enumerate(hits, start=1)
    )


def _chat_prompt(
    question: str, history: tuple[ChatMessage, ...], hits: list[SearchHit]
) -> str:
    prior = "\n".join(f"{item.role}: {item.content}" for item in history[-6:]) or "(none)"
    evidence = "\n".join(
        f"[C{index}] {hit.chunk.metadata.title}, chunk {hit.chunk.ordinal}: {hit.chunk.text}"
        for index, hit in enumerate(hits, start=1)
    )
    return "\n".join(
        [
            "QUESTION",
            question,
            "",
            "CONVERSATION HISTORY",
            prior,
            "",
            "EVIDENCE",
            evidence,
            "",
            "RESPONSE REQUIREMENTS",
            "Answer in the language used by the question.",
            "Use only supplied evidence and cite every substantive statement with [C#].",
            "Preserve exact wording when quoting. State insufficient evidence when needed.",
        ]
    )


def _chat_citation_error(answer: str, citations: tuple[Citation, ...]) -> str:
    labels = set(re.findall(r"\[(C\d+)\]", answer))
    if not labels:
        return "the model returned no evidence labels."
    allowed = {item.citation_id for item in citations}
    unsupported = sorted(labels - allowed)
    if unsupported:
        return f"the model referenced unknown labels: {', '.join(unsupported)}."
    uncited = _uncited_claims(answer, r"\[C\d+\]")
    if uncited:
        return f"{len(uncited)} substantive paragraph(s) had no citation."
    return ""


def _uncited_claims(answer: str, citation_pattern: str) -> list[str]:
    claims: list[str] = []
    for paragraph in re.split(r"\n\s*\n", answer):
        text = paragraph.strip()
        if not text or text.startswith("#") or re.search(citation_pattern, text):
            continue
        if "insufficient evidence" in text.casefold():
            continue
        if len(re.findall(r"\w+", text, flags=re.UNICODE)) >= 4:
            claims.append(text)
    return claims
