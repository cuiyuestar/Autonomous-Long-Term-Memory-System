"""ALTM 运行时配置读取。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

_PACKAGED_HIGH_RISK_FLAGS_PATH = Path(
    str(files("altm").joinpath("configs", "high_risk_flags.env"))
)
_REPOSITORY_HIGH_RISK_FLAGS_PATH = (
    Path(__file__).resolve().parents[2] / "configs" / "high_risk_flags.env"
)
DEFAULT_HIGH_RISK_FLAGS_PATH = (
    _PACKAGED_HIGH_RISK_FLAGS_PATH
    if _PACKAGED_HIGH_RISK_FLAGS_PATH.exists()
    else _REPOSITORY_HIGH_RISK_FLAGS_PATH
)

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class HighRiskFlags:
    enable_default_active_window_in_build_context: bool = True
    enable_active_window_lifecycle_feedback: bool = True
    enable_l4_persona_active_window: bool = True
    enable_l4_persona_candidates: bool = True
    enable_cross_session_l3_candidates: bool = True
    enable_auto_l2_semantic_merge: bool = True
    enable_auto_l2_tombstone: bool = True
    enable_optional_context_tokenizer: bool = True
    enable_review_event_sourcing: bool = True
    enable_review_audit_projections: bool = True
    enable_high_risk_defaults: bool = True

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> HighRiskFlags:
        values = _read_env_file(path or DEFAULT_HIGH_RISK_FLAGS_PATH)
        env = os.environ if environ is None else environ
        for name in _FLAG_NAMES:
            if name in env:
                values[name] = env[name]

        master_enabled = _bool_flag(
            values.get("ALTM_ENABLE_HIGH_RISK_DEFAULTS"),
            default=True,
            name="ALTM_ENABLE_HIGH_RISK_DEFAULTS",
        )
        return cls(
            enable_default_active_window_in_build_context=master_enabled
            and _bool_flag(
                values.get("ALTM_ENABLE_DEFAULT_ACTIVE_WINDOW_IN_BUILD_CONTEXT"),
                default=True,
                name="ALTM_ENABLE_DEFAULT_ACTIVE_WINDOW_IN_BUILD_CONTEXT",
            ),
            enable_active_window_lifecycle_feedback=master_enabled
            and _bool_flag(
                values.get("ALTM_ENABLE_ACTIVE_WINDOW_LIFECYCLE_FEEDBACK"),
                default=True,
                name="ALTM_ENABLE_ACTIVE_WINDOW_LIFECYCLE_FEEDBACK",
            ),
            enable_l4_persona_active_window=master_enabled
            and _bool_flag(
                values.get("ALTM_ENABLE_L4_PERSONA_ACTIVE_WINDOW"),
                default=True,
                name="ALTM_ENABLE_L4_PERSONA_ACTIVE_WINDOW",
            ),
            enable_l4_persona_candidates=master_enabled
            and _bool_flag(
                values.get("ALTM_ENABLE_L4_PERSONA_CANDIDATES"),
                default=True,
                name="ALTM_ENABLE_L4_PERSONA_CANDIDATES",
            ),
            enable_cross_session_l3_candidates=master_enabled
            and _bool_flag(
                values.get("ALTM_ENABLE_CROSS_SESSION_L3_CANDIDATES"),
                default=True,
                name="ALTM_ENABLE_CROSS_SESSION_L3_CANDIDATES",
            ),
            enable_auto_l2_semantic_merge=master_enabled
            and _bool_flag(
                values.get("ALTM_ENABLE_AUTO_L2_SEMANTIC_MERGE"),
                default=True,
                name="ALTM_ENABLE_AUTO_L2_SEMANTIC_MERGE",
            ),
            enable_auto_l2_tombstone=master_enabled
            and _bool_flag(
                values.get("ALTM_ENABLE_AUTO_L2_TOMBSTONE"),
                default=True,
                name="ALTM_ENABLE_AUTO_L2_TOMBSTONE",
            ),
            enable_optional_context_tokenizer=master_enabled
            and _bool_flag(
                values.get("ALTM_ENABLE_OPTIONAL_CONTEXT_TOKENIZER"),
                default=True,
                name="ALTM_ENABLE_OPTIONAL_CONTEXT_TOKENIZER",
            ),
            enable_review_event_sourcing=master_enabled
            and _bool_flag(
                values.get("ALTM_ENABLE_REVIEW_EVENT_SOURCING"),
                default=True,
                name="ALTM_ENABLE_REVIEW_EVENT_SOURCING",
            ),
            enable_review_audit_projections=master_enabled
            and _bool_flag(
                values.get("ALTM_ENABLE_REVIEW_AUDIT_PROJECTIONS"),
                default=True,
                name="ALTM_ENABLE_REVIEW_AUDIT_PROJECTIONS",
            ),
            enable_high_risk_defaults=master_enabled,
        )


def high_risk_flags(path: str | Path | None = None) -> HighRiskFlags:
    return HighRiskFlags.load(path=path)


_FLAG_NAMES = {
    "ALTM_ENABLE_DEFAULT_ACTIVE_WINDOW_IN_BUILD_CONTEXT",
    "ALTM_ENABLE_ACTIVE_WINDOW_LIFECYCLE_FEEDBACK",
    "ALTM_ENABLE_L4_PERSONA_ACTIVE_WINDOW",
    "ALTM_ENABLE_L4_PERSONA_CANDIDATES",
    "ALTM_ENABLE_CROSS_SESSION_L3_CANDIDATES",
    "ALTM_ENABLE_AUTO_L2_SEMANTIC_MERGE",
    "ALTM_ENABLE_AUTO_L2_TOMBSTONE",
    "ALTM_ENABLE_OPTIONAL_CONTEXT_TOKENIZER",
    "ALTM_ENABLE_REVIEW_EVENT_SOURCING",
    "ALTM_ENABLE_REVIEW_AUDIT_PROJECTIONS",
    "ALTM_ENABLE_HIGH_RISK_DEFAULTS",
}


def _read_env_file(path: str | Path) -> dict[str, str]:
    env_path = Path(path)
    if not env_path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in _FLAG_NAMES:
            values[key] = _unquote(value.strip())
    return values


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _bool_flag(value: str | None, default: bool, name: str) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ValueError("Invalid boolean value for %s: %s" % (name, value))
