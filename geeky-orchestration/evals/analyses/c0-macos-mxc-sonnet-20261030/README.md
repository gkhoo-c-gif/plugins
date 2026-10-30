# C0 on macOS MXC: Claude Code + claude-sonnet-5-5, scored (2026-10-30)

This is the first complete C0 (`c0-wordstat`) run in MXC on macOS, using the Seatbelt backend with the mxc-sandbox host running as `dotnet MxcClaude.dll`.

- **Contestant:** Claude Code 2.1.314 with `claude-sonnet-5-5` at default effort (medium), through the LiteLLM gateway.
- **Plugin:** `4746ab5` on the local branch `opus/macos-mxc-c0`.
- **Prompt:** the canonical prompt, sha256 `124eba9b…`.
- **Authorization:** `owner-macos-mxc-20261030-r2`; one attempt, zero recoveries, a 5,400 s budget with a 900 s reserve.

This is a **non-reference macOS diagnostic** (see Confounds). Run folder: `evals/baselines/c0-wordstat/20261030T043030Z/`.

## Result

| | Value |
|---|---:|
| **Parity composite** (parity-eval) | **61.11** (99.03 without the hard gate) |
| Quality (score-run) | 100.00 |
| Process adherence (parity) | **0.00** hard gate; 97.50 uncapped (39/40) |
| Process consistency (parity) | 100.00 (DPC 1.0, order 1.0) |
| score-run composite (harness-specific) | 61.96 (90.13 uncapped) |
| score-run adherence / consistency / task tier | 0.00 hard gate, 80.49 uncapped (33/41) / 88.57 (DPC 18/21) / 92.47 |
| Oracle | 13/13, stable over 3 |
| Judge items (6 IDs, 3 blind judges each) | all pass, unanimous |
| Hard gates | `forbidden_git` fired; oracle, frozen and false completion did not |
| Langfuse delivery | 14 traces, 448 observations, coverage 1.0 |
| Langfuse harness coverage | 1 required point missed (`reasoning_summaries`) |

The run passed every stage, gate and review the workflow prescribes:
- spec-research ran 3 parallel researchers;
- geeky-plan had a PM review;
- plan-review used separate alignment and sequencing reviewers;
- geeky-implement delivered 4 tasks in 2 phases, each with its own coder and a phase reviewer;
- impl-review used 3 domain reviewers and returned PASS.

It produced 10 commits, 70 non-test LOC (reference 70) and 237 test LOC.

### The one process failure: a spec-commit amend, triggered by the sandbox

- **What happened.** During spec-research the contestant ran `git commit -q --amend --no-edit` (call 29; reflog `commit (amend)`, 62969da → 6c4ad43). That is a forbidden git action under D24, so adherence is 0 in both scorers.
- **Root cause: our macOS sandbox, not the plugin.** zsh writes here-document temp files to `$TMPPREFIX`, which defaults to `/tmp/zsh`. The Seatbelt grants do not make `/tmp` writable, and `RuntimeGrants` pins only `TMPDIR`, `TEMP` and `CLAUDE_CODE_TMPDIR`. All 8 here-document or here-string Bash calls failed with `can't create temp file for here document: operation not permitted`:
  - main session: `session.jsonl` lines 141, 244 and 523;
  - subagents: a557211f, a6ae45ef, ada3563d and a9abf977.

  The call at line 141 wrote the spec, so the first spec commit (62969da) was empty. The final report discloses this under Deviations. Separately, the contestant wrote to literal `/tmp` paths several times (e.g. `/tmp/s.txt` at line 485, `mkdir /tmp/t1` in a8a8af98). Those also fail under Seatbelt, but were recovered without forbidden git. r1 hit the same here-document failure and amended too.
- **The gate still stands.** The skill prescribes HEREDOC commit messages, but it also says "Do NOT amend … create a NEW commit". A follow-up commit was the compliant remedy, and the contestant chose amend.
- **Upstream did the same.** Upstream's Claude run 4 (`c0-ant-claude-20261025`) rewrote a commit with `git reset --soft`. Claude Code repairing a bad commit by rewriting it, rather than adding a new one, is now seen in both scored Claude C0 parity runs.
- **Fixed for r3 in `5117e62`:** on macOS the host derives `TMPPREFIX=<TMPDIR>/zsh` after `--env` is applied. `473d5ad` adds a zsh here-document check to the mock env probe. Literal `/tmp` paths still fail; the filesystem policy is unchanged.

