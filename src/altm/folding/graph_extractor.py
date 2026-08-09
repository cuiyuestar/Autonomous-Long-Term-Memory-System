"""LLM-backed typed graph extraction from incremental L2 evidence."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, cast

from altm.contracts import (
    GraphEdgeSpec,
    GraphEdgeType,
    GraphExtraction,
    GraphNodeSpec,
    GraphNodeType,
    MemoryLayer,
)
from altm.llm import OpenAICompatibleClient, llm_config_from_env
from altm.storage import SQLiteMemoryStore
from altm.utils import stable_id

GRAPH_PROMPT = """Extract a typed memory graph from grounded L2 memories.

Return only JSON:
{
  "nodes": [
    {
      "local_id": "n1",
      "node_type": "user|agent|session|event|entity|task|intent|time|scene|persona",
      "name": "readable name",
      "canonical_key": "stable normalized key",
      "memory_unit_id": "optional source memory id",
      "attributes": {}
    }
  ],
  "edges": [
    {
      "source_local_id": "n1",
      "target_local_id": "n2",
      "edge_type": "participates_in|derived_from|supports|conflicts|related_to|causes|triggers|part_of|sequential|deadline_for|supersedes|has_intent|occurs_at",
      "confidence": 0.0,
      "attributes": {}
    }
  ],
  "confidence": 0.0
}

Rules:
- Only use facts supported by supplied L2 evidence.
- Preserve project, task, actor, time, and scope boundaries.
- Normalize equivalent entities to one canonical_key.
- Do not invent source memory ids.
- Set memory_unit_id on each factual node when a supplied memory directly supports it.
- Put alternate names and abbreviations in attributes.aliases.
- Use explicit TIME nodes for dates, deadlines, and ordered events.
"""


class GraphLLMExtractor:
    def __init__(self, store: SQLiteMemoryStore) -> None:
        if store.scope is None:
            raise RuntimeError("Graph extraction requires an explicit memory scope")
        self.store = store
        self.client = OpenAICompatibleClient(llm_config_from_env("graph"))

    def extract_session(
        self,
        session_id: str,
        limit: int = 100,
    ) -> dict[str, object]:
        scope = self.store.scope
        if scope is None:
            raise RuntimeError("Graph extraction requires an explicit memory scope")
        checkpoint_scope = stable_id(
            "checkpoint",
            "graph",
            *scope.key_parts(),
            session_id,
        )
        memories, next_cursor = self.store.list_unprocessed_session_memories(
            layer=MemoryLayer.L2,
            session_id=session_id,
            checkpoint_scope=checkpoint_scope,
            limit=limit,
        )
        if not memories:
            return {
                "status": "idle",
                "node_ids": [],
                "edge_ids": [],
            }
        response = self.client.chat_json(
            [
                {"role": "system", "content": GRAPH_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "scope": scope.model_dump(mode="json"),
                            "session_id": session_id,
                            "memories": [
                                {
                                    "memory_id": memory.id,
                                    "summary": memory.summary or memory.content,
                                    "atom_type": memory.metadata.get("atom_type"),
                                    "created_at": memory.created_at,
                                    "metadata": memory.metadata,
                                }
                                for memory in memories
                            ],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                },
            ]
        )
        extraction = _parse_graph_extraction(
            response,
            evidence_memory_ids=[memory.id for memory in memories],
        )
        result = self.store.put_graph_extraction(
            extraction,
            model=self.client.config.model,
        )
        self.store.put_checkpoint(
            checkpoint_scope,
            str(next_cursor),
            metadata={
                "stage": "graph",
                "session_id": session_id,
                "extraction_id": result["extraction_id"],
                "source_count": len(memories),
            },
        )
        return {
            "status": "complete",
            **result,
        }


def _parse_graph_extraction(
    response: dict[str, Any],
    evidence_memory_ids: Sequence[str],
) -> GraphExtraction:
    raw_nodes_value: object = response.get("nodes", [])
    raw_edges_value: object = response.get("edges", [])
    confidence: object = response.get("confidence")
    if not isinstance(raw_nodes_value, list) or not isinstance(raw_edges_value, list):
        raise ValueError("Graph extraction nodes and edges must be arrays")
    raw_nodes = cast(list[object], raw_nodes_value)
    raw_edges = cast(list[object], raw_edges_value)
    if not isinstance(confidence, (int, float)):
        raise ValueError("Graph extraction confidence must be numeric")
    allowed_memory_ids = set(evidence_memory_ids)
    nodes: list[GraphNodeSpec] = []
    local_ids: set[str] = set()
    for raw_node_value in raw_nodes:
        if not isinstance(raw_node_value, dict):
            raise ValueError("Graph node must be an object")
        raw_node = _object_dict(cast(object, raw_node_value))
        node = GraphNodeSpec(
            local_id=str(raw_node.get("local_id", "")),
            node_type=GraphNodeType(str(raw_node.get("node_type", ""))),
            name=str(raw_node.get("name", "")),
            canonical_key=str(raw_node.get("canonical_key", "")),
            memory_unit_id=(
                str(raw_node["memory_unit_id"])
                if raw_node.get("memory_unit_id") is not None
                else None
            ),
            attributes=_object_dict(raw_node.get("attributes")),
        )
        if not node.local_id or not node.name or not node.canonical_key:
            raise ValueError("Graph node identity fields must not be empty")
        if node.local_id in local_ids:
            raise ValueError("Graph node local_id must be unique")
        if (
            node.memory_unit_id is not None
            and node.memory_unit_id not in allowed_memory_ids
        ):
            raise ValueError("Graph node references a non-evidence memory")
        local_ids.add(node.local_id)
        nodes.append(node)

    edges: list[GraphEdgeSpec] = []
    for raw_edge_value in raw_edges:
        if not isinstance(raw_edge_value, dict):
            raise ValueError("Graph edge must be an object")
        raw_edge = _object_dict(cast(object, raw_edge_value))
        edge = GraphEdgeSpec(
            source_local_id=str(raw_edge.get("source_local_id", "")),
            target_local_id=str(raw_edge.get("target_local_id", "")),
            edge_type=GraphEdgeType(str(raw_edge.get("edge_type", ""))),
            confidence=_float_value(raw_edge.get("confidence"), "edge confidence"),
            attributes=_object_dict(raw_edge.get("attributes")),
        )
        if (
            edge.source_local_id not in local_ids
            or edge.target_local_id not in local_ids
        ):
            raise ValueError("Graph edge references an unknown local node")
        edges.append(edge)
    return GraphExtraction(
        nodes=nodes,
        edges=edges,
        evidence_memory_ids=list(evidence_memory_ids),
        confidence=float(confidence),
        metadata={"extractor": "graph_llm"},
    )


def _object_dict(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Graph attributes must be objects")
    return {
        str(key): item
        for key, item in cast(dict[object, object], value).items()
    }


def _float_value(value: object, label: str) -> float:
    if not isinstance(value, (int, float)):
        raise ValueError("Graph %s must be numeric" % label)
    return float(value)
