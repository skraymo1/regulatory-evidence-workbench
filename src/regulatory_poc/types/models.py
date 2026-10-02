from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class DocumentMetadata:
    document_id: str
    title: str
    authority: str
    language: str = "unknown"
    country: str = ""
    reactor_type: str = ""
    standard: str = ""
    project_number: str = ""
    source_name: str = ""
    source_kind: str = "upload"
    source_uri: str = ""
    source_version: str = ""
    revision: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DocumentMetadata:
        return cls(**{key: str(value.get(key) or "") for key in cls.__dataclass_fields__})


@dataclass(frozen=True)
class DocumentChunk:
    chunk_id: str
    text: str
    ordinal: int
    metadata: DocumentMetadata

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "text": self.text,
            "ordinal": self.ordinal,
            "metadata": self.metadata.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DocumentChunk:
        return cls(
            chunk_id=str(value["chunk_id"]),
            text=str(value["text"]),
            ordinal=int(value["ordinal"]),
            metadata=DocumentMetadata.from_dict(value["metadata"]),
        )


@dataclass(frozen=True)
class SearchHit:
    chunk: DocumentChunk
    score: float


@dataclass(frozen=True)
class SearchFilters:
    document_ids: tuple[str, ...] = field(default_factory=tuple)
    countries: tuple[str, ...] = field(default_factory=tuple)
    reactor_types: tuple[str, ...] = field(default_factory=tuple)
    languages: tuple[str, ...] = field(default_factory=tuple)
    standards: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SourceDocument:
    source_id: str
    source_kind: str
    source_uri: str
    source_name: str
    version: str
    content: bytes
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SyncResult:
    source: str
    discovered: int
    indexed: int
    skipped: int
    failed: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Citation:
    citation_id: str
    document_id: str
    title: str
    authority: str
    chunk_ordinal: int
    excerpt: str
    source_name: str
    source_uri: str = ""
    source_hash: str = ""


@dataclass(frozen=True)
class ComparisonRequest:
    question: str
    document_a_id: str
    document_b_id: str
    top_k: int = 5


@dataclass(frozen=True)
class ComparisonResult:
    answer: str
    citations: tuple[Citation, ...] = field(default_factory=tuple)
    sufficient_evidence: bool = False
    report_id: str = ""
    indexing_status: str = ""
    review_status: str = "unreviewed"
    cached: bool = False


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


@dataclass(frozen=True)
class ChatRequest:
    question: str
    conversation_id: str = ""
    filters: SearchFilters = field(default_factory=SearchFilters)
    top_k: int = 8


@dataclass(frozen=True)
class ChatResult:
    conversation_id: str
    answer: str
    citations: tuple[Citation, ...] = field(default_factory=tuple)
    sufficient_evidence: bool = False