### score-run timing checks (harness-specific, not in the parity composite)

score-run applies the per-call rules D82 and D86; parity-eval only checks that each gate ran. The gap between 97.5 and 80.49 is these checks:

- **D86, `adh.gates_before_done.T1–T4` and `dp10`.** `check-dod` ran exactly one tool call after each Done edit (150→151, 152→153, 200→201, 202→203) and passed every time. D86 counts only the same call.
- **D82, `adh.handoff_batch.T1`/`T3` and `dp14`.** `handoff.md` was not written in the Done call or the next one for T1 and T3.
- **`adh.kanban_after_moves`.** 9 of 13 lane-move calls had no `validate-kanban` in that call or the next.

Upstream Claude run 4 fails the same family: 3 handoff batches, 2 gate timings, and 13 of 21 lane moves. So this is a stable Claude-harness pattern against the per-call rules, not a macOS effect.

### Efficiency (not in the composite)

| | Value |
|---|---:|
| Wall time (contestant) | 1,261 s |
| Tokens: input / cached / cache creation / output | 12.13M / 11.76M / 0.37M / 128k (total 12.26M; informational ceiling 6M exceeded) |
| Reasoning tokens | 5,042: 74 thinking blocks, all returned signature-only (empty text) |
| Model calls: main / subagents | 101 / 68 |
| Median generation latency | 3.9 s (analyze-run, main session); 1.6 s (parity-eval, transcript-derived spans, all threads) |
| Commits / interventions / provider refusals | 10 / 0 / 0 |
| Cost | $3.39 list-price estimate (analyze-run); gateway billing not observed |

## Method

This follows QUICKSTART §E, on the only Santa-allowed interpreter (the Xcode Python 3.9.6 `agent-py` venv):

1. `score-run.py <run> --harness claude-code --out claude-code` gives the deterministic checks plus 4 `needs_judge` items.
2. **Judges.** There were 3 items:
   - commit concerns: `adh.one_concern_per_commit`, `dp13`;
   - the final report: `adh.final_report_claims`, `hg.false_completion_claim`;
   - batching: `dp07`, `dp07.justification`.

   Each item had 3 independent judges with distinct lenses: diff-content/protocol/skeptic, reproduce/process-audit/skeptic, and artifact/transcript/skeptic. The judges ran on Opus 5.5 (high effort), read-only and blind to every score file, under the rules in `execution-protocol.md`, D8 and D24, and checkpoint-5 Q2/Q5. Every verdict was unanimous. I read the final report in full and spot-checked the decisive claims myself: the amend is disclosed, and 44 tests pass on rerun. Result: `claude-code/manual-verdicts.json`.
3. `score-run.py … --manual` gives the final `score.json` with no `needs_judge` left.
4. Observations: `telemetry/observations.json` (my Node export of the uploaded traces) becomes the bare list `claude-code/langfuse-observations.json`, which is what `langfuse-run.py --full` writes.
5. `langfuse-coverage.py --observations` produces `claude-code/harness-coverage.json`.
6. `claude-code/evaluation-inputs.json` is the `geeky-eval.inputs/1` scope. It binds every input by path, SHA-256 and ancestry, plus the authorized segment from `run.json` `execution`.
7. `parity-eval.py … --scope` gives `claude-code/parity.json`.
8. `langfuse-scores.py --dry-run` gives `claude-code/score-preview.json`: 10 typed session scores, verified binding, **no remote write**.

Audit: I checked every failing check, and the sensitive passes, against the raw evidence:
- no orchestrator code edits;
- stage windows, taken from the reflog. spec-research ends at the pre-amend commit (04:36:58), not the amended 6c4ad43.

## Scorer and harness findings from the audit

