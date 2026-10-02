from __future__ import annotations

import hashlib
from dataclasses import replace
from azure.core.exceptions import AzureError

from regulatory_poc.repo.documents import chunk_document, make_document_id
from regulatory_poc.repo.blob import BlobDocumentStore
from regulatory_poc.repo.regulatory_store import RecordStore, SourceStore
from regulatory_poc.repo.requirement_extraction import extract_requirements
from regulatory_poc.repo.requirement_paragraphs import supporting_paragraphs
from regulatory_poc.repo.search import SearchRepository
from regulatory_poc.types.models import DocumentMetadata
from regulatory_poc.types.requirements import Requirement


ROLES = ("Reference baseline", "National regulation", "Translation reference")
KINDS = {
    "Reference baseline": "reference", "UK": "uk", "Korea": "korea",
    "Argentina": "argentina", "Korean English reference": "korea",
}
EXTRACTION_VERSION = "requirement-inventory-v2"
PARAGRAPH_VERSION = "reference-supporting-paragraphs-v1"


def document_role(record: dict) -> str:
    role = record.get("role", "")
    if role in {"UK", "Korea", "Argentina"}:
        return "National regulation"
    return "Translation reference" if role == "Korean English reference" else role


def document_jurisdiction(record: dict) -> str:
    role = record.get("role", "")
    if role == "Reference baseline":
        return ""
    if record.get("jurisdiction"):
        return record["jurisdiction"].strip()
    explicit = record.get("metadata", {}).get("country", "")
    if explicit and explicit not in ROLES and explicit != "Korean English reference":
        return explicit.strip()
    return "Korea" if role == "Korean English reference" else role if role in KINDS else ""


class RegulatoryLibrary:
    def __init__(self, state: RecordStore, sources: SourceStore, search: SearchRepository) -> None:
        self.state, self.sources, self.search = state, sources, search

    def list_documents(self) -> list[dict]:
        return self.state.list("regulatory-documents/")

    def get(self, document_id: str) -> dict:
        if not document_id or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in document_id):
            raise ValueError("Invalid document identifier.")
        record = self.state.get(f"regulatory-documents/{document_id}.json")
        if record is None:
            raise ValueError("Regulatory document not found; import it in Comparison sources.")
        return record

    def requirements(self, document_id: str) -> tuple[Requirement, ...]:
        return tuple(Requirement.from_dict(item) for item in self.get(document_id)["requirements"])

    def supporting_paragraphs(self, document_id: str) -> tuple[dict[str, list[dict]], list[str]]:
        """Numbered reference-profile paragraphs, extracted once from the stored original and cached."""
        record = self.get(document_id)
        if record["role"] != "Reference baseline":
            return {}, []
        key = f"regulatory-paragraphs/{document_id}.json"
        cached = self.state.get(key)
        version = record["metadata"].get("source_version", "")
        if cached and cached.get("version") == PARAGRAPH_VERSION and cached.get("source_version") == version:
            return cached["paragraphs"], cached["warnings"]
        try:
            content, _ = self.sources.download_version(DocumentMetadata.from_dict(record["metadata"]))
        except (ValueError, OSError, AzureError) as exc:
            return {}, [f"Supporting paragraphs unavailable ({exc}); only high-level statements are compared."]
        paragraphs, warnings = supporting_paragraphs(content)
        self.state.put(key, {"version": PARAGRAPH_VERSION, "source_version": version,
                             "paragraphs": paragraphs, "warnings": warnings})
        return paragraphs, warnings

    def import_document(
        self, content: bytes, name: str, role: str, language: str,
        standard: str = "", revision: str = "",
        jurisdiction: str = "", extraction_profile: str = "auto",
    ) -> dict:
        if role not in {*ROLES, *KINDS}:
            raise ValueError("Select a supported document role.")
        language, jurisdiction = language.strip(), jurisdiction.strip()
        if role in {"National regulation", "Translation reference"} and not language:
            raise ValueError("Supply the source language as a nonempty label.")
        if role == "National regulation" and not jurisdiction:
            raise ValueError("Supply a jurisdiction for the national regulation.")
        jurisdiction = jurisdiction or document_jurisdiction({"role": role})
        if role == "Reference baseline" and (standard != "Reference standard" or revision != "Rev. 1"):
            raise ValueError("The current complete inventory supports Reference standard Rev. 1 only.")
        metadata = DocumentMetadata(
            document_id=make_document_id(name, content), title=name,
            authority="reference" if role == "Reference baseline" else role,
            country="" if role == "Reference baseline" else jurisdiction,
            language=language, standard=standard, revision=revision, source_name=name,
        )
        profile = KINDS.get(role, "auto") if extraction_profile == "auto" else extraction_profile
        existing = self.state.get(f"regulatory-documents/{metadata.document_id}.json")
        if existing is not None:
            fields = ("source_name", "language", "standard", "revision")
            same_metadata = all(existing["metadata"].get(field, "") == getattr(metadata, field)
                                for field in fields)
            existing_profile = existing.get("extraction_profile", KINDS.get(existing["role"], "auto"))
            if (existing["role"] != role or document_jurisdiction(existing) != metadata.country
                    or not same_metadata or existing_profile != profile):
                raise ValueError(
                    "This file is already imported with different metadata, role or extraction profile. "
                    "Use the saved source; changes require an explicit reviewed migration, not re-import."
                )
            return existing if existing.get("indexing_status") == "indexed" else self.retry_index(metadata.document_id)
        inventory = extract_requirements(content, metadata, kind=profile)
        if not inventory.requirements:
            raise ValueError("No requirements extracted. Verify the document format before importing.")
        metadata = self.sources.save(content, metadata)
        record = {
            "metadata": metadata.to_dict(), "role": role, "jurisdiction": metadata.country,
            "extraction_profile": profile,
            "requirements": [item.to_dict() for item in inventory.requirements],
            "warnings": list(inventory.warnings), "complete": inventory.complete,
            "extraction_version": EXTRACTION_VERSION,
            "indexing_status": "pending", "approval_status": "unapproved",
        }
        self.state.put(f"regulatory-documents/{metadata.document_id}.json", record)
        return self.retry_index(metadata.document_id)

    def retry_index(self, document_id: str) -> dict:
        record = self.get(document_id)
        metadata = DocumentMetadata.from_dict(record["metadata"])
        chunks = []
        for requirement in self.requirements(document_id):
            prefix = f"REQKEY:{requirement.key}\n"
            for chunk in chunk_document(requirement.title + "\n" + requirement.description, metadata):
                key = hashlib.sha256(f"{requirement.key}:{chunk.ordinal}".encode()).hexdigest()
                chunks.append(replace(chunk, chunk_id=key, text=prefix + chunk.text))
        try:
            # Bound each SDK upload request; individual vector calls remain metered.
            for start in range(0, len(chunks), 100):
                self.search.upsert(chunks[start:start + 100])
        except (RuntimeError, ValueError, OSError, AzureError) as exc:
            record["indexing_error"] = str(exc)
            self.state.put(f"regulatory-documents/{document_id}.json", record)
            raise RuntimeError("Source saved, but indexing failed. Retry in Comparison sources.") from exc
        record["indexing_status"] = "indexed"
        record.pop("indexing_error", None)
        self.state.put(f"regulatory-documents/{document_id}.json", record)
        if isinstance(self.sources, BlobDocumentStore):
            self.sources.indexed(document_id)
        return record
