from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from dataclasses import replace
from azure.core.exceptions import AzureError

from regulatory_poc.repo.agents import ComparisonAgent
from regulatory_poc.repo.documents import chunk_document
from regulatory_poc.repo.regulatory_store import RecordStore
from regulatory_poc.repo.search import SearchRepository
from regulatory_poc.service.regulatory_library import RegulatoryLibrary, document_jurisdiction, document_role
from regulatory_poc.service.regulatory_scope import national_requirements, reference_pairs, report_reference_pairs, selected_keys
from regulatory_poc.service.regulatory_coverage import national_rows
from regulatory_poc.service.regulatory_mapping import map_requirement
from regulatory_poc.service.translation import TRANSLATION_RULES, verify_translation
from regulatory_poc.types.models import DocumentMetadata, SearchFilters
from regulatory_poc.types.requirements import Requirement


RULE_VERSION = "regulatory-table-v5"
MODEL_TIMEOUT_SECONDS = 180


class RegulatoryTableService:
    def __init__(
        self, library: RegulatoryLibrary, reports: RecordStore,
        agent: ComparisonAgent | None, rules: dict, model_identity: dict,
        report_search: SearchRepository, translation_agent: ComparisonAgent | None = None,
    ) -> None:
        self.library, self.reports, self.agent = library, reports, agent
        self.rules, self.model_identity, self.report_search = rules, model_identity, report_search
        self.translation_agent = translation_agent

    def create(
        self, baseline_id: str, country: str, document_ids: list[str],
        selected_ids: list[str] | None = None, reference_id: str = "",
        reference_source_id: str = "",
    ) -> dict:
        baseline = self.library.get(baseline_id)
        if baseline["role"] != "Reference baseline" or not baseline["complete"]:
            raise ValueError("A complete Reference standard Rev. 1 inventory is required; inspect extraction warnings.")
        country = country.strip()
        if not country or not document_ids:
            raise ValueError("Supply a jurisdiction and at least one national document.")
        documents = [self.library.get(key) for key in sorted(set(document_ids))]
        if any(document_role(item) != "National regulation" or document_jurisdiction(item) != country
               for item in documents):
            raise ValueError("All national documents must belong to the selected jurisdiction.")
        if any(item["indexing_status"] != "indexed" for item in documents):
            raise ValueError("Finish source indexing before creating the comparison.")
        requirements = [
            requirement for item in documents
            for requirement in self.library.requirements(item["metadata"]["document_id"])
        ]
        selected = selected_keys(requirements, selected_ids or [], {
            item["metadata"]["document_id"] for item in documents
            if item["role"] in {"UK", "Korea", "Argentina"}
        })
        if reference_source_id and not reference_id:
            raise ValueError("Supply a translation reference before selecting its source association.")
        if reference_id:
            reference = self.library.get(reference_id)
            if document_role(reference) != "Translation reference":
                raise ValueError("Select a document with the Translation reference role.")
            source_ids = {item["metadata"]["document_id"] for item in documents}
            if not reference_source_id and len(source_ids) == 1:
                reference_source_id = next(iter(source_ids))
            if reference_source_id not in source_ids:
                raise ValueError("Reference source association is ambiguous; select one national document.")
            pairs = reference_pairs(requirements, list(self.library.requirements(reference_id)), reference_source_id)
            if selected and not set(selected).intersection(pairs):
                raise ValueError("No selected source requirements have a same-identifier reference pairing.")
            documents.append(reference)
        paragraphs, paragraph_warnings = self.library.supporting_paragraphs(baseline_id)
        inputs = {
            "baseline": baseline["metadata"], "country": country,
            "sources": [item["metadata"] for item in documents],
            "selected_ids": selected, "reference_id": reference_id,
            "reference_source_id": reference_source_id,
            "rules": self.rules, "schema_version": RULE_VERSION,
            "translation_rules": TRANSLATION_RULES,
            "model": self.model_identity,
            "inventory_digest": hashlib.sha256(json.dumps(
                [baseline["requirements"], *[item["requirements"] for item in documents]],
                sort_keys=True, ensure_ascii=False,
            ).encode()).hexdigest(),
            "paragraph_digest": hashlib.sha256(json.dumps(
                paragraphs, sort_keys=True, ensure_ascii=False,
            ).encode()).hexdigest(),
        }
        report_id = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
        saved = self.reports.get(f"regulatory-reports/{report_id}.json")
        if saved:
            return self.get(report_id)
        record = {
            "report_id": report_id, "report_type": "regulatory-table",
            "inputs": inputs, "review_status": "unreviewed",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "indexing_status": "not_started",
            "warnings": [
                warning for item in [baseline, *documents] for warning in item["warnings"]
            ] + paragraph_warnings + ([
                "Source and translation reference may be different revisions. Matching article "
                "numbers alone do not establish equivalent editions; review dates and amendments."
            ] if reference_id else []) + ([
                "Offline mode retrieves candidates lexically; it is not cross-language semantic "
                "matching and performs no comparison. Empty results are not proof of absence."
            ] if self.agent is None else [
                "Each processed row screens every selected national clause against the full reference "
                "requirement, then re-screens for partial or missing topics. Coverage is limited to "
                "the supplied documents; cited instruments that were not supplied are listed per row."
            ]),
            "inventories": {
                item["metadata"]["document_id"]: item["requirements"] for item in documents
            },
            "rows": [
                {"baseline": item.to_dict(), "status": "pending", "matches": [],
                 "candidates": [], "note": "", "translation_checks": {},
                 "paragraphs": paragraphs.get(item.identifier, [])}
                for item in self.library.requirements(baseline_id)
            ],
        }
        self._save(record)
        return self.get(report_id)

    def get(self, report_id: str) -> dict:
        if len(report_id) != 64 or any(char not in "0123456789abcdef" for char in report_id):
            raise ValueError("Invalid comparison identifier.")
        result = self.reports.get(f"regulatory-reports/{report_id}.json")
        if result is None:
            raise ValueError("Comparison not found; create a country table first.")
        updates = {
            item["baseline"]["key"]: item
            for item in self.reports.list(f"regulatory-rows/{report_id}/")
        }
        checks = {
            (item["requirement_key"], item["article_key"]): item["result"]
            for item in self.reports.list(f"regulatory-translations/{report_id}/")
        }
        result["article_checks"] = {
            **result.get("article_checks", {}),
            **{
            item["article_key"]: item["result"]
            for item in self.reports.list(f"regulatory-article-checks/{report_id}/")
            },
        }
        result["rows"] = [updates.get(row["baseline"]["key"], row) for row in result["rows"]]
        for row in result["rows"]:
            row["translation_checks"] = {
                **row.get("translation_checks", {}),
                **{
                article_key: value for (requirement_key, article_key), value in checks.items()
                if requirement_key == row["baseline"]["key"]
                },
            }
        result["national_rows"] = national_rows(result)
        indexed = self.reports.get(f"regulatory-index-state/{report_id}.json")
        if indexed:
            result["indexing_status"] = (
                "indexed" if indexed.get("digest") == _row_digest(result) else "pending"
            )
            if indexed.get("error"):
                result["indexing_error"] = indexed["error"]
        elif updates:
            result["indexing_status"] = "pending"
        return result

    def _save(self, report: dict) -> None:
        self.reports.put(f"regulatory-reports/{report['report_id']}.json", report)

    def list_reports(self) -> list[dict]:
        return [self.get(item["report_id"]) for item in self.reports.list("regulatory-reports/")]

    def _save_row(self, report_id: str, row: dict) -> None:
        key = hashlib.sha256(row["baseline"]["key"].encode()).hexdigest()
        self.reports.put(f"regulatory-rows/{report_id}/{key}.json", row)

    def _national(self, report: dict) -> list[Requirement]:
        return national_requirements(report)

    def _clauses(self, report: dict) -> list[dict]:
        national = self._national(report)
        reference = report_reference_pairs(report, national)
        clauses = []
        for requirement in national:
            item = requirement.to_dict()
            item["english_reference"] = (
                reference[requirement.key].to_dict() if requirement.key in reference else None
            )
            clauses.append(item)
        return clauses

    def _candidates(self, row: dict, report: dict) -> list[dict]:
        """Offline lexical candidates: one query per statement and supporting paragraph."""
        clauses = {item["key"]: item for item in self._clauses(report)}
        reference_keys = {
            item.key: key for key, item in report_reference_pairs(report, self._national(report)).items()
        }
        baseline = row["baseline"]
        queries = [baseline["title"] + "\n" + baseline["description"]] + [
            item["text"] for item in row.get("paragraphs", [])
        ]
        filters = SearchFilters(document_ids=tuple(source["document_id"] for source in report["inputs"]["sources"]))
        results = [self.library.search.search(query, 50, filters=filters) for query in queries]
        candidates, seen = [], set()
        for rank in range(50):
            for hits in results:
                if rank >= len(hits):
                    continue
                match = re.match(r"REQKEY:([^\n]+)\n", hits[rank].chunk.text)
                key = match and (match[1] if match[1] in clauses else reference_keys.get(match[1]))
                if key and key not in seen:
                    seen.add(key)
                    candidates.append(clauses[key])
        return candidates[:30]

    async def process_row(self, report_id: str, key: str) -> dict:
        report = self.get(report_id)
        row = next((item for item in report["rows"] if item["baseline"]["key"] == key), None)
        if row is None:
            raise ValueError("Reference requirement not found in this table.")
        if row["status"] not in {"pending", "error"}:
            return report
        try:
            if self.agent is None:
                candidates = self._candidates(row, report)
                row.update(candidates=candidates, status="candidates_only" if candidates else "unresolved",
                           note=("Offline retrieval only; no technical mapping performed." if candidates else
                                 "No candidate retrieved within the selected scope; not proof of absence."))
            else:
                result = await map_requirement(
                    self.agent, self.rules, row, self._clauses(report),
                    [source["source_name"] for source in report["inputs"]["sources"]],
                    MODEL_TIMEOUT_SECONDS,
                )
                outcome = result.pop("outcome")
                row.update(result, status={
                    "proposed": "proposed", "no_comparable": "no_comparable",
                }.get(outcome, "unresolved"))
        except (RuntimeError, ValueError, OSError, AzureError) as exc:
            message = (
                f"Model request exceeded {MODEL_TIMEOUT_SECONDS} seconds; no result was saved for this row."
                if isinstance(exc, TimeoutError) else str(exc)
            )
            row.update(status="error", note=message)
            self._save_row(report_id, row)
            raise RuntimeError(f"Requirement processing failed: {message} Retry the saved table.") from exc
        self._save_row(report_id, row)
        return self.get(report_id)

    async def verify_scope_article(self, report_id: str, article_key: str) -> dict:
        report = self.get(report_id)
        inputs = report["inputs"]
        if not inputs["reference_id"]:
            raise ValueError("Select a table with a translation reference.")
        national = self._national(report)
        source = next((item for item in national if item.key == article_key), None)
        if source is None:
            raise ValueError("The article is outside the selected national scope.")
        reference = report_reference_pairs(report, national).get(article_key)
        if reference is None:
            raise ValueError("No associated same-identifier reference exists; confirm source association and alignment.")
        if article_key not in report["article_checks"]:
            result = await verify_translation(self.translation_agent, source, reference)
            key = hashlib.sha256(article_key.encode()).hexdigest()
            self.reports.put(f"regulatory-article-checks/{report_id}/{key}.json",
                             {"article_key": article_key, "result": result})
        return self.get(report_id)

    async def verify_article(self, report_id: str, requirement_key: str, article_key: str) -> dict:
        report = self.get(report_id)
        row = next((item for item in report["rows"] if item["baseline"]["key"] == requirement_key), None)
        if row is None:
            raise ValueError("Reference requirement not found.")
        source = next((item for item in row["candidates"] if item["key"] == article_key), None)
        reference = report_reference_pairs(report, self._national(report)).get(article_key)
        if not source or reference is None:
            raise ValueError("No associated translation reference is available for this article.")
        if article_key not in row["translation_checks"]:
            result = await verify_translation(
                self.translation_agent, Requirement.from_dict(source),
                reference,
            )
            key = hashlib.sha256(f"{requirement_key}:{article_key}".encode()).hexdigest()
            self.reports.put(f"regulatory-translations/{report_id}/{key}.json", {
                "requirement_key": requirement_key, "article_key": article_key, "result": result,
            })
        return self.get(report_id)

    def index_report(self, report_id: str) -> dict:
        report = self.get(report_id)
        country = report["inputs"]["country"]
        text = json.dumps({
            "report_type": "Unreviewed regulatory mapping, NOT authoritative regulation",
            "country": country, "report_id": report_id, "rows": report["rows"],
            "national_rows": report["national_rows"],
            "article_translation_checks": report["article_checks"],
        }, ensure_ascii=False)
        metadata = DocumentMetadata(
            document_id=report_id, title=f"Reference standard vs {country} (unreviewed)",
            authority="Generated report; not authoritative source", country=country,
            standard="Reference standard", revision="Rev. 1", source_kind="report",
            source_name=f"{report_id}.json", source_version=report_id,
        )
        try:
            self.report_search.delete_document(report_id)
            provenance = json.dumps({
                "report_id": report_id, "country": country, "review_status": "unreviewed",
                "original_sources": [report["inputs"]["baseline"], *report["inputs"]["sources"]],
            }, ensure_ascii=False)
            chunks = [
                replace(chunk, text=provenance + "\n" + chunk.text)
                for chunk in chunk_document(text, metadata)
            ]
            for start in range(0, len(chunks), 100):
                self.report_search.upsert(chunks[start:start + 100])
        except (RuntimeError, ValueError, OSError, AzureError) as exc:
            self.reports.put(f"regulatory-index-state/{report_id}.json", {"error": str(exc)})
            raise RuntimeError("Table saved; report indexing failed. Retry without regenerating.") from exc
        self.reports.put(f"regulatory-index-state/{report_id}.json", {"digest": _row_digest(report)})
        return self.get(report_id)


def _row_digest(report: dict) -> str:
    return hashlib.sha256(json.dumps(
        [report["rows"], report.get("article_checks", {}), report.get("national_rows", [])],
        sort_keys=True, ensure_ascii=False
    ).encode()).hexdigest()
