"""Scoped heterogeneous graph retrieval with PPR and path explanations."""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from altm.contracts import (
    MemoryStatus,
    MemoryUnit,
    RecallCandidate,
    RecallQuery,
    ScoreBreakdown,
)
from altm.lifecycle import adjust_retrieval_score
from altm.storage import SQLiteMemoryStore


@dataclass(frozen=True)
class GraphRetrievalPolicy:
    seed_limit: int = 16
    max_hops: int = 3
    subgraph_node_limit: int = 512
    damping: float = 0.85
    max_iterations: int = 50
    tolerance: float = 1e-8
    min_ppr_score: float = 1e-8


class HeterogeneousGraphRetriever:
    """Recall MemoryUnits through typed Entity/Temporal graph neighborhoods."""

    def __init__(
        self,
        store: SQLiteMemoryStore,
        policy: GraphRetrievalPolicy | None = None,
    ) -> None:
        self.store = store
        self.policy = policy or GraphRetrievalPolicy()

    def recall(self, query: RecallQuery) -> Sequence[RecallCandidate]:
        if self.store.scope is None:
            return []
        seeds = list(
            self.store.search_graph_nodes(
                query.text,
                limit=self.policy.seed_limit,
            )
        )
        if not seeds:
            return []
        seed_ids = [str(seed["id"]) for seed in seeds]
        raw_subgraph = self.store.get_graph_subgraph(
            seed_ids,
            max_hops=self.policy.max_hops,
            node_limit=self.policy.subgraph_node_limit,
        )
        nodes = _object_list(raw_subgraph.get("nodes"))
        edges = _object_list(raw_subgraph.get("edges"))
        if not nodes:
            return []

        nodes_by_id = {str(node["id"]): node for node in nodes}
        adjacency, edges_by_id = _build_adjacency(edges)
        personalization = _personalization(seed_ids, nodes_by_id)
        ranks = _personalized_page_rank(
            node_ids=list(nodes_by_id),
            adjacency=adjacency,
            personalization=personalization,
            damping=self.policy.damping,
            max_iterations=self.policy.max_iterations,
            tolerance=self.policy.tolerance,
        )
        if not ranks:
            return []

        memory_scores: dict[str, float] = {}
        memory_target_nodes: dict[str, str] = {}
        for node_id, graph_score in sorted(
            ranks.items(),
            key=lambda item: (-item[1], item[0]),
        ):
            if graph_score < self.policy.min_ppr_score:
                continue
            node = nodes_by_id[node_id]
            for memory_id in _string_list(node.get("evidence_memory_ids")):
                memory_scores[memory_id] = (
                    memory_scores.get(memory_id, 0.0) + graph_score
                )
                current_target = memory_target_nodes.get(memory_id)
                if current_target is None or ranks.get(current_target, 0.0) < graph_score:
                    memory_target_nodes[memory_id] = node_id

        if not memory_scores:
            return []
        max_score = max(memory_scores.values())
        candidates: list[RecallCandidate] = []
        for memory_id, graph_score in sorted(
            memory_scores.items(),
            key=lambda item: (-item[1], item[0]),
        ):
            memory = self.store.get_memory_unit(memory_id)
            if memory is None or not _memory_allowed(memory, query):
                continue
            normalized_score = graph_score / max_score if max_score > 0 else 0.0
            retrieval_score = adjust_retrieval_score(memory, normalized_score)
            if retrieval_score <= 0:
                continue
            target_node_id = memory_target_nodes[memory_id]
            path_node_ids, path_edge_ids = _shortest_path(
                seed_ids=seed_ids,
                target_node_id=target_node_id,
                adjacency=adjacency,
            )
            path_nodes = [
                _node_explanation(nodes_by_id[node_id])
                for node_id in path_node_ids
                if node_id in nodes_by_id
            ]
            path_edges = [
                _edge_explanation(edges_by_id[edge_id])
                for edge_id in path_edge_ids
                if edge_id in edges_by_id
            ]
            seed_types = {
                str(nodes_by_id[node_id].get("node_type"))
                for node_id in path_node_ids[:1]
                if node_id in nodes_by_id
            }
            matched_by = ["graph_ppr", "graph_subgraph"]
            if "entity" in seed_types:
                matched_by.append("graph_entity")
            if "time" in seed_types:
                matched_by.append("graph_temporal")
            candidates.append(
                RecallCandidate(
                    memory=memory,
                    score=ScoreBreakdown(
                        retrieval_score=retrieval_score,
                        resident_score=memory.score.resident_score,
                        structural=memory.score.structural,
                        recency=memory.score.recency,
                        access=memory.score.access,
                        semantic=graph_score,
                        evidence_quality=memory.score.evidence_quality,
                    ),
                    matched_by=matched_by,
                    explanation=(
                        "Typed graph recall used personalized PageRank and the "
                        "shortest supporting path."
                    ),
                    metadata={
                        "graph": {
                            "seed_node_id": path_node_ids[0] if path_node_ids else None,
                            "target_node_id": target_node_id,
                            "ppr_score": graph_score,
                            "nodes": path_nodes,
                            "edges": path_edges,
                        }
                    },
                )
            )
            if len(candidates) >= query.top_k:
                break
        return candidates


