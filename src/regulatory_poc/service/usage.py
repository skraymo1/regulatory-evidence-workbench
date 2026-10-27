from __future__ import annotations

import json
import math
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from regulatory_poc.repo.regulatory_store import RecordStore


_scope: ContextVar[dict] = ContextVar("model_usage_scope", default={})


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def usage_scope(**values):
    token = _scope.set({**_scope.get(), **values})
    try:
        yield
    finally:
        _scope.reset(token)


def parse_pricing(value: str) -> dict:
    try:
        rates = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("POC_MODEL_PRICING_USD_PER_MILLION must be a JSON object.") from exc
    if not isinstance(rates, dict):
        raise ValueError("Model pricing must map deployment names to input/output rates.")
    for model, rate in rates.items():
        if not model or not isinstance(rate, dict) or set(rate) != {"input", "output"}:
            raise ValueError("Each model price requires input and output USD rates per million tokens.")
        if any(isinstance(v, bool) or not isinstance(v, (int, float))
               or not math.isfinite(v) or v < 0 for v in rate.values()):
            raise ValueError("Model prices must be finite, nonnegative numbers.")
    return rates


def _field(value, name):
    return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)


def _tokens(value, *names) -> int | None:
    for name in names:
        count = _field(value, name)
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            return count
    return None


@dataclass
class ModelCall:
    response: object = None


class UsageTracker:
    def __init__(self, store: RecordStore, pricing: str = "{}") -> None:
        self.store = store
        self._pricing = parse_pricing(pricing)

    @property
    def pricing(self) -> dict:
        configured = self.store.get("model-pricing/current.json")
        return parse_pricing(json.dumps(configured)) if configured is not None else self._pricing

    def set_pricing(self, value: str) -> None:
        self.store.put("model-pricing/current.json", parse_pricing(value))

    @contextmanager
    def call(self, model: str, operation: str, *, embedding: bool = False):
        rate = self.pricing.get(model)
        event = {
            "event_id": uuid.uuid4().hex, "started_at": timestamp(),
            "model": model, "operation": operation, **_scope.get(),
            "status": "started", "input_tokens": None, "output_tokens": None,
            "estimated_cost_usd": None, "rates_usd_per_million": rate,
        }
        key = f"model-usage/{event['event_id']}.json"
        self.store.put(key, event)
        call = ModelCall()
        status = "completed"
        try:
            yield call
        except BaseException:
            # Persist interrupted/failed requests too; they may still incur provider charges.
            status = "failed"
            raise
        finally:
            usage = _field(call.response, "usage_details")
            if usage is None:
                usage = _field(call.response, "usage")
            input_tokens = _tokens(usage, "input_token_count", "input_tokens", "prompt_tokens")
            output_tokens = 0 if embedding else _tokens(
                usage, "output_token_count", "output_tokens", "completion_tokens",
            )
            cost = None
            if rate is not None and input_tokens is not None and output_tokens is not None:
                cost = (input_tokens * rate["input"] + output_tokens * rate["output"]) / 1_000_000
            event.update(
                status=status, finished_at=timestamp(),
                input_tokens=input_tokens, output_tokens=output_tokens, estimated_cost_usd=cost,
            )
            self.store.put(key, event)

    def events(self) -> list[dict]:
        return sorted(self.store.list("model-usage/"), key=lambda item: item["started_at"], reverse=True)


@contextmanager
def model_call(tracker: UsageTracker | None, model: str, operation: str, **kwargs):
    if tracker is None:
        yield ModelCall()
    else:
        with tracker.call(model, operation, **kwargs) as call:
            yield call


def usage_summary(events: list[dict]) -> dict:
    costs = [item["estimated_cost_usd"] for item in events if item["estimated_cost_usd"] is not None]
    return {
        "calls": len(events),
        "input_tokens": sum(item["input_tokens"] or 0 for item in events),
        "output_tokens": sum(item["output_tokens"] or 0 for item in events),
        "estimated_cost_usd": sum(costs) if costs or not events else None,
        "unpriced_or_unknown_calls": sum(item["estimated_cost_usd"] is None for item in events),
        "unknown_usage_calls": sum(
            item["input_tokens"] is None or item["output_tokens"] is None for item in events
        ),
    }
