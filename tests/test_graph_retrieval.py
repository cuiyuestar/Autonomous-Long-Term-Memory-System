import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from altm.contracts import (  # noqa: E402
    GraphEdgeSpec,
    GraphEdgeType,
    GraphExtraction,
    GraphNodeSpec,
    GraphNodeType,
    LifecycleState,
    MemoryLayer,
    MemoryScope,
    MemoryStatus,
    MemoryUnit,
    RecallQuery,
)
from altm.retrieval import (  # noqa: E402
    FTSRetrievalEngine,
    HeterogeneousGraphRetriever,
    reciprocal_rank_scores,
)
from altm.storage import SQLiteMemoryStore  # noqa: E402
from altm.utils import sha256_text, utc_now_iso  # noqa: E402


class GraphRetrievalTest(unittest.TestCase):
    def test_temporal_ppr_returns_memory_with_minimal_supporting_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(Path(tmpdir) / "memory.sqlite3", "agent-a")
            memory = _memory(
                "release-task",
                "Release ALTM before the planned date.",
                store.scope,
            )
            store.put_memory_unit(memory)
            store.put_graph_extraction(_graph(memory.id), model="graph-model")

            candidates = HeterogeneousGraphRetriever(store).recall(
                RecallQuery(text="What is due on 2026-09-01?", top_k=5)
            )

            self.assertEqual([candidate.memory.id for candidate in candidates], [memory.id])
            candidate = candidates[0]
            self.assertIn("graph_ppr", candidate.matched_by)
            self.assertIn("graph_temporal", candidate.matched_by)
            graph = candidate.metadata["graph"]
            self.assertEqual(
                [edge["edge_type"] for edge in graph["edges"]],
                ["deadline_for"],
            )
            self.assertEqual(len(graph["nodes"]), 2)

    def test_entity_alias_hits_graph_and_default_rrf_recall(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(Path(tmpdir) / "memory.sqlite3", "agent-a")
            memory = _memory(
                "release-task",
                "The release task is active.",
                store.scope,
            )
            store.put_memory_unit(memory)
            store.put_graph_extraction(_graph(memory.id), model="graph-model")

            candidates = FTSRetrievalEngine(store).recall(
                RecallQuery(text="Advanced Long-Term Memory", top_k=5)
            )

            self.assertEqual(candidates[0].memory.id, memory.id)
            self.assertIn("graph_ppr", candidates[0].matched_by)
            self.assertEqual(
                candidates[0].metadata["rank_fusion"]["strategy"],
                "rrf",
            )
            self.assertEqual(
                candidates[0].metadata["graph"]["edges"][0]["edge_type"],
                "related_to",
            )

    def test_graph_recall_is_agent_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            owner = _store(db_path, "agent-a")
            other = _store(db_path, "agent-b")
            memory = _memory("private-task", "Private release task.", owner.scope)
            owner.put_memory_unit(memory)
            owner.put_graph_extraction(_graph(memory.id), model="graph-model")

            recalled = HeterogeneousGraphRetriever(other).recall(
                RecallQuery(text="Advanced Long-Term Memory", top_k=5)
            )

            self.assertEqual(recalled, [])

    def test_canonical_node_retains_evidence_across_extractions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _store(Path(tmpdir) / "memory.sqlite3", "agent-a")
            first = _memory("first", "First ALTM observation.", store.scope)
            second = _memory("second", "Second ALTM observation.", store.scope)
            store.put_memory_units([first, second])
            store.put_graph_extraction(
                _entity_only_graph(first.id),
                model="graph-model",
            )
            store.put_graph_extraction(
                _entity_only_graph(second.id),
                model="graph-model",
            )

            nodes = store.search_graph_nodes("ALTM", limit=5)

            self.assertEqual(len(nodes), 1)
            self.assertEqual(
                set(nodes[0]["evidence_memory_ids"]),
                {first.id, second.id},
            )

    def test_reciprocal_rank_fusion_is_deterministic(self) -> None:
        scores = reciprocal_rank_scores(
            [
                ["a", "b", "c"],
                ["b", "a"],
                ["a", "a"],
            ],
            rank_constant=60,
        )

        self.assertGreater(scores["a"], scores["b"])
        self.assertGreater(scores["b"], scores["c"])
        self.assertAlmostEqual(
            scores["a"],
            (1 / 61) + (1 / 62) + (1 / 61),
        )


def _store(db_path: Path, agent_id: str) -> SQLiteMemoryStore:
    store = SQLiteMemoryStore(db_path, scope=_scope(agent_id))
    store.initialize()
    return store


def _scope(agent_id: str) -> MemoryScope:
    return MemoryScope(
        tenant_id="tenant",
        workspace_id="workspace",
        user_id="user",
        agent_id=agent_id,
    )


def _memory(
    memory_id: str,
    content: str,
    scope: MemoryScope | None,
) -> MemoryUnit:
    if scope is None:
        raise AssertionError("test store must have scope")
    now = utc_now_iso()
    return MemoryUnit(
        id=memory_id,
        scope=scope,
        layer=MemoryLayer.L2,
        lifecycle_state=LifecycleState.SHORT,
        status=MemoryStatus.ACTIVE,
        content=content,
        content_hash=sha256_text(content),
        summary=content,
        created_at=now,
        updated_at=now,
        metadata={"session_id": "session"},
    )


def _graph(memory_id: str) -> GraphExtraction:
    return GraphExtraction(
        nodes=[
            GraphNodeSpec(
                local_id="entity",
                node_type=GraphNodeType.ENTITY,
                name="ALTM",
                canonical_key="entity:altm",
                attributes={"aliases": ["Advanced Long-Term Memory"]},
            ),
            GraphNodeSpec(
                local_id="task",
                node_type=GraphNodeType.TASK,
                name="Release ALTM",
                canonical_key="task:release-altm",
                memory_unit_id=memory_id,
                attributes={"status": "active"},
            ),
            GraphNodeSpec(
                local_id="time",
                node_type=GraphNodeType.TIME,
                name="2026-09-01",
                canonical_key="time:2026-09-01",
                attributes={"iso": "2026-09-01"},
            ),
        ],
        edges=[
            GraphEdgeSpec(
                source_local_id="entity",
                target_local_id="task",
                edge_type=GraphEdgeType.RELATED_TO,
                confidence=0.95,
            ),
            GraphEdgeSpec(
                source_local_id="time",
                target_local_id="task",
                edge_type=GraphEdgeType.DEADLINE_FOR,
                confidence=0.97,
            ),
        ],
        evidence_memory_ids=[memory_id],
        confidence=0.96,
    )


def _entity_only_graph(memory_id: str) -> GraphExtraction:
    return GraphExtraction(
        nodes=[
            GraphNodeSpec(
                local_id="entity",
                node_type=GraphNodeType.ENTITY,
                name="ALTM",
                canonical_key="entity:altm",
                memory_unit_id=memory_id,
            )
        ],
        edges=[],
        evidence_memory_ids=[memory_id],
        confidence=0.9,
    )


if __name__ == "__main__":
    unittest.main()
