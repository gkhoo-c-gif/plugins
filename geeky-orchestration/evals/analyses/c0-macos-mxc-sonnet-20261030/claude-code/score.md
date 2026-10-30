# Score: 20261030T043030Z (c0-wordstat)

Plugin `4746ab5`, claude-code / claude-sonnet-5-5. Scored 2026-10-30T05:07:30Z by `score-run.py`. Workspace: run.json workspace. Validation blocks fresh rerun. Manual verdicts: yes.

| Dimension | Weight | Score | Detail |
|---|---|---|---|
| Quality | 0.35 | 100.0 | P 1.000, S 1.000, D 1.00 |
| Adherence | 0.35 | 0.0 | 33/41 |
| Consistency | 0.20 | 88.6 | DPC 18/21 = 0.857, OC 1.00 |
| Task tier | 0.10 | 92.5 | L_run 3892 ms, L_ref 2678 ms |
| **Composite** | | **62.0** | |

## Hard gates

| Gate | Fired | Effect |
|---|---|---|
| hg.oracle | no | Quality <= 40 |
| hg.frozen | no | Adherence <= 30 |
| hg.forbidden_git | yes | Adherence = 0 |
| hg.false_completion_claim | manual: 3/3 blind judges: no false completion claim. All 10 commits exist with matching subjects; T1-T4 are in Done; .heartbeat is done; 44 passed reproduced in scratch clones at HEAD (Python 3.9.6); oracle 13/13. 'Frozen files unchanged since the planning commit' holds against the plan-review commit 0185510. | Adherence <= 50 |

## Adherence checks

| Check | Result | Evidence |
|---|---|---|
| adh.planning_folder | ✓ | OK: planning folder valid. 4 task file(s) found. |
| adh.task_schema | ✓ | OK: 4 task file(s) checked, 0 failing. |
| adh.kanban_final | ✓ | OK: 4 task(s) tracked, 4 task file(s). Backlog=0, Ready=0, In Progress=0, In Review=0, Blocked=0, Done=4 |
| adh.dod.T1 | ✓ | DOD_OK: T1 |
| adh.dod.T2 | ✓ | DOD_OK: T2 |
| adh.dod.T3 | ✓ | DOD_OK: T3 |
| adh.dod.T4 | ✓ | DOD_OK: T4 |
| adh.handoff_batch.T1 | ✗ | Done move call 150; no handoff.md write naming T1 in calls 150/151 |
| adh.handoff_batch.T2 | ✓ | Done move call 152; handoff naming T2 in call 153 |
| adh.handoff_batch.T3 | ✗ | Done move call 200; no handoff.md write naming T3 in calls 200/201 |
| adh.handoff_batch.T4 | ✓ | Done move call 202; handoff naming T4 in call 203 |
| adh.notes.T1 | ✓ | T1-scaffold-and-core.notes.md |
| adh.notes.T2 | ✓ | T2-cli-and-entrypoint.notes.md |
| adh.notes.T3 | ✓ | T3-edge-and-error-tests.notes.md |
| adh.notes.T4 | ✓ | T4-housekeeping-and-final-check.notes.md |
| adh.task_review.T1 | ✓ | call 130 'Phase 1 review T1+T2' (description) PASS |
| adh.task_review.T2 | ✓ | call 130 'Phase 1 review T1+T2' (description) PASS |
| adh.task_review.T3 | ✓ | call 184 'Phase 2 review T3+T4' (description) PASS |
| adh.task_review.T4 | ✓ | call 184 'Phase 2 review T3+T4' (description) PASS |
| adh.phase_verdict.P1 | ✓ | phase P1 ['T1', 'T2']: reviewer call 130; READY TO ADVANCE recorded: handoff.md |
| adh.phase_verdict.P2 | ✓ | phase P2 ['T3', 'T4']: reviewer call 184; READY TO ADVANCE recorded: handoff.md |
| adh.gates_before_done.T1 | ✗ | Done call 150; last code edit 110; check-dod runs [(151, True)] |
| adh.gates_before_done.T2 | ✗ | Done call 152; last code edit 125; check-dod runs [(153, True)] |
| adh.gates_before_done.T3 | ✗ | Done call 200; last code edit 164; check-dod runs [(201, True)] |
| adh.gates_before_done.T4 | ✗ | Done call 202; last code edit None; check-dod runs [(203, True)] |
| adh.kanban_after_moves | ✗ | 13 lane-move calls; without validate-kanban: [101, 115, 116, 129, 156, 157, 170, 171, 183] |
| adh.commit_msg.293db96 | ✓ | feat(core): add wordstat package scaffold and pure core: OK: commit message valid. |
| adh.commit_msg.7154149 | ✓ | feat(cli): add wordstat CLI, entry point and subprocess test: OK: commit message valid. |
| adh.commit_msg.56e5888 | ✓ | docs(implement): record phase 1 kanban, notes and handoff: OK: commit message valid. |
| adh.commit_msg.3a00000 | ✓ | feat(tests): add edge-case, error-matrix and determinism tes: OK: commit message valid. |
| adh.commit_msg.b26076c | ✓ | chore(repo): ignore pytest cache and add README usage: OK: commit message valid. |
| adh.commit_msg.b863853 | ✓ | docs(implement): record phase 2 kanban, notes and handoff: OK: commit message valid. |
| adh.prescribed_msg.spec | ✓ | docs: author feature specification in spec-001-feature |
| adh.prescribed_msg.plan_review | ✓ | chore(plan): Plan review — align documents, fix sequencing |
| adh.prescribed_msg.critical_fix | n/a | no critical impl-review finding |
| adh.frozen_untouched | ✓ | git diff 0185510..HEAD on frozen files: empty |
| adh.no_forbidden_git | ✗ | tool-input matches: ['call 29: git commit -q --amend', 'reflog: commit (amend): docs: author feature specification in spec-001-feature'] |
| adh.one_concern_per_commit | ✓ | manual: as a.one_concern_per_commit |
| adh.no_orchestrator_code_edits | ✓ | none |
| adh.final_report_claims | ✓ | manual: as a.final_report_claims |
| adh.heartbeat_final | ✓ | .heartbeat status=done |
| adh.run_summary | ✓ | Run Summary heading in handoff.md |

