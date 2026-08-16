# Embedding Quantitative Evaluation

Date: 2026-08-16

This evaluation isolates the effect of remote semantic vectors by comparing:

- L0: raw memories with local lexical, FTS, and fallback retrieval.
- L0 + Embedding: the same raw memories plus `text-embedding-v4`.

Both variants use the same datasets, questions, evidence labels, Top-K values,
and metric definitions. The embedding variant does not include L1-L4, Graph,
Active Window, CCR, or answer generation.

## Provider Validation

| Item | Result |
|---|---:|
| Model | `text-embedding-v4` |
| Dimension | 1,024 |
| Batch size | 10 |
| Retry policy | 3 retries with exponential backoff |
| Production memory index | 49 / 49 |
| LongMemEval vector coverage | 49,776 / 49,776 |
| LoCoMo vector coverage | 5,882 / 5,882 |

## Benchmark Results

| Dataset | Variant | Recall-any@1 | Recall-any@5 | Recall-any@10 | Recall-all@10 | nDCG@10 | MRR | p95 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| LongMemEval, first 100 | L0 | 47.00% | 73.00% | 80.00% | 62.00% | 58.21% | 58.48% | 468.39 ms |
| LongMemEval, first 100 | L0 + Embedding | 74.00% | 99.00% | 100.00% | 82.00% | 84.54% | 85.82% | 1,032.84 ms |
| LoCoMo full | L0 | 14.68% | 27.75% | 37.13% | 31.69% | 23.01% | 20.68% | 78.95 ms |
| LoCoMo full | L0 + Embedding | 27.40% | 56.66% | 66.40% | 55.05% | 42.96% | 39.53% | 1,248.58 ms |

## Absolute Gains

| Dataset | Recall-any@1 | Recall-any@5 | Recall-any@10 | Recall-all@10 | nDCG@10 | MRR | p95 multiplier |
|---|---:|---:|---:|---:|---:|---:|---:|
| LongMemEval | +27.00 pp | +26.00 pp | +20.00 pp | +20.00 pp | +26.32 pp | +27.34 pp | 2.21x |
| LoCoMo | +12.71 pp | +28.91 pp | +29.26 pp | +23.36 pp | +19.95 pp | +18.85 pp | 15.82x |

Semantic vectors materially improve every retrieval-quality metric. The largest
LoCoMo gain is Recall-any@10, which rises by 29.26 percentage points. The
largest cost is online latency because each query calls the remote embedding
provider. Query-vector caching, local serving, and latency-aware routing should
be evaluated before production scale.

These numbers measure evidence retrieval rather than final answer accuracy.
They do not establish embedding-dependent L3 scene quality or L4 persona
quality; those require separate formation labels and controlled ablations.
