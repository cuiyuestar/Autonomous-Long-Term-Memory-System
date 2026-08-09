import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters.hermes import register  # noqa: E402


class _FakeHermesContext:
    def __init__(self) -> None:
        self.hooks: dict[str, object] = {}
        self.calls: list[tuple[str, dict[str, object]]] = []

    def register_hook(self, name: str, callback: object) -> None:
        self.hooks[name] = callback

    def dispatch_tool(self, name: str, args: dict[str, object]) -> str:
        self.calls.append((name, args))
        if name.endswith("memory_prepare_turn"):
            prepared = {
                "cycle_id": "cycle-1",
                "scope": {
                    "tenant_id": args["tenant_id"],
                    "workspace_id": args["workspace_id"],
                    "user_id": args["user_id"],
                    "agent_id": args["agent_id"],
                },
                "session_id": args["session_id"],
                "turn_id": args["turn_id"],
                "user_memory_id": "user-memory",
                "query": args["query"],
                "context": {
                    "items": [
                        {
                            "band": "working",
                            "content": "The release deadline is 2026-09-01.",
                            "source_memory_ids": ["deadline-memory"],
                            "retrieval_marker": "memory://deadline-memory#hash",
                            "metadata": {},
                        }
                    ],
                    "token_budget": 1200,
                    "metadata": {},
                },
                "enqueued_job_ids": [],
                "status": "prepared",
                "metadata": {},
            }
            return json.dumps(
                {
                    "result": json.dumps(prepared),
                    "structuredContent": prepared,
                }
            )
        if name.endswith("memory_commit_turn"):
            return json.dumps(
                {
                    "result": {
                        "cycle_id": args["cycle_id"],
                        "status": "committed",
                    }
                }
            )
        raise AssertionError("Unexpected tool: %s" % name)


class HermesAdapterTest(unittest.TestCase):
    def test_prepare_injects_context_and_commit_uses_real_marker_citation(self) -> None:
        context = _FakeHermesContext()
        with patch.dict(
            os.environ,
            {
                "ALTM_HERMES_TENANT_ID": "tenant",
                "ALTM_HERMES_WORKSPACE_ID": "workspace",
                "ALTM_HERMES_AGENT_ID": "hermes-agent",
                "ALTM_HERMES_MCP_SERVER_NAME": "altm",
            },
            clear=True,
        ):
            register(context)

        pre_hook = context.hooks["pre_llm_call"]
        injected = pre_hook(
            session_id="session",
            user_message="When is the release?",
            turn_id="turn",
            sender_id="user",
        )
        post_hook = context.hooks["post_llm_call"]
        post_hook(
            session_id="session",
            assistant_response=(
                "The release is due on 2026-09-01 "
                "(memory://deadline-memory)."
            ),
            turn_id="turn",
        )

        self.assertIn("<altm_memory_context>", injected["context"])
        self.assertEqual(
            context.calls[0][0],
            "mcp_altm_memory_prepare_turn",
        )
        self.assertEqual(context.calls[0][1]["user_id"], "user")
        self.assertEqual(
            context.calls[1][0],
            "mcp_altm_memory_commit_turn",
        )
        self.assertEqual(
            context.calls[1][1]["cited_memory_ids"],
            ["deadline-memory"],
        )


if __name__ == "__main__":
    unittest.main()
