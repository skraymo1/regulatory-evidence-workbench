"""Exhaustive, multi-pass mapping of one Reference requirement against a national collection.

Pass 1 screens every selected clause, in batches, against the explicit topics of the
full Reference requirement (statement plus supporting paragraphs). Pass 2 analyses the
shortlist. A residual pass re-screens the remaining clauses for topics that are still
partial or not explicit, then analysis is repeated with any newly found clauses.
The equivalence category is calculated here from the topic checklist, never by the model.
"""

from __future__ import annotations

import asyncio
import json

from regulatory_poc.types.requirements import Requirement


SCREEN_BATCH_CHARS = 120_000
TOPIC_STATUSES = ("explicit", "partial", "not_explicit")
OUTCOMES = ("proposed", "no_comparable", "insufficient_evidence")
EQUIVALENCE_LABELS = {
    "largely_equivalent": "Largely equivalent",
    "small_differences": "Small number of differences",
    "major_differences": "More than a third of the requirements are different",
    "not_assessed": "Not assessed",
}


def equivalence(topics: list[dict], outcome: str) -> str:
    """Partial topics count as differences; up to one third different is 'small'."""
    if outcome == "insufficient_evidence" or not topics:
        return "not_assessed"
    different = sum(topic["status"] != "explicit" for topic in topics)
    if different == 0:
        return "largely_equivalent"
    return "small_differences" if different * 3 <= len(topics) else "major_differences"


def baseline_view(row: dict) -> dict:
    return {
        "identifier": row["baseline"]["identifier"], "title": row["baseline"]["title"],
        "statement": row["baseline"]["description"],
        "supporting_paragraphs": [
            {"identifier": item["identifier"], "text": item["text"]}
            for item in row.get("paragraphs", [])
        ],
    }


def clause_view(item: dict) -> dict:
    view = {key: item[key] for key in ("key", "source_name", "identifier", "title", "description", "language")}
    if item.get("english_reference"):
        view["translation_reference"] = {
            key: item["english_reference"][key] for key in ("title", "description", "language")
        }
    return view


def batches(clauses: list[dict]) -> list[list[dict]]:
    result, current, size = [], [], 0
    for clause in clauses:
        length = len(json.dumps(clause_view(clause), ensure_ascii=False))
        if current and size + length > SCREEN_BATCH_CHARS:
            result.append(current)
            current, size = [], 0
        current.append(clause)
        size += length
    return result + ([current] if current else [])


