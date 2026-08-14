"""Verify query-induced memory emergence with a real configured graph model."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from altm.application import AltmApplication
from altm.contracts import (
    LifecycleState,
    MemoryLayer,
    MemoryScope,
    MemoryStatus,
    MemoryUnit,
    RecallQuery,
)
from altm.folding import GraphLLMExtractor
from altm.retrieval import FTSRetrievalEngine, QueryEmergenceEngine
from altm.storage import SQLiteMemoryStore
from altm.utils import sha256_text, utc_now_iso

QUERY = "ORIONANCHOR9F3"
SESSION_ID = "controlled-emergence"
SCOPE = MemoryScope(
    tenant_id="verify",
    workspace_id="emergence",
    user_id="tester",
    agent_id="harness",
)


def build_memory(memory_id: str, content: str, atom_type: str) -> MemoryUnit:
    now = utc_now_iso()
    return MemoryUnit(
        id=memory_id,
        scope=SCOPE,
        layer=MemoryLayer.L2,
        lifecycle_state=LifecycleState.SHORT,
        status=MemoryStatus.ACTIVE,
        content=content,
        content_hash=sha256_text(content),
        summary=content,
        created_at=now,
        updated_at=now,
        metadata={
            "session_id": SESSION_ID,
            "atom_type": atom_type,
            "review_status": "approved",
        },
    )


def verify(db_path: Path) -> dict[str, object]:
    app = AltmApplication(db_path)
    store = SQLiteMemoryStore(db_path, scope=SCOPE)
    store.initialize()
    seed = build_memory(
        "seed-orion",
        "ORIONANCHOR9F3 release is delayed because the packaging workflow is blocked.",
        "issue",
    )
    neighbor = build_memory(
        "neighbor-certificate",
        "The supplier signing certificate is unavailable and directly causes "
        "the packaging workflow block.",
        "project_fact",
    )
    unrelated = build_memory(
        "unrelated",
        "The office lunch menu changed on Friday.",
        "project_fact",
    )
    for memory in (seed, neighbor, unrelated):
        store.put_memory_unit(memory)

    before = app.build_context(
        query=QUERY,
        token_budget=1200,
        limit=10,
        active_window_mode="off",
        scope=SCOPE,
    )
    before_ids = {
        memory_id
        for item in before.items
        for memory_id in item.source_memory_ids
    }

    extraction = GraphLLMExtractor(store).extract_session(SESSION_ID)
    controlled_edges = [
        edge
        for edge in store.list_graph_edges()
        if {edge["source_memory_id"], edge["target_memory_id"]}
        == {seed.id, neighbor.id}
    ]
    if not controlled_edges:
        raise AssertionError(
            {
                "reason": "real graph extraction did not connect controlled memories",
                "extraction": extraction,
                "all_edges": store.list_graph_edges(),
            }
        )

    after = app.build_context(
        query=QUERY,
        token_budget=1200,
        limit=10,
        active_window_mode="off",
        scope=SCOPE,
    )
    after_by_id = {
        memory_id: item
        for item in after.items
        for memory_id in item.source_memory_ids
    }

    query = RecallQuery(
        text=QUERY,
        top_k=10,
        preferred_layers=[MemoryLayer.L2],
        session_id=SESSION_ID,
    )
    engine = QueryEmergenceEngine(store, FTSRetrievalEngine(store))
    zero_hop_items = list(
        engine.emerge(query, seed_limit=1, max_hops=0)
    )
    two_hop_items = list(
        engine.emerge(query, seed_limit=1, max_hops=2)
    )
    repeated_items = list(
        engine.emerge(query, seed_limit=1, max_hops=2)
    )
    zero_hop_ids = {item.memory.id for item in zero_hop_items}
    two_hop_ids = {item.memory.id for item in two_hop_items}
    two_hop_by_id = {
        item.memory.id: item
        for item in two_hop_items
    }

    for edge in controlled_edges:
        store.update_graph_edge_metadata(
            str(edge["id"]),
            {"review_status": "rejected"},
        )
    rejected_ids = {
        item.memory.id
        for item in engine.emerge(query, seed_limit=1, max_hops=2)
    }
    for edge in controlled_edges:
        store.update_graph_edge_metadata(
            str(edge["id"]),
            {"review_status": "approved"},
        )

    other_scope = MemoryScope(
        tenant_id="verify",
        workspace_id="emergence",
        user_id="tester",
        agent_id="other-agent",
    )
    other_store = SQLiteMemoryStore(db_path, scope=other_scope)
    other_store.initialize()
    cross_scope = list(
        QueryEmergenceEngine(
            other_store,
            FTSRetrievalEngine(other_store),
        ).emerge(query, seed_limit=1, max_hops=2)
    )

    store.put_memory_unit(
        neighbor.model_copy(
            update={
                "status": MemoryStatus.TOMBSTONED,
                "updated_at": utc_now_iso(),
            }
        )
    )
    tombstoned_ids = {
        item.memory.id
        for item in engine.emerge(query, seed_limit=1, max_hops=2)
    }

    neighbor_context = after_by_id.get(neighbor.id)
    neighbor_emerged = two_hop_by_id.get(neighbor.id)
    report: dict[str, object] = {
        "graph_extraction_status": extraction["status"],
        "controlled_edge_count": len(controlled_edges),
        "controlled_edge_types": sorted(
            {str(edge["edge_type"]) for edge in controlled_edges}
        ),
        "context_before_ids": sorted(before_ids),
        "context_after": [
            {
                "id": memory_id,
                "matched_by": item.metadata.get("matched_by"),
            }
            for memory_id, item in after_by_id.items()
        ],
        "context_graph_only_neighbor": (
            neighbor.id not in before_ids
            and neighbor_context is not None
            and "graph_ppr"
            in neighbor_context.metadata.get("matched_by", [])
        ),
        "zero_hop_ids": sorted(zero_hop_ids),
        "two_hop": [
            {
                "id": item.memory.id,
                "matched_by": item.matched_by,
                "contains_query": (
                    QUERY.lower() in item.memory.content.lower()
                ),
            }
            for item in two_hop_items
        ],
        "positive_query_emergence": (
            neighbor.id not in zero_hop_ids
            and neighbor.id in two_hop_ids
            and neighbor_emerged is not None
            and "graph_ppr" in neighbor_emerged.matched_by
        ),
        "neighbor_contains_query": (
            QUERY.lower() in neighbor.content.lower()
        ),
        "repeat_stable": (
            [item.memory.id for item in two_hop_items]
            == [item.memory.id for item in repeated_items]
        ),
        "rejected_all_paths_excludes_neighbor": (
            neighbor.id not in rejected_ids
        ),
        "cross_scope_empty": len(cross_scope) == 0,
        "tombstone_excludes_neighbor": (
            neighbor.id not in tombstoned_ids
        ),
    }
    report["passed"] = all(
        [
            report["graph_extraction_status"] == "complete",
            report["controlled_edge_count"] > 0,
            report["context_graph_only_neighbor"],
            report["positive_query_emergence"],
            not report["neighbor_contains_query"],
            report["repeat_stable"],
            report["rejected_all_paths_excludes_neighbor"],
            report["cross_scope_empty"],
            report["tombstone_excludes_neighbor"],
        ]
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional path for the JSON report.",
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(
        prefix="altm-emergence-live-"
    ) as tmpdir:
        report = verify(Path(tmpdir) / "verify.sqlite3")
    rendered = json.dumps(
        report,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
