# Benchmark and Anonymous Trace Evaluation

The current runner and report format are documented below. See the
[full quantitative evaluation framework](quantitative-evaluation-framework.md)
for the proposed public benchmark suite, ALTM-specific metrics, controlled
ablations, safety gates, and implementation phases.

ALTM evaluates retrieval against local copies of:

- [LongMemEval](https://github.com/xiaowu0162/LongMemEval)
- [LoCoMo](https://github.com/snap-research/locomo)
- anonymized real Agent traces using `altm_anonymous_trace_v1`

The repository does not download or redistribute these datasets. Obtain them
through an approved environment and pass their local paths explicitly.

## Public Benchmarks

```bash
altm-benchmark run \
  --format longmemeval \
  --dataset /approved/path/longmemeval_s_cleaned.json \
  --db ./data/benchmark.sqlite3 \
  --output ./reports/longmemeval.json \
  --top-k 5 \
  --top-k 10 \
  --enrichment l0
```

Formats:

- `longmemeval`: one isolated corpus per question, scored against
  `answer_session_ids`.
- `locomo`: one isolated corpus per conversation, scored against QA
  `evidence` turn IDs.
- `anonymous-trace`: one isolated corpus per HMAC-anonymized trace.

Blank LongMemEval turns are ignored while non-blank turns retain their original
source indices. nDCG counts each gold evidence identifier at most once, so
multiple retrieved memories derived from one source cannot inflate ranking
gain.

Enrichment modes:

- `l0`: raw-message retrieval baseline with no model calls.
- `embedding`: L0 plus a real remote embedding index, without L1-L4 or Graph.
- `l2`: real L1 summarization and L2 extraction before retrieval.
- `full`: L1/L2, embedding index, Graph LLM, semantic L3, and semantic L4.

`embedding`, `l2`, and `full` fail when required model configuration is absent.
They never replace semantic stages with mock output.

Reports contain Recall-any@K, Recall-all@K, nDCG@K, MRR, p50/p95/p99 latency,
per-category aggregates, dataset SHA-256, Python/platform information, and
per-question evidence IDs.

## Anonymous Traces

Raw JSONL records use `kind=message` or `kind=query`:

```json
{"kind":"message","trace_id":"project","session_id":"s1","turn_id":"t1","timestamp":"2026-08-01T00:00:00Z","role":"user","content":"We selected SQLite."}
{"kind":"query","trace_id":"project","session_id":"s1","query_id":"q1","query":"Which database did we select?","relevant_turn_ids":["t1"],"category":"decision_recall"}
```

Anonymize before evaluation:

```bash
export ALTM_TRACE_ANONYMIZATION_SALT="<random secret of at least 16 characters>"
altm-benchmark anonymize-trace \
  --input /approved/path/raw-trace.jsonl \
  --output ./data/anonymous-trace.jsonl
```

The importer:

1. HMAC-SHA256 hashes trace, session, turn, query, and relevance IDs.
2. Redacts common bearer/API tokens, email addresses, phone numbers, IPs, and
   user home paths.
3. Writes through a temporary file and atomically replaces the destination.
4. Does not persist the salt.

Only records marked `anonymized=true` are accepted by the evaluator. The
built-in redactor is a baseline, not a legal guarantee; organizations should
provide already-sanitized traces or an approved redaction pipeline before
moving data across trust boundaries.
