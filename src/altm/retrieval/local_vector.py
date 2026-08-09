"""Small local lexical-vector retriever.

This is not a replacement for semantic embeddings. It gives Phase 4 a
dependency-light vector path that works for Chinese text by combining optional
jieba tokens, ASCII words, and character n-grams.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from importlib import import_module
from typing import Protocol, cast

from altm.contracts import MemoryLayer, MemoryStatus, MemoryUnit
from altm.storage import SQLiteMemoryStore


class _JiebaModule(Protocol):
    def setLogLevel(self, level: int) -> None: ...  # noqa: N802

    def cut(self, text: str) -> Iterable[str]: ...


def tokenize_for_local_vector(text: str) -> list[str]:
    tokens: list[str] = []
    lowered = text.lower()
    tokens.extend(re.findall(r"[a-z0-9_]{2,}", lowered))
    chinese_chars = re.findall(r"[\u4e00-\u9fff]", text)

    try:
        jieba = cast(_JiebaModule, import_module("jieba"))
        jieba.setLogLevel(30)
        tokens.extend(token for token in jieba.cut(text) if len(token.strip()) >= 2)
    except Exception:
        pass

    for size in (2, 3):
        tokens.extend(
            "".join(chinese_chars[index : index + size])
            for index in range(0, max(0, len(chinese_chars) - size + 1))
        )
    return tokens


def cosine_similarity(left: Iterable[str], right: Iterable[str]) -> float:
    left_counts = Counter(left)
    right_counts = Counter(right)
    if not left_counts or not right_counts:
        return 0.0
    shared = set(left_counts) & set(right_counts)
    dot = sum(left_counts[token] * right_counts[token] for token in shared)
    left_norm = math.sqrt(sum(value * value for value in left_counts.values()))
    right_norm = math.sqrt(sum(value * value for value in right_counts.values()))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


class LocalVectorRetriever:
    def __init__(self, store: SQLiteMemoryStore) -> None:
        self.store = store

    def search(
        self,
        query: str,
        limit: int = 10,
        layers: Sequence[MemoryLayer] = (),
        session_id: str | None = None,
        statuses: Sequence[MemoryStatus] = (),
    ) -> Sequence[tuple[MemoryUnit, float]]:
        query_tokens = tokenize_for_local_vector(query)
        candidates = self.store.list_memory_units(limit=1000)
        scored: list[tuple[MemoryUnit, float]] = []
        for memory in candidates:
            if layers and memory.layer not in layers:
                continue
            if statuses and memory.status not in statuses:
                continue
            if session_id is not None and memory.metadata.get("session_id") != session_id:
                continue
            text = "%s\n%s" % (memory.content, memory.summary or "")
            score = cosine_similarity(query_tokens, tokenize_for_local_vector(text))
            if score > 0:
                scored.append((memory, score))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:limit]
