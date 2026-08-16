# Full-Market Selection Baseline (2026-07-29)

> Current trust boundary (2026-08-13): this file is a historical product baseline, not an authorization contract. Current published graphs use `market-scan-snapshot-digest-v2`; a migration `legacy_backfill` can be audited but cannot authorize Discovery, delta/alerts, strategy, probability/future research, degraded retry-copy, or TOP100 refresh. The shared action gate additionally requires the exact full-market scope and internally consistent diagnostics v1 with no blockers, exactly one info `score_distribution.pass`, and no score-distribution conflict. Read-only browse/audit remains visible. Current point-in-time evidence is v4 with quote-event/observed/decision and per-bar `as_of` ordering. Managed research writers and scan retention share a cross-process database lease; database-only restore refuses populated side artifacts. Use README, REQUIREMENTS, DESIGN, and OPERATIONS for the live contract.

This document records the implementation baseline before the full-market selection workspace is expanded. It separates verified behavior, reproducible product gaps, and hypotheses that require measured evidence.

## Verified implementation baseline

- The runtime is a local FastAPI application backed by SQLite. A single runtime leader owns one active full-market task across manual, scheduled, and retry triggers.
- The persisted run contract includes `mode`, `quote_date`, `data_date`, `rule_version`, status/counts, coverage, timestamps, duration, and sanitized diagnostics.
- The persisted result contract includes rank, score values, quote/K-line provenance, structured degradation flags, replay metrics, and `score_details`.
- `official` and `intraday` use different temporal and score fingerprints. Missing and skipped rows do not receive synthetic zero scores.
- Published snapshots are immutable, pageable, filterable, replayable without network access, and exportable from SQLite as XLSX.
- Publication guards cover all-market and SH/SZ/BJ eligible coverage, eligible-universe ratio, quote timestamp parsing and span, stale-date clusters, score distribution, wall-clock budget, and date drift.

## API baseline

Before mode-aware history work, the API exposed global `latest`, global `latest-published`, an unfiltered run list, run detail, paged results, XLSX export, cancel, and retry. Result filters covered status, one market, industry text, ST/new-stock state, minimum quality, keyword, and one sort key.

The discovery API already supported richer persisted criteria: multiple markets and industries, min/max quality, trend, change, turnover, amount and score, plus up to three sort keys. The browser could create only the simpler subset, so imported complex presets were read-only.

## Browser request baseline

- Opening the scan workspace requested the latest task, then the latest published run when necessary, followed by one result page.
- An active run used one non-overlapping status poll every two seconds.
- A visible idle workspace checked the latest task every 30 seconds.
- One filter or page change requested one result page of at most 100 rendered rows.
- One export request returned the complete current filtered snapshot.
- The browser did not expose the existing run-list API as a history selector.

## Performance baseline

The latest documented live full-market run processed 5,532 symbols, ranked 5,487, skipped 45, reached 99.19% coverage, and completed in 517,336 ms (about 8.6 minutes). This is one observed live run, not a percentile. The reliability contract currently evaluates non-retry scan duration over 30 days and uses a p95 target of 90 minutes.

The implementation did not persist per-stage durations, so stock-pool, quote, K-line, scoring, persistence, and publication costs could not be separated from that observation. A repeatable cold/warm benchmark is therefore required before attributing the bottleneck or claiming an optimization.

## Reproducible product gaps

1. Global latest queries could return a run from a different mode than the browser's selected start mode.
2. The mode control selected the next requested mode but did not select a mode-specific published history.
3. The browser had no history-batch selector even though the run-list API existed.
4. Rank comparison selected the prior completed scope before checking the rule fingerprint; interleaved modes could therefore block an otherwise useful same-mode comparison.
5. Persisted score details and replay inputs were not rendered as frozen leaderboard evidence.
6. Opening a stock moved directly to current analysis without first distinguishing it from the historical scan snapshot.
7. Browser-created presets exposed only a subset of the discovery criteria supported by the backend.
8. Progress showed aggregate counts only; it did not show scan stage, ETA, throughput, per-market coverage, or a structured explanation of publication degradation.

## Hypotheses requiring evidence

- Deterministic scores may or may not separate subsequent returns. Replay correctness and healthy score distribution do not prove predictive value.
- The K-line/provider stage is likely the dominant live cost, but this must be demonstrated with stage timings.
- Warm compatible cache and overlap verification may reduce provider payload or processing cost, but the required provider response means cache presence alone cannot be treated as a successful refresh.
- A shorter scan may improve intraday usefulness, but no production invariant may be weakened to achieve it.

## Non-negotiable evaluation rules

Future effectiveness evaluation must start from immutable published snapshots, use only later completed trading-day data for outcomes, report `insufficient_data` below a documented sample floor, and keep evaluation separate from automatic production-weight changes. Performance work must preserve the current universe, temporal, provenance, publication, retry, and replay contracts.
