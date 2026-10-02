"""Literal, page-addressable source requirements; never generated summaries."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from regulatory_poc.types.models import DocumentMetadata


@dataclass(frozen=True)
class Requirement:
    key: str
    identifier: str
    title: str
    description: str
    document_id: str
    source_name: str
    source_hash: str
    page: int
    end_page: int
    language: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Requirement:
        return cls(**{name: value[name] for name in cls.__dataclass_fields__})


@dataclass(frozen=True)
class RequirementInventory:
    document: DocumentMetadata
    requirements: tuple[Requirement, ...]
    warnings: tuple[str, ...]
    complete: bool