def _build_adjacency(
    edges: Sequence[dict[str, object]],
) -> tuple[
    dict[str, list[tuple[str, str, float]]],
    dict[str, dict[str, object]],
]:
    adjacency: dict[str, list[tuple[str, str, float]]] = {}
    edges_by_id: dict[str, dict[str, object]] = {}
    for edge in edges:
        metadata = edge.get("metadata")
        if (
            isinstance(metadata, dict)
            and cast(dict[object, object], metadata).get("review_status")
            == "rejected"
        ):
            continue
        edge_id = str(edge["id"])
        source_id = str(edge["source_node_id"])
        target_id = str(edge["target_node_id"])
        weight = max(
            0.0,
            _float_value(edge.get("weight"))
            * _float_value(edge.get("confidence")),
        )
        if weight <= 0:
            continue
        edges_by_id[edge_id] = edge
        adjacency.setdefault(source_id, []).append((target_id, edge_id, weight))
        adjacency.setdefault(target_id, []).append((source_id, edge_id, weight))
    for values in adjacency.values():
        values.sort(key=lambda item: (item[0], item[1]))
    return adjacency, edges_by_id


def _personalization(
    seed_ids: Sequence[str],
    nodes_by_id: dict[str, dict[str, object]],
) -> dict[str, float]:
    raw: dict[str, float] = {}
    for rank, node_id in enumerate(seed_ids, start=1):
        if node_id in nodes_by_id and node_id not in raw:
            raw[node_id] = 1.0 / float(rank)
    total = sum(raw.values())
    if total <= 0:
        return {}
    return {node_id: score / total for node_id, score in raw.items()}


def _personalized_page_rank(
    node_ids: Sequence[str],
    adjacency: dict[str, list[tuple[str, str, float]]],
    personalization: dict[str, float],
    damping: float,
    max_iterations: int,
    tolerance: float,
) -> dict[str, float]:
    if not node_ids or not personalization:
        return {}
    ranks = {
        node_id: personalization.get(node_id, 0.0)
        for node_id in node_ids
    }
    for _ in range(max_iterations):
        dangling_mass = sum(
            ranks[node_id]
            for node_id in node_ids
            if not adjacency.get(node_id)
        )
        next_ranks = {
            node_id: (
                (1.0 - damping) * personalization.get(node_id, 0.0)
                + damping * dangling_mass * personalization.get(node_id, 0.0)
            )
            for node_id in node_ids
        }
        for node_id in node_ids:
            neighbors = adjacency.get(node_id, ())
            total_weight = sum(weight for _, _, weight in neighbors)
            if total_weight <= 0:
                continue
            for neighbor_id, _, weight in neighbors:
                next_ranks[neighbor_id] = next_ranks.get(neighbor_id, 0.0) + (
                    damping * ranks[node_id] * weight / total_weight
                )
        delta = sum(
            abs(next_ranks.get(node_id, 0.0) - ranks.get(node_id, 0.0))
            for node_id in node_ids
        )
        ranks = next_ranks
        if delta <= tolerance:
            break
    total = sum(ranks.values())
    if total <= 0:
        return {}
    return {node_id: score / total for node_id, score in ranks.items()}


def _shortest_path(
    seed_ids: Sequence[str],
    target_node_id: str,
    adjacency: dict[str, list[tuple[str, str, float]]],
) -> tuple[list[str], list[str]]:
    queue: deque[str] = deque()
    predecessor: dict[str, tuple[str, str] | None] = {}
    for seed_id in seed_ids:
        if seed_id in predecessor:
            continue
        predecessor[seed_id] = None
        queue.append(seed_id)
    while queue:
        node_id = queue.popleft()
        if node_id == target_node_id:
            break
        for neighbor_id, edge_id, _ in adjacency.get(node_id, ()):
            if neighbor_id in predecessor:
                continue
            predecessor[neighbor_id] = (node_id, edge_id)
            queue.append(neighbor_id)
    if target_node_id not in predecessor:
        return [target_node_id], []

    node_path = [target_node_id]
    edge_path: list[str] = []
    cursor = target_node_id
    while True:
        previous = predecessor[cursor]
        if previous is None:
            break
        parent_id, edge_id = previous
        node_path.append(parent_id)
        edge_path.append(edge_id)
        cursor = parent_id
    node_path.reverse()
    edge_path.reverse()
    return node_path, edge_path


def _memory_allowed(memory: MemoryUnit, query: RecallQuery) -> bool:
    if memory.status in {MemoryStatus.DELETED, MemoryStatus.TOMBSTONED}:
        return False
    if query.preferred_layers and memory.layer not in query.preferred_layers:
        return False
    if query.statuses and memory.status not in query.statuses:
        return False
    return not (
        query.session_id is not None
        and memory.metadata.get("session_id") != query.session_id
    )


def _object_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, object]] = []
    for item in cast(list[object], value):
        if isinstance(item, dict):
            result.append(
                {
                    str(key): nested
                    for key, nested in cast(
                        dict[object, object],
                        item,
                    ).items()
                }
            )
    return result


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in cast(list[object], value)]


def _float_value(value: object) -> float:
    if not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _node_explanation(node: dict[str, object]) -> dict[str, object]:
    return {
        "id": node["id"],
        "node_type": node.get("node_type"),
        "name": node.get("name"),
        "canonical_key": node.get("canonical_key"),
    }


def _edge_explanation(edge: dict[str, object]) -> dict[str, object]:
    return {
        "id": edge["id"],
        "source_node_id": edge.get("source_node_id"),
        "target_node_id": edge.get("target_node_id"),
        "edge_type": edge.get("edge_type"),
        "confidence": edge.get("confidence"),
    }
