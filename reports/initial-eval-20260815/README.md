# Initial Quantitative Evaluation

Date: 2026-08-15

This report establishes the first reproducible L0 evidence-retrieval baseline.
It does not include answer generation, remote embeddings, or embedding-dependent
L3/L4 progression.

## Quality Gates

| Gate | Result |
|---|---:|
| Domain tests | 162 / 162 |
| Harness lifecycle tests | 3 / 3 |
| Runtime health checks | 8 / 8 |
| Cross-repository integration scenarios | 6 / 6 |
| Real Graph emergence controls | 6 / 6 |
| Ruff | Passed |
| Pyright | 0 errors, 0 warnings |

## Public Retrieval Baselines

| Dataset | Questions | Scored | Recall-any@1 | Recall-any@5 | Recall-any@10 | Recall-all@10 | nDCG@10 | MRR | p95 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| LongMemEval cleaned, first 100 | 100 | 100 | 47.00% | 73.00% | 80.00% | 62.00% | 58.21% | 58.48% | 468.39 ms |
| LoCoMo full | 1,986 | 1,982 | 14.68% | 27.75% | 37.13% | 31.69% | 23.01% | 20.68% | 78.95 ms |

LongMemEval uses the first 100 records rather than a stratified sample: 70 are
single-session user questions and 30 are multi-session questions. LoCoMo has
four questions without gold evidence identifiers; the retrieval report marks
them as skipped.

## Validation Notes

- The official cleaned LongMemEval data contains blank source turns. The loader
  now ignores them while retaining the source indices of non-blank turns.
- nDCG now counts each gold evidence identifier once. Repeated memories derived
  from one source cannot push nDCG above 1.
- The real Graph check used a lexically isolated neighbor. It appeared only
  after graph expansion and disappeared when all support paths were rejected,
  when the Agent scope changed, and when the neighbor was tombstoned.
- Raw per-question reports include dataset hashes, environment details, evidence
  identifiers, ranked memory identifiers, metrics, and latency.