def _object(payload: str) -> dict:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("Model response is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("Model response must be a JSON object")
    return value


def _strings(value, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"Model field {field} must be a list of strings")
    return value


def validate_screen(payload: str, clauses: list[dict], topics: list[dict] | None) -> tuple[list[dict], dict]:
    value = _object(payload)
    if topics is None:
        raw = value.get("topics")
        if not isinstance(raw, list) or not raw or len(raw) > 40:
            raise ValueError("Screening must enumerate 1-40 explicit reference topics")
        topics, seen = [], set()
        for item in raw:
            if not isinstance(item, dict) or any(
                not isinstance(item.get(field), str) or not item[field].strip() for field in ("id", "topic")
            ) or item["id"] in seen:
                raise ValueError("Each reference topic needs a unique id and a topic")
            seen.add(item["id"])
            topics.append({"id": item["id"], "topic": item["topic"], "source": str(item.get("source", ""))})
    ids, allowed, relevant = {topic["id"] for topic in topics}, {item["key"] for item in clauses}, {}
    if not isinstance(value.get("relevant"), list):
        raise ValueError("Screening must include a relevant array")
    for item in value["relevant"]:
        if not isinstance(item, dict) or item.get("key") not in allowed:
            raise ValueError("Screening returned an unknown clause key")
        topic_ids = _strings(item.get("topic_ids"), "topic_ids")
        if not topic_ids or set(topic_ids) - ids:
            raise ValueError("Screening topic_ids must reference enumerated topics")
        relevant.setdefault(item["key"], set()).update(topic_ids)
    return topics, relevant


STATUS_ALIASES = {
    "explicit": "explicit", "fully_explicit": "explicit", "covered": "explicit", "full": "explicit",
    "partial": "partial", "partially_explicit": "partial", "partly": "partial",
    "not_explicit": "not_explicit", "notexplicit": "not_explicit", "none": "not_explicit",
    "absent": "not_explicit", "missing": "not_explicit", "not_covered": "not_explicit",
}


def _status(value) -> str | None:
    if not isinstance(value, str):
        return None
    return STATUS_ALIASES.get(value.strip().lower().replace(" ", "_").replace("-", "_"))


def validate_mapping(payload: str, candidates: list[dict], topics: list[dict], repair: bool = False) -> dict:
    """Validate the analysis JSON.

    With ``repair`` (used only after a corrective retry has also failed), inconsistencies
    are resolved conservatively instead of rejected: citations to clauses without their
    own analysis are dropped, unsupported topics become not explicit, missing topics are
    added as not explicit and the outcome is recalculated. Every repair is recorded in
    the evidence-limitations note so reviewers can see it.
    """
    value = _object(payload)
    repairs: list[str] = []
    allowed = {item["key"]: item for item in candidates}
    for field in ("matches", "topics", "national_only_topics", "terminology"):
        if not isinstance(value.get(field), list):
            if repair and field in ("national_only_topics", "terminology"):
                value[field] = []
                continue
            raise ValueError(f"Model response must include a {field} array")
    if not isinstance(value.get("note"), str) or not value["note"].strip():
        if not repair:
            raise ValueError("Model response must include evidence limitations in note")
        value["note"] = "The model returned no evidence-limitations note."
    outcome = value.get("outcome")
    if outcome not in OUTCOMES:
        if not repair:
            raise ValueError("Model mapping outcome is invalid")
        outcome = None
    matches, seen = [], set()
    for match in value["matches"]:
        if not isinstance(match, dict) or match.get("key") not in allowed or match["key"] in seen:
            if repair:
                repairs.append(f"ignored an unknown or duplicate match {match.get('key') if isinstance(match, dict) else match!r}")
                continue
            raise ValueError(
                f"Model returned an unknown or duplicate requirement key "
                f"{match.get('key') if isinstance(match, dict) else match!r}"
            )
        if any(not isinstance(match.get(field), str) for field in (
            "analysis", "english_title", "english_description"
        )) or not match["analysis"].strip():
            if repair:
                repairs.append(f"ignored match {match['key']} without an analysis")
                continue
            raise ValueError(f"Match {match['key']} needs analysis, english_title and english_description strings")
        seen.add(match["key"])
        # Quotations always come from the pinned inventory, never from model output.
        matches.append({"source": allowed[match["key"]], "analysis": match["analysis"],
                        "english_title": match["english_title"],
                        "english_description": match["english_description"]})
    expected, checked = {topic["id"]: topic for topic in topics}, []
    for item in value["topics"]:
        if not isinstance(item, dict) or item.get("id") not in expected or item["id"] in {c["id"] for c in checked}:
            if repair:
                continue
            raise ValueError("Topic checklist must assess each enumerated topic once")
        keys = _strings(item.get("keys", []), "topic keys") if not repair or isinstance(item.get("keys"), list) else []
        keys = [key for key in keys if isinstance(key, str)]
        status = _status(item.get("status"))
        if status is None:
            if not repair:
                raise ValueError(
                    f"Topic {item['id']} status {item.get('status')!r} must be one of {', '.join(TOPIC_STATUSES)}"
                )
            status = "partial" if keys else "not_explicit"
            repairs.append(f"topic {item['id']} had an invalid status")
        unknown = [key for key in keys if key not in seen]
        if unknown:
            if not repair:
                raise ValueError(
                    f"Topic {item['id']} cites {', '.join(unknown)}, which is not in matches. Either add "
                    "each cited clause to matches with its own analysis and translation, or remove it from the topic keys"
                )
            keys = [key for key in keys if key in seen]
            repairs.append(f"topic {item['id']} cited {', '.join(unknown)} without a match analysis")
        if status == "not_explicit" and keys:
            if not repair:
                raise ValueError(f"Topic {item['id']} is not_explicit but cites keys; not_explicit topics cite none")
            status = "partial"
            repairs.append(f"topic {item['id']} was not_explicit but cited clauses; treated as partial")
        if status != "not_explicit" and not keys:
            if not repair:
                raise ValueError(f"Topic {item['id']} is {status} but cites no match keys")
            status = "not_explicit"
            repairs.append(f"topic {item['id']} had no supporting match; treated as not explicit")
        checked.append({**expected[item["id"]], "status": status, "keys": keys,
                        "explanation": str(item.get("explanation", ""))})
    if len(checked) != len(expected):
        if not repair:
            missing = [key for key in expected if key not in {c["id"] for c in checked}]
            raise ValueError(f"Topic checklist must assess each enumerated topic once; missing {', '.join(missing)}")
        for key in expected:
            if key not in {c["id"] for c in checked}:
                checked.append({**expected[key], "status": "not_explicit", "keys": [],
                                "explanation": "Not assessed by the model."})
                repairs.append(f"topic {key} was not assessed; treated as not explicit")
        order = list(expected)
        checked.sort(key=lambda item: order.index(item["id"]))
    covered = any(item["status"] != "not_explicit" for item in checked)
    # One-to-one rows: a clause is a correspondence only if an reference topic relies on it.
    cited = {key for item in checked for key in item["keys"]}
    if repair:
        derived = "proposed" if covered else (
            "insufficient_evidence" if outcome == "insufficient_evidence" else "no_comparable"
        )
        if outcome != derived:
            repairs.append(f"outcome recalculated as {derived}")
        outcome = derived
    elif (outcome == "proposed") != bool(matches) or (outcome == "proposed") != covered:
        raise ValueError("Model mapping outcome contradicts its matches or topic checklist")
    matches = [match for match in matches if match["source"]["key"] in cited]
    national_only = []
    for item in value["national_only_topics"]:
        if not isinstance(item, dict) or not isinstance(item.get("topic"), str) or not item["topic"].strip():
            if repair:
                continue
            raise ValueError("National-only topics need a topic")
        keys = _strings(item.get("keys", []), "national-only keys") if not repair or isinstance(item.get("keys"), list) else []
        if set(keys) - set(allowed):
            if not repair:
                raise ValueError("National-only topic cites an unknown key")
            keys = [key for key in keys if key in allowed]
        kept = [key for key in keys if key in cited]
        if keys and not kept:
            continue
        national_only.append({"topic": item["topic"], "keys": kept})
    terminology = []
    for item in value["terminology"]:
        fields = ("term", "language", "literal_english", "reference_term", "concern")
        if not isinstance(item, dict) or any(not isinstance(item.get(field), str) for field in fields) \
                or not item["term"].strip():
            if repair:
                continue
            raise ValueError("Terminology entries need term, language, literal_english, reference_term and concern")
        terminology.append({field: item[field] for field in fields})
    note = value["note"]
    if repairs:
        note = f"{note} Automatic consistency repairs after a failed corrective retry: {'; '.join(repairs)}."
    cited_instruments = value.get("cited_instruments", [])
    if repair and not (isinstance(cited_instruments, list) and all(isinstance(i, str) for i in cited_instruments)):
        cited_instruments = []
    return {
        "matches": matches, "topics": checked, "national_only_topics": national_only,
        "terminology": terminology, "note": note, "outcome": outcome,
        "cited_instruments": _strings(cited_instruments, "cited_instruments"),
        **({"repairs": repairs} if repairs else {}),
    }


async def _ask(agent, request: dict, validate, timeout: float, repair=None):
    """One call plus one corrective retry; then an optional conservative repair."""
    prompt = json.dumps(request, ensure_ascii=False)
    async with asyncio.timeout(timeout):
        payload = await agent.compare(prompt)
    try:
        return validate(payload)
    except ValueError as exc:
        retry = {**request, "previous_response": payload,
                 "previous_response_error": f"{exc}. Return the complete corrected JSON only."}
        async with asyncio.timeout(timeout):
            payload = await agent.compare(json.dumps(retry, ensure_ascii=False))
        try:
            return validate(payload)
        except ValueError:
            if repair is None:
                raise
            return repair(payload)


async def _screen(agent, rules, row, clauses, topics, focus, timeout):
    relevant = {}
    for batch in batches(clauses):
        request = {
            "task": "residual_screen" if focus else "screen",
            "instruction": rules["residual_instruction" if focus else "screen_instruction"],
            "reference_requirement": baseline_view(row), "topics": topics, "focus_topic_ids": focus,
            "clauses": [clause_view(item) for item in batch],
        }
        topics, found = await _ask(agent, request, lambda p, b=batch, t=topics: validate_screen(p, b, t), timeout)
        for key, ids in found.items():
            relevant.setdefault(key, set()).update(ids)
    return topics, relevant


async def _analyse(agent, rules, row, shortlist, topics, documents, timeout):
    request = {
        "task": "analyse", "instruction": rules["analysis_instruction"],
        "reference_requirement": baseline_view(row), "topics": topics,
        "supplied_documents": documents, "candidates": [clause_view(item) for item in shortlist],
    }
    return await _ask(agent, request, lambda p: validate_mapping(p, shortlist, topics), timeout,
                      repair=lambda p: validate_mapping(p, shortlist, topics, repair=True))


async def map_requirement(agent, rules: dict, row: dict, clauses: list[dict],
                          documents: list[str], timeout: float) -> dict:
    by_key = {item["key"]: item for item in clauses}
    if not clauses:
        raise ValueError("The selected national documents contain no extracted clauses to screen.")
    topics, relevant = await _screen(agent, rules, row, clauses, None, [], timeout)
    passes = [{"pass": "screen", "clauses": len(clauses), "found": sorted(relevant)}]
    result = None
    if relevant:
        result = await _analyse(agent, rules, row, [by_key[k] for k in by_key if k in relevant],
                                topics, documents, timeout)
    open_ids = [t["id"] for t in (result["topics"] if result else topics) if t.get("status") != "explicit"]
    if open_ids and (result is None or result["outcome"] != "insufficient_evidence"):
        remaining = [item for item in clauses if item["key"] not in relevant]
        _, extra = await _screen(agent, rules, row, remaining, topics, open_ids, timeout) if remaining else (topics, {})
        passes.append({"pass": "residual_screen", "clauses": len(remaining), "topics": open_ids,
                       "found": sorted(extra)})
        if extra:
            for key, ids in extra.items():
                relevant.setdefault(key, set()).update(ids)
            result = await _analyse(agent, rules, row, [by_key[k] for k in by_key if k in relevant],
                                    topics, documents, timeout)
    if result is None:
        result = {
            "matches": [], "national_only_topics": [], "terminology": [], "cited_instruments": [],
            "topics": [{**topic, "status": "not_explicit", "keys": [], "explanation": ""} for topic in topics],
            "outcome": "no_comparable",
            "note": (f"No clause among all {len(clauses)} selected national clauses was identified as "
                     "addressing this requirement, after a residual re-screen. This is not proof that "
                     "the jurisdiction lacks such a requirement in documents outside the collection."),
        }
    shortlist = [by_key[key] for key in by_key if key in relevant]
    return {
        **result, "candidates": shortlist,
        "equivalence": equivalence(result["topics"], result["outcome"]),
        "screening": {"complete": True, "clauses": len(clauses), "batches": len(batches(clauses)),
                      "passes": passes},
    }
