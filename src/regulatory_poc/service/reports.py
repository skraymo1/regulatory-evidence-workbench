from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from datetime import datetime, timezone

from regulatory_poc.repo.prompt_agents import fidelity_rules
from regulatory_poc.repo.documents import chunk_document
from regulatory_poc.types.models import (
    Citation, ComparisonResult, DocumentChunk, DocumentMetadata,
)


class SavedComparisonService:
    def __init__(self, comparison, sources, store, report_search, settings) -> None:
        self._comparison, self._sources = comparison, sources
        self.store, self.search, self.settings = store, report_search, settings

    async def compare(self, request) -> ComparisonResult:
        docs = {item.document_id: item for item in self._sources.list_documents()}
        if request.document_a_id not in docs or request.document_b_id not in docs:
            raise ValueError("Both selected editions must be indexed before comparing.")
        rules = fidelity_rules()
        inputs = {
            "request": asdict(request),
            "sources": [docs[key].to_dict() for key in (
                request.document_a_id, request.document_b_id
            )],
            "rules_version": rules["version"],
            "prompt_sha256": hashlib.sha256(
                json.dumps(rules, sort_keys=True).encode()
            ).hexdigest(),
            "agent_name": self.settings.agent_name,
            "agent_version": self.settings.agent_version,
            "model": self.settings.model_deployment_name,
            "model_version": self.settings.model_version,
            "evidence_policy_version": "paired-iq-v3",
        }
        report_id = hashlib.sha256(
            json.dumps(inputs, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        saved = self.store.get(f"reports/{report_id}.json")
        if saved:
            return self._result(saved, cached=True)
        result = await self._comparison.compare(request)
        record = {
            "report_id": report_id, "schema_version": 1, "inputs": inputs,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "review_status": "unreviewed", "indexing_status": "pending",
            "result": asdict(result), "indexing_error": "",
        }
        self.store.put(f"reports/{report_id}.json", record)
        return self.retry_index(report_id)

    def list_reports(self) -> list[dict]:
        return self.store.list("reports/")

    def get(self, report_id: str) -> dict:
        if not report_id or any(char not in "0123456789abcdef" for char in report_id):
            raise ValueError("Invalid report identifier.")
        record = self.store.get(f"reports/{report_id}.json")
        if record is None:
            raise ValueError("Saved report not found.")
        return record

    def retry_index(self, report_id: str) -> ComparisonResult:
        record = self.get(report_id)
        request = record["inputs"]["request"]
        # Report evidence retains the complete original A/B provenance in Blob and index text.
        text = json.dumps({
            "report_id": report_id, "review_status": "unreviewed",
            "focus": request["question"], "answer": record["result"]["answer"],
            "original_citations": record["result"]["citations"],
        }, ensure_ascii=False)
        metadata = DocumentMetadata(
            document_id=report_id, title=f"Unreviewed fidelity: {request['question'][:100]}",
            authority="Generated report; not authoritative source",
            source_kind="report", source_name=f"{report_id}.json",
            source_uri=f"{self.settings.blob_account_url}/{self.settings.report_container}"
            f"/reports/{report_id}.json",
            source_version=report_id, standard=record["inputs"]["sources"][0]["standard"],
            revision=record["inputs"]["sources"][0].get("revision", ""),
        )
        provenance = json.dumps({
            "report_id": report_id,
            "original_sources": [{
                key: source[key] for key in (
                    "document_id", "language", "source_uri", "source_version",
                )
            } for source in record["inputs"]["sources"]],
        }, ensure_ascii=False)
        chunks = [
            replace(chunk, text=f"{provenance}\n{chunk.text}")
            for chunk in chunk_document(text, metadata)
        ]
        try:
            self.search.delete_document(report_id)
            self.search.upsert(chunks)
            record["indexing_status"] = "indexed"
            record["indexing_error"] = ""
        except Exception as exc:
            # Durable answer must survive any index/embedding outage without model regeneration.
            record["indexing_status"] = "pending"
            record["indexing_error"] = f"{type(exc).__name__}: retry report indexing."
        self.store.put(f"reports/{report_id}.json", record)
        return self._result(record)

    @staticmethod
    def _result(record: dict, cached: bool = False) -> ComparisonResult:
        payload = dict(record["result"])
        payload["citations"] = tuple(Citation(**item) for item in payload["citations"])
        return replace(
            ComparisonResult(**payload), report_id=record["report_id"],
            indexing_status=record["indexing_status"], review_status="unreviewed", cached=cached,
        )
