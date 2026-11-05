# Langfuse telemetry for MXC eval runs

The [Windows C0 comparison](../analyses/c0-claudex-001-win-20261026/README.md)
records paid-run identity audits and ten verified published scores per session.
The earlier model-free identity fixtures remain [separate evidence](../analyses/telemetry-identity-20261025/README.md).
The macOS runs ([C2, 2026-11-02](../analyses/c2-macos-mxc-20261102/README.md)) hold 21 scores per session: 11 automatic `geeky.*` scores (runner, `GEEKY_EVAL_SCORES=1`) and 10 parity scores (`langfuse-scores.py`). Per-run numbers are in that analysis.

Both runners are configured to capture every eval session for Langfuse. The Claude Code runner is `run-baseline.ps1` and the Codex runner is `run-codex-baseline.ps1`. Capture includes turns, model generations, subagents or child threads, tool inputs/outputs and available reasoning summaries without configured truncation. Delivery and required coverage need separate evidence. Use the traces to inspect, evaluate and score runs alongside `score-run.py`.

Each harness uses Langfuse's own transcript hook, pinned in `pins.json` and verified by SHA-256:

| Harness | Hook | Runs | Full-content settings |
|---|---|---|---|
| Claude Code | [`langfuse/claude-observability-plugin`](https://github.com/langfuse/claude-observability-plugin) `hooks/langfuse_hook.py` (Python, `langfuse` SDK) | `Stop` and `SessionEnd`, from the run's `--settings` | `CC_LANGFUSE_MAX_CHARS=2147483647`, skill content captured, `showThinkingSummaries: true` |
| Codex | [`@langfuse/codex-observability-plugin`](https://github.com/langfuse/codex-observability-plugin) `dist/index.mjs` (Node 22+, self-contained bundle) | `Stop`, registered through the profile's `-c hooks=...` overrides and trusted by its exact command. That command is `agent-py python <run home>/langfuse-tracer.py`, which runs node with the bundle, so no shell has to quote a path like `C:\Program Files\nodejs\node.exe` | Never truncates; `model_reasoning_summary = "detailed"` |

On macOS, Claude Code runs have no pinned hook. Claude Code exports native OTel spans, and the runner uploads the transcript after the run with `evals/telemetry/transcript-otlp.mts`. The native spans carry structure, timing and token counts as metadata but no prompt or response content, because Langfuse drops OTel log events. The content comes from the post-run upload.

Each hook reads the transcript the harness already writes and uploads it over OTLP. That is the Claude session JSONL and `subagents/` folder, or the Codex rollout and its child-thread rollouts. The hooks fail open, so an upload problem never stops a run.

The MXC adapters preserve those pinned upstream files. A Claude bootstrap and a
derived Codex converter add the [agent identity contract](#agent-and-run-identities).
The SDK remains pinned at 4.18.0 because the upstream Claude hook uses SDK 4.x
backdated-span internals; a major-version migration requires separate validation.

Thinking: in `claude -p` runs, Claude Code asks for `display: "omitted"` thinking by default, which leaves only signatures in the transcript. All 124 thinking blocks in C1 run 6 were empty. `showThinkingSummaries` makes it request API-side summaries instead. In `-p` mode the macOS runs found that setting has no effect; pass `--thinking-display summarized` (commit ea144eb), which returned text in 181 thinking blocks for claude-sonnet-5-5 on C2. gpt-6.1-sol through LiteLLM returned no thinking text (0 of 202 blocks), so its effort cannot be checked from traces. Codex runs used `summary: none`, so their reasoning was encrypted only. Summaries are the only plaintext reasoning either vendor returns; raw reasoning stays provider-encrypted.

## Setup (once per host)

```powershell
pwsh -File geeky-orchestration/evals/telemetry/install-telemetry-tools.ps1   # needs uv; Node 22+ for Codex
```

This installs, under `<ToolsDir>`, which defaults to `C:\repos\.eval-tools` on Windows and `~/repos/.eval-tools` elsewhere:
- `langfuse/claude-hook/<commit>/langfuse_hook.py`
- `langfuse/codex-tracing/<version>/dist/index.mjs`
- `langfuse-py`, a venv with the pinned `langfuse` SDK

## Environment

Telemetry is on when the host has `TRACE_TO_LANGFUSE=true`. Pass `-NoTelemetry` to a runner to turn it off.

| Variable | Used by |
|---|---|
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL` | Claude; Codex fallback |
| `LANGFUSE_CODEX_PUBLIC_KEY`, `LANGFUSE_CODEX_SECRET_KEY`, `LANGFUSE_CODEX_BASE_URL` | Codex (take precedence) |
| `LANGFUSE_TRACING_ENVIRONMENT`, `LANGFUSE_CODEX_ENVIRONMENT` | runners explicitly select `mxc-win`, `mxc-linux` or `mxc-mac` from the actual host. macOS Claude runs use `mxc-claude-mac`: `LANGFUSE_TRACING_ENVIRONMENT` for the transcript upload and automatic scores, and the resource attribute `deployment.environment.name` for native OTel spans |
| `CC_LANGFUSE_TRACE_TAGS` / `LANGFUSE_CODEX_TAGS` | appended to the run tags |
| `LANGFUSE_USER_ID` / `LANGFUSE_CODEX_USER_ID` | runners set a safe family fallback; adapters assign each observed agent its own user ID |

Keys reach the sandbox only as bare `--env NAME`. The MXC host copies the value and never prints it. Codex always gets an explicit `LANGFUSE_CODEX_USER_ID`, so its hook never reads `CODEX_HOME/auth.json`. When the host trusts a private CA through `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE` or `NODE_EXTRA_CA_CERTS`, those variables pass through with a read-only grant on the bundle's folder.

## Finding a run in Langfuse

Every trace carries `geeky-eval`, `identity:agent-v1`, harness, platform,
environment, agent-family, case, run and plugin tags, plus `mock` for mocks.
Agent-role and agent-type tags distinguish each actor. The human-readable
Langfuse session ID is `run.json.telemetry.session_id`. The original
`run.json.session_id` remains the native Claude session/Codex thread ID.
For historical runs without a telemetry session override, use the native ID.

## Agent and run identities

| Field | Windows example | Linux / macOS equivalent |
|---|---|---|
| Environment | `mxc-win` | `mxc-linux`, `mxc-mac` |
| Agent family | `codex-mxc-win`, `claude-mxc-win` | replace `win` with `linux` or `mac` |
| Run session | `codex-mxc-win-c0-wordstat-<run-stamp>` | same platform naming |
| Orchestrator trace | `codex-mxc-win-orchestrator` | same platform naming |
| Coder trace | `claude-mxc-win-coder-<agent-name>` | same platform naming |
| User ID | `<family>-<role>-<agent-name>-<native-agent-id>` | same platform naming |

When the agent name equals its role, it appears once. User IDs are ASCII,
bounded below Langfuse's 200-character limit and retain collision-resistant
suffixes when truncation is necessary. They identify the agent/subagent rather
than the human operator. Native roles come from structured metadata; unknown
types stay `unknown`, with the original type and name retained.

Each agent owns its trace. All actors from one run share its session. This
avoids Langfuse's trace-level user propagation overwriting the orchestrator's
identity when a coder or reviewer exports. Nested tool calls and generations
remain under their own actor's `AGENT` observation. Cross-agent dispatch edges
are stored as `parent_agent_id`, `parent_trace_id` and
`parent_observation_id` metadata, rather than physical cross-trace nesting.

Filter by environment or `agent-family`/`agent-role` tags, then group by user ID,
run session or metadata. Flat metadata includes `identity_schema`, `harness`,
`platform`, `environment`, `case`, `run`, `run_group_id`, `plugin_sha`, configured
model/reasoning and provider, worker/reviewer defaults, `mock`, `agent_id`,
`agent_role`, `agent_type`, `agent_name`, `agent_user_id`, `actor_scope`, native
session ID, available parent IDs and Codex agent path. Generation observations
continue to carry the actual model, usage and reasoning data from the transcript.

Codex recovers unannounced children from structured parent metadata, including
nested `source.subagent.thread_spawn` records. Explicit trace seeds are retained
for independent roots; an external parent becomes a logical metadata link when
an agent needs independent trace ownership. The raw transcripts and pinned
upstream files are unchanged. Existing ingested sessions are not renamed.

[Validation and live examples](../analyses/telemetry-identity-20261025/README.md)
include Windows MXC mocks and separate three-agent fixtures, with zero model
inference. Linux naming is covered by tests, not live execution here. macOS has run live: C0 on 2026-10-30 and C2 on 2026-11-02.

After each run the runner reads the session back with `langfuse-run.py` and writes `langfuse.json` (counts by type, usage totals, truncated observations). It records `telemetry.delivered` and `telemetry.observations` in `run.json`. Claude runs also keep the hook log as `langfuse-hook.log`. To pull a session in full, including all inputs and outputs:

```powershell
python geeky-orchestration/evals/telemetry/langfuse-run.py --session-id <id> --output summary.json --full observations.json
```

`langfuse-run.py` reads `/api/public/v2/observations`, the only read API on Langfuse v4 events-only deployments. The `langfuse` CLI (API 4.35.0) likewise refuses the deprecated sessions and traces endpoints. Use `langfuse api observations list --session-id <id> --fields core,basic,io,model,usage,metadata`; the default fields omit io and usage. Use `--trace-id` to see uploaded child observations, since only the root Conversational Turn carries the session id.

`delivered` is retained as a compatibility alias for `observations_present`
(observations > 0). It proves presence only. `observations_status` is `present`
or `pending`; `coverage_status` is `met`, `missing` or `unknown` for the required
harness points. Neither establishes that every captured observation arrived.
`scores_verified` stays false in runner telemetry: the separate publisher's
readback report proves score delivery. A hook exit of zero or two equal poll
counts cannot prove complete ingestion. A failed read resets presence/coverage
claims even if an earlier local summary remains on disk.

Recover from healthy retained captures using the existing pinned flush/replay,
with the original identity, environment and hook state. Do not rerun a contestant
to repair telemetry. From the plugin root, the existing operator commands are:

```powershell
# Codex: existing Stop payload points at the retained rollout; preserve its .langfuse state.
python evals/telemetry/codex-flush.py <node> <pinned-tracer.mjs> <payload.json>
# Claude: in a host session with the original resolved telemetry object and pinned hook.
. ./evals/telemetry/telemetry.ps1
Invoke-ClaudeTelemetryFlush -Telemetry $telemetry -SessionId <native-id> -TranscriptPath <session.jsonl>
# Re-read presence and required coverage independently, after egress recovers.
python evals/telemetry/langfuse-run.py --session-id <id> --output summary.json --full observations.json
python evals/telemetry/langfuse-coverage.py --session-id <id> --output coverage.json
```

DNS/export errors and partial delivery remain pending. Keep the local evidence
and use these commands after the outage; there is no inference, automatic paid
recovery or new outbox in this procedure.

## Harness coverage

`langfuse-coverage.py` checks which harness points a session reached, using its Langfuse observations: all five skills, subagents, the coder and reviewer roles, planning gates, pytest, commits, file edits, reasoning summaries and token usage. It also reports nested subagents (the agent may run `geeky-implement` itself or delegate it to `geeky-implement-orchestrator`), MCP gate tools and encrypted Codex handoffs as information only. The runners call it after each run and write `langfuse-coverage.json`, plus `telemetry.coverage_complete` and `telemetry.coverage_missed` in `run.json`. Run it on demand too:

```powershell
python geeky-orchestration/evals/telemetry/langfuse-coverage.py --session-id <id>
```

It was calibrated on two real sessions, Claude C1 run 6 and Codex C1 run 3 (replayed through the pinned tracers under `environment=smoke`). Both hit every point except reasoning summaries, which those runs predate. Native-OTel Claude runs on macOS first reported `reasoning_summaries` as missed on every run, because `has_reasoning` did not read Anthropic thinking content blocks; commit a8a1fb4 fixed that. `cases/c0-wordstat` is the minimal case built to hit every point; see its `spec.md`.

## Known limits

- **Codex inter-agent payloads are encrypted by OpenAI's own backend.** Through a LiteLLM provider they stay plaintext (0 encrypted handoffs in both LiteLLM C0 runs). With the OpenAI backend, `spawn_agent`, `send_message` and `followup_task` arguments, and the `agent_message` a child thread receives, carry `encrypted_content` (`encrypted_function_args`), so no plaintext exists on disk or in Langfuse. Each thread's own messages, tool calls and outputs are plaintext. Coverage counts these handoffs as `encrypted_handoffs`.
- **A stage counts when its SKILL.md is read.** Codex reads skills by opening the file, and an agent may read every skill up front: Codex C0 attempt 5 hit `stage:impl-review` without reaching it. Check the commits and the review files when a stage matters.
- **Raw reasoning stays encrypted**; only the summaries are plaintext.
- **Export timeout.** Both SDKs cap one OTLP export at `LANGFUSE_TIMEOUT` seconds, 5 by default. A full session needs longer, so the runners pass `LANGFUSE_TIMEOUT=300`. Without it, the C1 Codex run 3 replay delivered nothing while the hook still exited 0.
- **Transcript upload truncation.** The macOS transcript upload caps every string at `CC_LANGFUSE_MAX_CHARS` (default 20000). The C2 launcher sets 1000000, because a 60,559-char field would otherwise be cut.
- **Child observations carry no session id.** Only the root Conversational Turn has it. Query uploaded child observations by trace id.
- **Proxies.** The Codex tracer's exporter uses Node's `http` module, which ignores `HTTPS_PROXY`. It needs direct egress to Langfuse, which the MXC sandbox provides.

## Scoring in Langfuse

After scoring with `scripts/parity-eval.py`, new publication requires an explicit
`geeky-eval.inputs/1` sidecar. Historical sidecars and score IDs stay unchanged;
create a separate sidecar for a newly authorized scope. Example commands (paths
relative to plugin root):

```powershell
python evals/scripts/parity-eval.py <run> --observations observations.json --score score.json --manual manual.json --scope evaluation-inputs.json --out derived
python evals/telemetry/langfuse-scores.py --run <run> --parity derived/parity.json --scope evaluation-inputs.json --dry-run
# Only after separate publication authorization:
python evals/telemetry/langfuse-scores.py --run <run> --parity derived/parity.json --scope evaluation-inputs.json --output score-readback.json
```

For native-OTel runs, pass a publish-run copy of `run.json` whose `telemetry` block names the environment (for example `mxc-claude-mac`). Without it `langfuse-scores.py` falls back to environment `mxc` and the scores land there.

The sidecar declares `schema`, `scope_id`, `kind` (`initial`, `continuation` or
`cumulative`), `parent_scope_id` (null only for initial), `configuration`
(`prompt_sha256`, `plugin_sha`; null only when unknown), and `sources` with
unique `id`, `path`, `sha256` and `ancestry` source IDs. Source paths resolve
relative to the sidecar. Include every run/observation/score/manual input;
include any coverage override used for publication. Each `segments` entry has
`segment_id`, `authorization_reference`, timezone-aware `start` and `end`.
For new `geeky-eval.execution/1` records, also include `attempt_id`; attempt,
segment and authorization reference must match the bound run record. The
runner's execution metadata supplies these declarations; local IDs alone do
not prove continuation authorization.

`ownership` declares `method` (`native` for an initial input, `source-reviewed`
for resumed inputs), a `rule`, optional `cutoff`, and hashed `evidence_source`
for source review. Resumed inputs also require `assignments`, each containing
`source_id` and explicit retained `logical_ids`; duplicates are rejected.
Cutoff timestamps alone never establish novel ownership. Retain source ancestry
when using the existing C0 reviewed logical derivation. This validator does not
merge arbitrary forks or certify the contents of a human review.

Use `metric_default` plus optional `metrics` overrides for `runtime`, `tokens`,
`calls`, `latency_idle`, `commits`, `coverage`, `backend` and `cost`. Each resolved
label requires `scope_id`, `population`, `source_ids`, `additive` and `complete`.
Default `additive` to false. Medians/idle spans, coverage, backend and cost cannot
be added. A mismatched population or incomplete source nulls the associated
efficiency value: render null as N/A. Thus cumulative time/commits can be shown
alongside explicitly labeled continuation usage, but cumulative tokens, backend
or cost remain N/A when unknown. Missing sources make completeness unknown and
block dependent evaluation/publication. Hash changes fail closed. Historical
parity evaluation without a sidecar remains available for retained comparisons;
publication always requires explicit scope. A retained parity file without an
embedded scope must itself be a hashed sidecar source.

Score IDs hash a structured tuple of session, scope ID, evaluator version and
score name. Same-scope reposts are idempotent; other scopes/versions have distinct
IDs. Since `parity-eval/1.3`, `parity.json` records the evaluator that made it
(`evaluator`). `--evaluator-version` defaults to that value and a different one
is refused; a `parity.json` without the field (an older evaluator) defaults to
`parity-eval/1` and cannot be published as `parity-eval/1.3` or later. Metadata includes scope, evaluator, sidecar/parity SHA-256 and prompt/plugin
fingerprints. An existing scoped ID with different bindings/values blocks writes;
use a distinct authorized scope/version instead of overwriting its evidence.
No legacy session/name score IDs are written. The publisher reads
expected IDs through `/api/public/v3/scores?fields=details,subject` and compares
session, name, data type, typed numeric/boolean value and all four binding fields
(`scope_id`, `evaluator`, `input_sha256`, `parity_sha256`). Only exact readback
sets `scores_verified=true`; missing/mismatched IDs, partial POSTs, DNS failures
or unsupported v3 return pending and exit 3. An unsupported instance requires
operator action; there is no fallback that declares delivery. `--dry-run`
previews IDs without connecting. Evaluator definitions are in
[langfuse-evaluators/](langfuse-evaluators/README.md). The full procedure is in
[../mxc-litellm-runbook.md](../mxc-litellm-runbook.md).

## Mock runs

The mocks exercise the real hooks with no model calls. `mock_claude.py` runs the `--settings` `Stop` hooks on its transcript and fails on any hook error. `mock_codex.py` runs the configured `Stop` hook on a turn-shaped rollout and requires the tracer's `.langfuse` delivery sidecar to record the turn. `run-mock-matrix.ps1` runs both harnesses on every case and fails a run whose session never reached Langfuse.

## Parity note

Telemetry changes the contestant setup: one extra `Stop` (and `SessionEnd`) hook, plus summarized thinking and reasoning. On macOS Claude runs there is no extra hook; the change is the native OTel environment flags and `--thinking-display summarized`. A run with telemetry is a different configuration from the earlier stored baselines (D101).
