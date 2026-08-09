"""Small shared helpers for deterministic identifiers and timestamps."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_id(prefix: str, *parts: str) -> str:
    digest = sha256_text("\n".join(parts))[:24]
    return "%s_%s" % (prefix, digest)


def random_id(prefix: str) -> str:
    return "%s_%s" % (prefix, uuid.uuid4().hex)
