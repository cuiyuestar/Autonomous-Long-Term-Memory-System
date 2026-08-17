"""Remote embedding-backed vector indexing and retrieval."""

from __future__ import annotations

from collections.abc import Sequence

from altm.contracts import MemoryLayer, MemoryStatus, MemoryUnit
from altm.llm import OpenAICompatibleEmbeddingClient
from altm.storage import SQLiteMemoryStore


def embedding_text(memory: MemoryUnit) -> str:
    parts = [
        memory.summary or "",
        memory.content,
        " ".join(str(value) for value in memory.metadata.values() if isinstance(value, str)),
    ]
    return "\n".join(part for part in parts if part)


class EmbeddingIndexer:
    def __init__(self, store: SQLiteMemoryStore, client: OpenAICompatibleEmbeddingClient) -> None:
        self.store = store
        self.client = client

    def index_missing(self, limit: int = 100) -> Sequence[MemoryUnit]:
        targets = list(self.store.list_embedding_targets(self.client.config.model, limit=limit))
        if not targets:
            return []
        vectors = self.client.embed_texts([embedding_text(memory) for memory in targets])
        if len(vectors) != len(targets):
            raise RuntimeError(
                "Embedding response count mismatch: expected %s, got %s"
                % (len(targets), len(vectors))
            )
        for memory, vector in zip(targets, vectors, strict=True):
            self.store.put_memory_embedding(
                memory_id=memory.id,
                embedding_model=self.client.config.model,
                content_hash=memory.content_hash,
                vector=vector,
            )
        return targets


class RemoteVectorRetriever:
    def __init__(self, store: SQLiteMemoryStore, client: OpenAICompatibleEmbeddingClient) -> None:
        self.store = store
        self.client = client

    def search(
        self,
        query: str,
        limit: int = 10,
        layers: Sequence[MemoryLayer] = (),
        session_id: str | None = None,
        statuses: Sequence[MemoryStatus] = (),
        cross_session_layers: Sequence[MemoryLayer] = (),
    ) -> Sequence[tuple[MemoryUnit, float]]:
        query_vector = self.client.embed_text(query)
        return self.store.search_embeddings(
            query_vector=query_vector,
            embedding_model=self.client.config.model,
            limit=limit,
            layers=layers,
            session_id=session_id,
            statuses=statuses,
            cross_session_layers=cross_session_layers,
        )
