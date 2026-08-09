"""MCP bearer authentication with hashed API keys and OIDC extension hooks."""

from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl, TypeAdapter


@dataclass(frozen=True)
class APIKeyRecord:
    client_id: str
    sha256: str
    scopes: tuple[str, ...]


class HashedAPIKeyVerifier:
    def __init__(self, records: list[APIKeyRecord]) -> None:
        if not records:
            raise ValueError("At least one hashed MCP API key is required")
        self.records = tuple(records)

    async def verify_token(self, token: str) -> AccessToken | None:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        for record in self.records:
            if hmac.compare_digest(digest, record.sha256):
                return AccessToken(
                    token=token,
                    client_id=record.client_id,
                    scopes=list(record.scopes),
                    subject=record.client_id,
                    claims={"auth_method": "hashed_api_key"},
                )
        return None


def token_verifier_from_env(profile: str) -> TokenVerifier:
    factory_path = os.environ.get("ALTM_MCP_TOKEN_VERIFIER_FACTORY", "").strip()
    if factory_path:
        return _load_verifier_factory(factory_path)

    records_json = os.environ.get("ALTM_MCP_API_KEYS_JSON", "").strip()
    if records_json:
        data: object = json.loads(records_json)
        if not isinstance(data, list):
            raise ValueError("ALTM_MCP_API_KEYS_JSON must be a JSON array")
        records = [
            _api_key_record(item)
            for item in cast(list[object], data)
        ]
    else:
        digest = os.environ.get("ALTM_MCP_API_KEY_SHA256", "").strip().lower()
        if not digest:
            raise RuntimeError(
                "Remote MCP requires ALTM_MCP_API_KEY_SHA256, "
                "ALTM_MCP_API_KEYS_JSON, or ALTM_MCP_TOKEN_VERIFIER_FACTORY"
            )
        records = [
            APIKeyRecord(
                client_id="altm-static-client",
                sha256=_validate_sha256(digest),
                scopes=("altm:runtime", "altm:admin"),
            )
        ]
    required_scope = "altm:%s" % profile
    if not any(required_scope in record.scopes for record in records):
        raise RuntimeError(
            "No configured MCP API key grants required scope %s" % required_scope
        )
    return HashedAPIKeyVerifier(records)


def auth_settings_from_env(profile: str) -> AuthSettings:
    issuer = os.environ.get("ALTM_MCP_ISSUER_URL", "http://localhost")
    resource = os.environ.get(
        "ALTM_MCP_RESOURCE_SERVER_URL",
        "http://127.0.0.1:8000/mcp",
    )
    return AuthSettings(
        issuer_url=TypeAdapter(AnyHttpUrl).validate_python(issuer),
        resource_server_url=TypeAdapter(AnyHttpUrl).validate_python(resource),
        required_scopes=["altm:%s" % profile],
    )


def _api_key_record(value: object) -> APIKeyRecord:
    if not isinstance(value, dict):
        raise ValueError("Each MCP API key record must be an object")
    record = cast(dict[object, object], value)
    client_id = str(record.get("client_id", "")).strip()
    digest = str(record.get("sha256", "")).strip().lower()
    scopes_value = record.get("scopes", [])
    if not client_id or not isinstance(scopes_value, list):
        raise ValueError("MCP API key record requires client_id and scopes")
    scopes = tuple(
        str(scope).strip()
        for scope in cast(list[object], scopes_value)
        if str(scope).strip()
    )
    if not scopes:
        raise ValueError("MCP API key record requires at least one scope")
    return APIKeyRecord(
        client_id=client_id,
        sha256=_validate_sha256(digest),
        scopes=scopes,
    )


def _validate_sha256(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("MCP API key sha256 must contain 64 lowercase hex characters")
    return value


def _load_verifier_factory(factory_path: str) -> TokenVerifier:
    if ":" not in factory_path:
        raise ValueError("Verifier factory must use module.path:factory format")
    module_name, factory_name = factory_path.split(":", 1)
    module = importlib.import_module(module_name)
    factory = getattr(module, factory_name, None)
    if not callable(factory):
        raise ValueError("Configured MCP token verifier factory is not callable")
    verifier = cast(Callable[[], object], factory)()
    if not callable(getattr(verifier, "verify_token", None)):
        raise ValueError("Configured MCP token verifier lacks verify_token")
    return cast(TokenVerifier, verifier)