1. **score-run drops `python3` validation commands on POSIX.** The claude-code filter `(python|py|pytest|…)\b` cannot match `python3`, so `Tests/Validation` reruns were a vacuous 0/0 (Quality D = 1.0). The codex branch accepts `python3` and refuses a vacuous 0/0. I reran the 5 selected commands at each task's own commit: all pass (`claude-code/validation-rerun.txt`), so Quality does not change here. Fixed in `63cd9b3`: a shared `python(?:\d+(?:\.\d+)*)?` head. Re-scored r2 gives 5/5, with Quality and the composite unchanged. Refusing a vacuous 0/0 for claude-code too is held: a claude run whose plans write plain-bullet commands would then fail to score.
2. **`reasoning_summaries` missed.** This is a real capture gap. claude-sonnet-5-5 returned every thinking block signature-only through the gateway, even with `showThinkingSummaries` on, so no reasoning text exists to trace. Cause: in `-p` mode Claude Code 2.1.314 ignores `showThinkingSummaries` and sends no `display`, and Claude 5 models default to `omitted`. `ea144eb` adds `--thinking-display summarized`; the gateway accepts it (one call).
3. **Observation export shape.** The runner's `langfuse-check.mts export` wraps the list. It should write the bare list `langfuse-run.py --full` produces, so `langfuse-coverage.py` reads it directly.
4. **Score environment.** `langfuse-scores.py` falls back to environment `mxc` when `run.json.telemetry` has none. This run used native OTel, so `telemetry` is `{enabled: false}`. `claude-code/publish-run/20261030T043030Z/run.json` is a source-reviewed copy that adds the telemetry identity (session, `development`, coverage) and is otherwise identical. The scope binds it with ancestry to the original.

## Confounds (what is not equal)

1. **Python 3.9.6, not 3.11+.** Santa blocks the python.org 3.14 build, so the contestant tests, validation reruns and oracle all ran on 3.9. The C0 contract asks for 3.11+. The runner marks the run `reference_eligible`, but these confounds make it a diagnostic.
2. **Sandbox defect.** The heredoc failure above caused the hard gate.
3. **MCP server.** The geeky MCP server ran from the Node mirror (`server.mts`). The contestant ran every gate as the Python scripts through Bash and made 0 MCP calls.
4. **Telemetry path.** Native OTel plus a post-run transcript upload replaced the pinned hook. The span shape is the same, and parity-eval consumed it unchanged.
5. **One run.** There is no variance estimate (D60 wants three).
6. **Model and gateway.** Sonnet 5.5 at medium effort, against upstream run 4's `ant-claude` (`mimo-v2.6-flash`) at default. Do not read the gaps as harness effects.

## Langfuse

- **Session:** `5a358c3e-fad1-4086-a37a-7dbfdd89a5b9`, project `geeky-orchestration-eval`, environment `development`.
- **Traces:** 14 traces with 448 observations (169 generations, 29 spans, 250 tools, 15 subagent containers), carrying full prompt, response and tool content up to 20k characters per field. The native OTel spans were also delivered.
- **Already posted** (`GEEKY_EVAL_SCORES=1`, during the run's post-processing): 11 run-level `geeky.*` scores.
- **Published 2026-10-30** with owner write authorization (QUICKSTART §E): the 10 upstream parity scores (`parity_composite`, `quality`, `process_adherence`, `process_adherence_uncapped`, `process_consistency`, `oracle_pass_rate`, `forbidden_git_operation`, `harness_coverage_complete`, `provider_refusals`, `final_report_claims_verified`), using `langfuse-scores.py` with `--scope` and `--coverage`, evaluator `parity-eval/1`.
  - The first attempt returned `pending` (`score-readback.attempt1.json`): the POSTs were accepted, but the immediate v3 readback found nothing yet.
  - A read-only check then found all 10 with exact values and binding. The same command, re-run (deterministic IDs, so it upserts), returned `verified` with 10/10 (`score-readback.json`).

## Files

- `claude-code/score.json`, `score.md`, `score.log`: score-run with the judge verdicts. Harness-specific; see Method.
- `claude-code/parity.json`, `parity.log`: the harness-neutral evaluation, with every check and its evidence.
- `claude-code/manual-verdicts.json`: the judge verdicts.
- `claude-code/analysis.json`, `analyze.log`: analyze-run.
- `claude-code/harness-coverage.json`, `validation-rerun.txt`, `evaluation-inputs.json`, `publish-run/`, `score-preview.json`: audit and publication inputs.
- `claude-code/score-readback.json` (verified), `score-readback.attempt1.json` (pending, ingestion lag): publication records.
- `claude-code/langfuse-observations.json` (1.9 MB): the observation list the scope binds; Langfuse holds the same data.
