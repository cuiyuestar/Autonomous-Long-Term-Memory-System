from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from altm.contracts import (  # noqa: E402
    EmbeddingConfig,
    LifecycleState,
    MemoryLayer,
    MemoryStatus,
    MemoryUnit,
    RecallQuery,
)
from altm.llm import (  # noqa: E402
    OpenAICompatibleEmbeddingClient,
    optional_embedding_client_from_env,
)
from altm.retrieval import FTSRetrievalEngine  # noqa: E402
from altm.retrieval.remote_vector import EmbeddingIndexer  # noqa: E402
from altm.storage import SQLiteMemoryStore  # noqa: E402
from altm.utils import sha256_text, utc_now_iso  # noqa: E402


class FakeEmbeddingHandler(BaseHTTPRequestHandler):
    last_path: str | None = None
    last_authorization: str | None = None

    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        inputs = payload["input"]
        if isinstance(inputs, str):
            texts = [inputs]
        else:
            texts = list(inputs)

        type(self).last_path = self.path
        type(self).last_authorization = self.headers.get("Authorization")

        data = [
            {
                "object": "embedding",
                "index": index,
                "embedding": self._embedding_for(text),
            }
            for index, text in enumerate(texts)
        ]
        response = {
            "object": "list",
            "model": payload["model"],
            "data": list(reversed(data)),
        }
        encoded = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _embedding_for(self, text: str) -> list[float]:
        if "semantic-target" in text or "semantic query" in text:
            return [1.0, 0.0, 0.0]
        if "other-target" in text:
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]


class FailingEmbeddingClient:
    class Config:
        model = "failing-embedding"

    config = Config()

    def embed_text(self, text: str) -> list[float]:
        raise RuntimeError("forced embedding failure")


class EmbeddingIntegrationTest(unittest.TestCase):
    def test_openai_compatible_embedding_client_calls_embeddings_endpoint(self) -> None:
        server = HTTPServer(("127.0.0.1", 0), FakeEmbeddingHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = OpenAICompatibleEmbeddingClient(
                EmbeddingConfig(
                    base_url="http://127.0.0.1:%s/compatible-mode/v1" % server.server_port,
                    api_key="test-key",
                    model="test-embedding",
                    timeout_seconds=10,
                )
            )
            vectors = client.embed_texts(["other-target memory", "semantic-target memory"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(
            FakeEmbeddingHandler.last_path,
            "/compatible-mode/v1/embeddings",
        )
        self.assertEqual(FakeEmbeddingHandler.last_authorization, "Bearer test-key")
        self.assertEqual(vectors, [[0.0, 1.0, 0.0], [1.0, 0.0, 0.0]])

    def test_indexer_caches_vectors_and_remote_recall_uses_remote_channel(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            store.initialize()
            store.put_memory_unit(_memory("semantic", "semantic-target memory"))
            store.put_memory_unit(_memory("other", "other-target memory"))

            server = HTTPServer(("127.0.0.1", 0), FakeEmbeddingHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = OpenAICompatibleEmbeddingClient(
                    EmbeddingConfig(
                        base_url="http://127.0.0.1:%s/v1" % server.server_port,
                        api_key="test-key",
                        model="test-embedding",
                        timeout_seconds=10,
                    )
                )
                indexed = EmbeddingIndexer(store, client).index_missing(limit=10)
                recalled = FTSRetrievalEngine(store, client).recall(
                    RecallQuery(text="semantic query", top_k=5)
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual({memory.id for memory in indexed}, {"semantic", "other"})
            cached = store.get_memory_embedding("semantic", "test-embedding")
            self.assertIsNotNone(cached)
            self.assertEqual(cached[1], [1.0, 0.0, 0.0])
            self.assertGreaterEqual(len(recalled), 1)
            self.assertEqual(recalled[0].memory.id, "semantic")
            self.assertIn("remote_vector", recalled[0].matched_by)

    def test_remote_embedding_failure_falls_back_to_local_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            store.initialize()
            store.put_memory_unit(_memory("fallback", "fallback target memory"))

            recalled = FTSRetrievalEngine(  # type: ignore[arg-type]
                store,
                FailingEmbeddingClient(),
            ).recall(RecallQuery(text="fallback", top_k=5))

            self.assertEqual(len(recalled), 1)
            self.assertEqual(recalled[0].memory.id, "fallback")
            self.assertNotIn("remote_vector", recalled[0].matched_by)
            self.assertIn("remote_vector_degraded", recalled[0].matched_by)
            self.assertIn("Degraded channels: remote_vector", recalled[0].explanation or "")

    def test_optional_embedding_client_from_env_returns_none_without_full_config(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(optional_embedding_client_from_env())


def _memory(memory_id: str, content: str) -> MemoryUnit:
    now = utc_now_iso()
    return MemoryUnit(
        id=memory_id,
        layer=MemoryLayer.L2,
        lifecycle_state=LifecycleState.SHORT,
        status=MemoryStatus.ACTIVE,
        content=content,
        content_hash=sha256_text(content),
        summary=content,
        created_at=now,
        updated_at=now,
    )


if __name__ == "__main__":
    unittest.main()
