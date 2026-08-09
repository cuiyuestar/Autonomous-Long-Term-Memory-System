import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from altm.contracts import (  # noqa: E402
    AccessSignal,
    EvidenceRelation,
    LifecycleState,
    MemoryLayer,
    MemoryScope,
    MemoryStatus,
    MemoryUnit,
    MemoryVisibility,
    PersonaFacet,
    PersonaStatus,
)
from altm.folding import SemanticL3SceneBuilder, SemanticL4PersonaDistiller  # noqa: E402
from altm.lifecycle import PersonaLifecycleManager, SceneLifecycleManager  # noqa: E402
from altm.llm import SemanticModelChain  # noqa: E402
from altm.storage import SQLiteMemoryStore  # noqa: E402
from altm.utils import sha256_text, utc_now_iso  # noqa: E402


class SemanticHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        system = payload["messages"][0]["content"]
        if "Create one grounded L3 SceneBlock" in system:
            result = {
                "title": "SQLite memory workflow",
                "scene_type": "workflow",
                "summary": "The Agent consistently uses SQLite for local memory work.",
                "active_facts": ["SQLite is the local memory store."],
                "historical_facts": [],
                "open_questions": ["Select the production vector index."],
                "known_risks": ["A full table vector scan will not scale."],
            }
        elif "Create one grounded L4 persona facet" in system:
            result = {
                "facet_key": "communication_language",
                "facet_type": "preference",
                "statement": "The user prefers Chinese technical explanations.",
                "workspace_scope": "all Agents in this workspace",
            }
        elif "Allowed labels: match, mismatch" in system:
            result = {"label": "match", "score": 0.96, "reason": "evidence matches"}
        elif "Allowed labels: entails, contradicts, neutral" in system:
            result = {"label": "entails", "score": 0.95, "reason": "no contradiction"}
        elif "Allowed labels: same_boundary, boundary_risk" in system:
            result = {
                "label": "same_boundary",
                "score": 0.94,
                "reason": "same workflow boundary",
            }
        elif "Allowed labels: same_scene, weak_relation, reject" in system:
            result = {
                "label": "same_scene",
                "score": 0.95,
                "reason": "one coherent scene",
            }
        elif "Allowed labels: user_workspace, project, temporary" in system:
            result = {
                "label": "user_workspace",
                "score": 0.97,
                "reason": "stable workspace-wide preference",
            }
        elif "Allowed labels: stable, observe, reject" in system:
            result = {
                "label": "stable",
                "score": 0.95,
                "reason": "cross-context stable evidence",
            }
        else:
            raise AssertionError("Unexpected semantic prompt: %s" % system)

        content = json.dumps(result)
        encoded = json.dumps(
            {"choices": [{"message": {"content": content}}]}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return


class SemanticL3L4Test(unittest.TestCase):
    def test_semantic_chain_defers_when_required_models_are_missing(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            result = SemanticModelChain.for_scene().evaluate_scene(
                payload={"source": "a", "target": "b"},
                evidence_memory_ids=["a", "b"],
            )

        self.assertEqual(result.decision, "defer")
        self.assertEqual(result.confidence, 0.0)
        self.assertIn("errors", result.metadata)

    def test_l3_typed_scene_is_written_as_observing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path, scope=_scope("agent-a"))
            store.initialize()
            first = _l2(
                "decision-a",
                "SQLite is the local memory store.",
                store.scope,
                session_id="session-a",
                atom_type="decision",
            )
            second = _l2(
                "decision-b",
                "The Agent uses SQLite for local memory.",
                store.scope,
                session_id="session-b",
                atom_type="decision",
            )
            for memory, vector in ((first, [1.0, 0.0]), (second, [0.99, 0.01])):
                store.put_memory_unit(memory)
                store.put_memory_embedding(memory.id, "embedding", memory.content_hash, vector)

            with _semantic_server_env():
                created = SemanticL3SceneBuilder(
                    store,
                    embedding_model="embedding",
                    similarity_threshold=0.90,
                ).build()

            self.assertEqual(len(created), 1)
            self.assertEqual(created[0].status, MemoryStatus.OBSERVING)
            self.assertEqual(created[0].lifecycle_state, LifecycleState.SHORT)
            with store.connect() as connection:
                row = connection.execute(
                    "SELECT scene_type, observation_cycles FROM l3_scenes"
                ).fetchone()
            self.assertEqual(row["scene_type"], "workflow")
            self.assertEqual(row["observation_cycles"], 1)

            store.record_access_signal(created[0].id, AccessSignal.USER_CONFIRMED)
            with _semantic_server_env():
                SemanticL3SceneBuilder(
                    store,
                    embedding_model="embedding",
                    similarity_threshold=0.90,
                ).build()
            activated = SceneLifecycleManager(store).advance()
            self.assertEqual(len(activated), 1)
            self.assertEqual(activated[0].status, MemoryStatus.ACTIVE)
            self.assertEqual(activated[0].lifecycle_state, LifecycleState.LONG)

    def test_l4_typed_persona_is_shared_but_private_l2_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store_a = SQLiteMemoryStore(db_path, scope=_scope("agent-a"))
            store_b = SQLiteMemoryStore(db_path, scope=_scope("agent-b"))
            store_a.initialize()
            store_b.initialize()
            first = _l2(
                "preference-a",
                "The user prefers Chinese technical explanations.",
                store_a.scope,
                session_id="session-a",
                atom_type="preference",
            )
            second = _l2(
                "preference-b",
                "Please continue providing deep technical answers in Chinese.",
                store_b.scope,
                session_id="session-b",
                atom_type="preference",
            )
            store_a.put_memory_unit(first)
            store_b.put_memory_unit(second)
            store_a.put_memory_embedding(
                first.id,
                "embedding",
                first.content_hash,
                [1.0, 0.0],
            )
            store_b.put_memory_embedding(
                second.id,
                "embedding",
                second.content_hash,
                [0.99, 0.01],
            )

            with _semantic_server_env():
                created = SemanticL4PersonaDistiller(
                    store_a,
                    embedding_model="embedding",
                    similarity_threshold=0.90,
                ).distill()

            self.assertEqual(len(created), 1)
            persona = created[0]
            self.assertEqual(persona.visibility, MemoryVisibility.USER_WORKSPACE)
            self.assertEqual(persona.status, MemoryStatus.OBSERVING)
            self.assertEqual(persona.lifecycle_state, LifecycleState.SHORT)
            self.assertIsNotNone(store_b.get_memory_unit(persona.id))
            self.assertIsNone(store_b.get_memory_unit(first.id))
            with store_a.connect() as connection:
                row = connection.execute(
                    """
                    SELECT status, source_agent_ids_json, observation_cycles
                    FROM l4_persona_facets
                    """
                ).fetchone()
            self.assertEqual(row["status"], "observing")
            self.assertEqual(
                set(json.loads(row["source_agent_ids_json"])),
                {"agent-a", "agent-b"},
            )
            self.assertEqual(row["observation_cycles"], 1)

            with _semantic_server_env():
                SemanticL4PersonaDistiller(
                    store_b,
                    embedding_model="embedding",
                    similarity_threshold=0.90,
                ).distill()
                SemanticL4PersonaDistiller(
                    store_a,
                    embedding_model="embedding",
                    similarity_threshold=0.90,
                ).distill()
            store_b.record_access_signal(persona.id, AccessSignal.USER_CONFIRMED)
            store_b.record_access_signal(persona.id, AccessSignal.USER_CONFIRMED)
            activated = PersonaLifecycleManager(store_b).advance()

            self.assertEqual(len(activated), 1)
            self.assertEqual(activated[0].status, MemoryStatus.ACTIVE)
            self.assertEqual(activated[0].lifecycle_state, LifecycleState.LONG)
            self.assertIsNotNone(store_a.get_memory_unit(persona.id))

    def test_high_confidence_new_evidence_supersedes_active_l4_facet(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(
                Path(tmpdir) / "memory.sqlite3",
                scope=_scope("agent-a"),
            )
            store.initialize()
            now = utc_now_iso()
            old_persona = PersonaFacet(
                id="old-persona",
                facet_key="response_detail",
                facet_type="preference",
                statement="The user prefers exhaustive responses.",
                workspace_scope="all Agents in this workspace",
                confidence=0.93,
                stability_score=0.93,
                status=PersonaStatus.ACTIVE,
                source_memory_ids=["old-evidence-a", "old-evidence-b"],
                source_agent_ids=["agent-a"],
                first_observed_at=now,
                last_observed_at=now,
                observation_cycles=4,
            )
            old_content = old_persona.model_dump_json()
            old_memory = MemoryUnit(
                id=old_persona.id,
                scope=store.scope,
                visibility=MemoryVisibility.USER_WORKSPACE,
                layer=MemoryLayer.L4,
                lifecycle_state=LifecycleState.LONG,
                status=MemoryStatus.ACTIVE,
                content=old_content,
                content_hash=sha256_text(old_content),
                summary=old_persona.statement,
                created_at=now,
                updated_at=now,
                metadata={
                    "facet_key": old_persona.facet_key,
                    "persona_status": "active",
                },
            )
            store.put_l4_persona(old_persona, old_memory)

            first = _l2(
                "concise-a",
                "The user now prefers concise technical responses.",
                store.scope,
                session_id="session-new-a",
                atom_type="preference",
            )
            second = _l2(
                "concise-b",
                "Keep future technical answers concise.",
                store.scope,
                session_id="session-new-b",
                atom_type="preference",
            )
            for memory, vector in (
                (first, [1.0, 0.0]),
                (second, [0.99, 0.01]),
            ):
                store.put_memory_unit(memory)
                store.put_memory_embedding(
                    memory.id,
                    "embedding",
                    memory.content_hash,
                    vector,
                )

            with _semantic_server_env():
                distiller = SemanticL4PersonaDistiller(
                    store,
                    embedding_model="embedding",
                    similarity_threshold=0.90,
                )
                distiller.synthesizer = _SupersessionSynthesizer(
                    old_persona.id
                )
                created = distiller.distill()

            self.assertEqual(len(created), 1)
            replacement = created[0]
            self.assertEqual(replacement.status, MemoryStatus.ACTIVE)
            self.assertEqual(replacement.lifecycle_state, LifecycleState.LONG)
            self.assertEqual(
                replacement.metadata["supersedes_persona_id"],
                old_persona.id,
            )
            old_stored = store.get_memory_unit(old_persona.id)
            self.assertIsNotNone(old_stored)
            self.assertEqual(old_stored.status, MemoryStatus.TOMBSTONED)
            self.assertEqual(
                old_stored.metadata["superseded_by"],
                replacement.id,
            )
            self.assertTrue(
                any(
                    ref.target_id == old_persona.id
                    and ref.relation == EvidenceRelation.SUPERSEDES
                    for ref in replacement.evidence_refs
                )
            )
            with store.connect() as connection:
                old_row = connection.execute(
                    "SELECT status FROM l4_persona_facets WHERE id = ?",
                    (old_persona.id,),
                ).fetchone()
                history = connection.execute(
                    "SELECT * FROM l4_persona_history"
                ).fetchone()
            self.assertEqual(old_row["status"], "superseded")
            self.assertEqual(
                history["replacement_persona_id"],
                replacement.id,
            )
            snapshot = json.loads(history["snapshot_json"])
            self.assertEqual(
                snapshot["persona"]["statement"],
                old_persona.statement,
            )


class _SupersessionSynthesizer:
    def __init__(self, previous_persona_id: str) -> None:
        self.previous_persona_id = previous_persona_id

    def chat_json(
        self,
        messages: list[dict[str, str]],
    ) -> dict[str, object]:
        system = messages[0]["content"]
        if "Create one grounded L4 persona facet" in system:
            return {
                "facet_key": "response_detail",
                "facet_type": "preference",
                "statement": "The user prefers concise technical responses.",
                "workspace_scope": "all Agents in this workspace",
            }
        if "Resolve a possible L4 persona change" in system:
            payload = json.loads(messages[1]["content"])
            return {
                "decision": "supersede",
                "supersedes_persona_id": self.previous_persona_id,
                "facet_key": "response_detail",
                "facet_type": "preference",
                "statement": "The user prefers concise technical responses.",
                "workspace_scope": "all Agents in this workspace",
                "confidence": 0.97,
                "reason": "Repeated newer evidence clearly reverses the preference.",
                "supporting_memory_ids": [
                    item["memory_id"]
                    for item in payload["evidence"]
                ],
            }
        raise AssertionError("Unexpected synthesis prompt: %s" % system)


class _semantic_server_env:
    def __enter__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SemanticHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.env = patch.dict(
            "os.environ",
            {
                "ALTM_LLM_BASE_URL": "http://127.0.0.1:%s/v1"
                % self.server.server_port,
                "ALTM_LLM_API_KEY": "test-key",
                "ALTM_LLM_MODEL": "semantic-test-model",
                "ALTM_SEMANTIC_MIN_SCORE": "0.80",
            },
            clear=True,
        )
        self.env.start()

    def __exit__(self, *args: object) -> None:
        self.env.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _scope(agent_id: str) -> MemoryScope:
    return MemoryScope(
        tenant_id="tenant",
        workspace_id="workspace",
        user_id="user",
        agent_id=agent_id,
    )


def _l2(
    memory_id: str,
    summary: str,
    scope: MemoryScope,
    session_id: str,
    atom_type: str,
) -> MemoryUnit:
    now = utc_now_iso()
    return MemoryUnit(
        id=memory_id,
        scope=scope,
        layer=MemoryLayer.L2,
        lifecycle_state=LifecycleState.SHORT,
        status=MemoryStatus.ACTIVE,
        content=summary,
        content_hash=sha256_text(summary),
        summary=summary,
        created_at=now,
        updated_at=now,
        metadata={
            "session_id": session_id,
            "atom_type": atom_type,
            "review_status": "approved",
        },
    )


if __name__ == "__main__":
    unittest.main()
