import asyncio
import hashlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from altm.adapters.mcp.auth import token_verifier_from_env  # noqa: E402


class MCPAuthTest(unittest.TestCase):
    def test_hashed_api_key_verifier_accepts_valid_key(self) -> None:
        token = "secret-api-key"
        digest = hashlib.sha256(token.encode()).hexdigest()
        with patch.dict(
            "os.environ",
            {"ALTM_MCP_API_KEY_SHA256": digest},
            clear=True,
        ):
            verifier = token_verifier_from_env("runtime")

        accepted = asyncio.run(verifier.verify_token(token))
        rejected = asyncio.run(verifier.verify_token("wrong"))
        self.assertIsNotNone(accepted)
        self.assertIn("altm:runtime", accepted.scopes)
        self.assertIsNone(rejected)

    def test_profile_requires_matching_scope(self) -> None:
        token = "runtime-only"
        config = json.dumps(
            [
                {
                    "client_id": "runtime-client",
                    "sha256": hashlib.sha256(token.encode()).hexdigest(),
                    "scopes": ["altm:runtime"],
                }
            ]
        )
        with patch.dict(
            "os.environ",
            {"ALTM_MCP_API_KEYS_JSON": config},
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "required scope"):
                token_verifier_from_env("admin")

    def test_remote_auth_rejects_plaintext_or_invalid_hash_configuration(self) -> None:
        with patch.dict(
            "os.environ",
            {"ALTM_MCP_API_KEY_SHA256": "plaintext-key"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "64 lowercase hex"):
                token_verifier_from_env("runtime")


if __name__ == "__main__":
    unittest.main()