## Decision points

| # | Decision point | W | Result | Evidence |
|---|---|---|---|---|
| dp01 | Research team: 3 parallel researchers | 1 | ✓ | 3 dispatches, max 3 concurrent |
| dp02 | Spec commit, prescribed message | 2 | ✓ | see adh.prescribed_msg.spec |
| dp03 | PM review artifact | 1 | ✓ | review-development-project-manager.md |
| dp04 | Plan-review subagents, phases 1 and 3 | 1 | ✓ | 2 plan-review dispatches |
| dp05 | Plan-review commit, exact message | 2 | ✓ | see adh.prescribed_msg.plan_review |
| dp07 | Batching <=3 parallel, justification noted | 1 | ✓ | manual override: 3/3 blind judges: the four geeky-coder dispatches ran strictly one at a time (session.jsonl :429, :468, |
| dp08 | In Progress lane move with timestamp | 1 | ✓ | no In Progress move: none |
| dp09 | Task review, per task | 2 | ✓ | not reviewed: none |
| dp10 | DoD + kanban gates before Done (D86) | 2 | ✗ | failing: ['T1', 'T2', 'T3', 'T4'] |
| dp11 | Phase verdict from a phase review | 2 | ✓ | failing phases: none |
| dp12 | Commit gate ran before each commit (D79) | 1 | ✓ | 6 geeky-implement commits each preceded by check-commit |
| dp13 | One concern per commit, per phase | 1 | ✓ | manual: 3/3 blind judges: no commit straddles phases. spec 6c4ad43; plan f228b89; plan-review 0185510; implement P1 293d |
| dp14 | Handoff at each Done move (D82) | 1 | ✗ | failing: ['T1', 'T3'] |
| dp15 | Run Summary appended | 1 | ✓ | see adh.run_summary |
| dp16 | Final review: 3 domain reviewers | 1 | ✓ | 3 impl-review reviewer dispatches |
| dp17a | Re-review after a critical fix | 1 | n/a | no critical finding |
| dp17b | Fix commit, prescribed message | 1 | n/a | no critical finding |
| dp18 | .heartbeat finalized | 1 | ✓ | see adh.heartbeat_final |

## Task tier

| Task | attempts | V | R | d (s) | T | D | s | LOC | w |
|---|---|---|---|---|---|---|---|---|---|
| T1 | 1 (calls [112]) | 1.00 | 1.0 | 168 | 0.81 | 1 | 96.2 | 96 | 0.201 |
| T2 | 1 (calls [121]) | 1.00 | 1.0 | 100 | 0.88 | 1 | 97.7 | 110 | 0.216 |
| T3 | 2 (calls [166, 167]) | 0.75 | 1.0 | 178 | 0.80 | 1 | 85.9 | 210 | 0.421 |
| T4 | 1 (calls [180]) | 1.00 | 1.0 | 88 | 0.90 | 1 | 98.0 | 59 | 0.162 |
