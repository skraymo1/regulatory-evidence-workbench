from __future__ import annotations

import json
from pathlib import Path

from regulatory_poc.types.models import ChatMessage


class JsonConversationRepository:
    def __init__(self, path: Path) -> None:
        self._path = path

    def get(self, conversation_id: str) -> tuple[ChatMessage, ...]:
        values = self._read().get(conversation_id, [])
        return tuple(
            ChatMessage(role=item["role"], content=item["content"]) for item in values
        )

    def append(self, conversation_id: str, *messages: ChatMessage) -> None:
        values = self._read()
        history = values.setdefault(conversation_id, [])
        history.extend({"role": item.role, "content": item.content} for item in messages)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _read(self) -> dict[str, list[dict[str, str]]]:
        if not self._path.exists():
            return {}
        try:
            value = json.loads(self._path.read_text(encoding="utf-8"))
            return {
                str(key): [
                    {"role": str(item["role"]), "content": str(item["content"])}
                    for item in items
                ]
                for key, items in value.items()
            }
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ValueError(f"Invalid conversation store: {self._path}") from exc
