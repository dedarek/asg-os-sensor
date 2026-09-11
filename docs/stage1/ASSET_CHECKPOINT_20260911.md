# Real asset checkpoint, 2026-09-11

Baseline: ef3b3e8, isolated advisor branch. Night branch was not merged.

- Real Goose used CommandCode `deepseek/deepseek-v4.1-flash` with normal TLS verification.
- Credential lives only in an ignored 0600 file; the run settings reference its path, never its value.
- Target: existing isolated real CLI PID 77504 / create_time 1789053293.235658. This is a known development sample, not an independent blind-test score.
- Source: `artifacts/stage1/real-investigation-20260911/pid_77504_1789092649847`.
- Goose persisted model/gateway configuration and an observed session permission policy. MCP/skills were reported empty within inspected sources, not globally absent.
- Assets are accepted from target-bound findings/evidence independently of a Hook recipe. Process exit 0 alone is insufficient. Failed processes and damaged/cross-instance evidence do not become successful checkpoints. Missing identity may retain partial assets.
- The role inference in this first pass relies too heavily on serving/launch metadata. It is a model conclusion with uncertainty, not an independent proof of autonomous orchestration; richer role evidence remains follow-up work.
- Existing learned Hook verification remains yesterday's supervisor-guided event snapshot. No new Hook was installed in this pass; continuous health/blocking is not claimed.

## Reproduction / deployment

`artifacts/stage1/real-investigation-20260911/settings.json` and `run_once.py` record the real run configuration. `recipes/runtime_assets.yaml` is a product-independent asset-first prompt, with phase budget and checkpoint/stop rules. The full Hook investigation recipe is unchanged.

`artifacts/stage1/dashboard-assets-20260911/settings.json` and `start.py` are the deployment settings/entry point; service stdout/stderr go to `service.log`, not a transient tool pipe. `deployment.json` records the current PID/create_time and port. The service retains single-target automatic investigation scope; other instances remain manually investigable.

Original source evidence is preserved. A copy of the old failed outcome is retained as `result.before_asset_checkpoint.json`: the newly implemented checkpoint validator reprocessed the actual completed run, rather than inventing or editing a model finding.

## Checks

- 33 focused asset/status/lifecycle tests passed; the 13 asset tests also pass on system Python 3.9 after postponed annotations were added.
- Real standalone scan: five candidates. Reproducing a closed stdout pipe before the fix yielded five diagnostic attempts but zero returned candidates; normal stdout yielded five. Diagnostic write failures are now caught locally, outside candidate acceptance semantics.
- Preview API/browser showed actual model/gateway and rules, explicit MCP/skill inspection scope, unchanged prior Hook evidence, and no active Goose job.

This delivers one real asset-first checkpoint to the dashboard, not general unknown-Agent automatic installation or a completed EDR product.
