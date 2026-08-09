"""Built-in content-aware compression with SQLite CCR retrieval."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import cast

from altm.contracts import MemoryUnit
from altm.llm import OpenAICompatibleClient, llm_config_from_env
from altm.storage import SQLiteMemoryStore

_CODE_HINT = re.compile(
    r"(^|\n)\s*(def |class |function |const |let |var |import |from |package |func )"
)
_LOG_HINT = re.compile(
    r"(^|\n).*(ERROR|WARN|INFO|DEBUG|Traceback|Exception|\d{4}-\d{2}-\d{2})"
)


@dataclass(frozen=True)
class CompressionResult:
    rendered: str
    marker: str
    content_type: str
    strategy: str
    original_token_estimate: int
    compressed_token_estimate: int
    compressed: bool
    metadata: dict[str, object] = field(
        default_factory=lambda: dict[str, object]()
    )


class ContentRouter:
    def __init__(self, store: SQLiteMemoryStore) -> None:
        self.store = store

    def compress(
        self,
        memory: MemoryUnit,
        target_tokens: int,
    ) -> CompressionResult:
        target = max(1, target_tokens)
        original = memory.content
        content_type = _content_type(memory)
        marker = "memory://%s#%s" % (memory.id, memory.content_hash[:16])
        original_tokens = _token_estimate(original)
        if original_tokens <= target:
            rendered = original
            strategy = "identity"
        elif content_type == "json":
            rendered = _compress_json(original, target)
            strategy = "json_structure"
        elif content_type == "code":
            rendered = _compress_code(original, target)
            strategy = "code_structure"
        elif content_type == "log":
            rendered = _compress_log(original, target)
            strategy = "log_signal"
        else:
            rendered, strategy = _compress_natural_language(original, target)

        compressed_tokens = _token_estimate(rendered)
        compressed = rendered != original
        self.store.put_ccr_entry(
            marker=marker,
            memory_id=memory.id,
            content_hash=memory.content_hash,
            content_type=content_type,
            strategy=strategy,
            original_text=original,
            compressed_text=rendered,
            metadata={
                "target_tokens": target,
                "compressed": compressed,
            },
        )
        return CompressionResult(
            rendered=rendered,
            marker=marker,
            content_type=content_type,
            strategy=strategy,
            original_token_estimate=original_tokens,
            compressed_token_estimate=compressed_tokens,
            compressed=compressed,
        )

    def retrieve(
        self,
        marker: str,
        query: str | None = None,
    ) -> dict[str, object] | None:
        entry = self.store.get_ccr_entry(marker)
        if entry is None:
            return None
        if not query:
            return entry
        original = str(entry["original_text"])
        matches = [
            line
            for line in original.splitlines()
            if query.casefold() in line.casefold()
        ]
        return {
            **entry,
            "query": query,
            "matches": matches[:50],
        }


def _content_type(memory: MemoryUnit) -> str:
    declared = memory.metadata.get("content_type")
    if isinstance(declared, str) and declared in {"json", "code", "log", "natural"}:
        return declared
    text = memory.content.strip()
    if text.startswith(("{", "[")):
        try:
            json.loads(text)
            return "json"
        except json.JSONDecodeError:
            pass
    if _CODE_HINT.search(text):
        return "code"
    if _LOG_HINT.search(text):
        return "log"
    return "natural"


def _compress_json(text: str, target_tokens: int) -> str:
    value: object = json.loads(text)
    compact = _prune_json(value, depth=0)
    rendered = json.dumps(compact, ensure_ascii=False, sort_keys=True)
    return _clip(rendered, target_tokens)


def _prune_json(value: object, depth: int) -> object:
    if depth >= 5:
        return "<nested content available via retrieval marker>"
    if isinstance(value, dict):
        return {
            str(key): _prune_json(item, depth + 1)
            for key, item in list(
                cast(dict[object, object], value).items()
            )[:40]
        }
    if isinstance(value, list):
        values = cast(list[object], value)
        result: list[object] = [
            _prune_json(item, depth + 1)
            for item in values[:8]
        ]
        if len(values) > 8:
            result.append({"__omitted_items__": len(values) - 8})
        return result
    if isinstance(value, str) and len(value) > 400:
        return value[:397] + "..."
    return value


def _compress_code(text: str, target_tokens: int) -> str:
    lines = text.splitlines()
    selected: list[str] = []
    for line in lines:
        stripped = line.strip()
        if (
            stripped.startswith(
                (
                    "import ",
                    "from ",
                    "package ",
                    "class ",
                    "def ",
                    "func ",
                    "function ",
                    "interface ",
                    "type ",
                )
            )
            or "TODO" in line
            or "FIXME" in line
            or "raise " in line
            or "throw " in line
        ):
            selected.append(line)
    if not selected:
        selected = lines[:30]
    return _clip("\n".join(selected), target_tokens)


def _compress_log(text: str, target_tokens: int) -> str:
    lines = text.splitlines()
    selected = [
        line
        for line in lines
        if any(
            marker in line
            for marker in ("ERROR", "WARN", "Exception", "Traceback", "FAILED")
        )
    ]
    if not selected:
        selected = lines[-30:]
    deduplicated = list(dict.fromkeys(selected))
    return _clip("\n".join(deduplicated), target_tokens)


def _compress_natural_language(text: str, target_tokens: int) -> tuple[str, str]:
    try:
        client = OpenAICompatibleClient(llm_config_from_env("headroom"))
    except RuntimeError:
        return "", "marker_only_model_unavailable"
    response = client.chat_json(
        [
            {
                "role": "system",
                "content": (
                    "Compress the supplied memory without inventing facts. Preserve "
                    "decisions, constraints, corrections, unresolved questions, "
                    "identifiers, paths, numbers, and risks. Return JSON with one "
                    "field: compressed."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"target_tokens": target_tokens, "text": text},
                    ensure_ascii=False,
                ),
            },
        ]
    )
    compressed = str(response.get("compressed", "")).strip()
    if not compressed:
        raise ValueError("Headroom model returned empty compressed content")
    return _clip(compressed, target_tokens), "llm_natural_language"


def _clip(text: str, target_tokens: int) -> str:
    return text[: target_tokens * 4]


def _token_estimate(text: str) -> int:
    return max(1, (len(text) + 3) // 4) if text else 0
