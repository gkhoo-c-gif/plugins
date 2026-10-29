"""Explicit SCRIPTED MOCK C0 fixture; no agent, provider, network or oracle calls.

Feature requirements come only from public brief.md/contract.md. The mock reads
real workflow skill files to emit stage-entry events, then runs local gates, tests
and candidate commits; scripted reviews do not prove model compliance.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from mock_claude import package_files, kanban_text, STAGES

LABEL = "SCRIPTED MOCK"
SOURCE = '''"""Deterministic wordstat CLI, scripted mock fixture from the public contract."""
import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys


def positive(value):
    # Avoid Python's decimal conversion limit for arbitrarily large positive N.
    if not re.fullmatch(r"[+]?[0-9]+", value) or not value.lstrip("+0"):
        raise argparse.ArgumentTypeError("must be an integer >= 1")
    return value


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8", newline="\\n")
    sys.stderr.reconfigure(encoding="utf-8", newline="\\n")
    parser = argparse.ArgumentParser(prog="wordstat")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--top", type=positive, default="3")
    parser.add_argument("file")
    args = parser.parse_args(argv)
    try:
        text = Path(args.file).read_bytes().decode("utf-8-sig")
    except OSError:
        print(f"wordstat: error: {args.file}: cannot read file", file=sys.stderr)
        return 3
    except UnicodeDecodeError:
        print(f"wordstat: error: {args.file}: not valid UTF-8", file=sys.stderr)
        return 3
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    ranked = sorted(Counter(tokens).items(), key=lambda item: (-item[1], item[0]))
    digits = args.top.lstrip("+0")
    limit = len(ranked) if len(digits) > len(str(len(ranked))) else min(int(digits), len(ranked))
    result = {"lines": len(text.splitlines()), "words": len(tokens), "chars": len(text),
              "top": [{"word": word, "count": count} for word, count in ranked[:limit]]}
    if args.json:
        print(json.dumps(result))
    else:
        for name in ("lines", "words", "chars"):
            print(f"{name}: {result[name]}")
        top = " ".join(f"{item['word']}={item['count']}" for item in result["top"])
        print("top:" + (" " + top if top else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

TESTS = r'''"""SCRIPTED MOCK tests derived from the public wordstat contract."""
import json
from pathlib import Path
import subprocess
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]


def cli(*args):
    return subprocess.run([sys.executable, "-m", "wordstat", *args], cwd=ROOT,
                          input=b"ignored stdin", capture_output=True, timeout=10)


def test_help():
    assert cli("--help").returncode == 0


@pytest.mark.parametrize("payload,lines,words,chars,top", [
    (b"", 0, 0, 0, []),
    (b"The cat sat.\nThe cat ran!\nA dog.\n", 3, 8, 33,
     [{"word":"cat","count":2},{"word":"the","count":2},{"word":"a","count":1}]),
    (b"\xef\xbb\xbfA\r\nA\r\n", 2, 2, 6, [{"word":"a","count":2}]),
    ("É a_42\u2028B".encode(), 2, 3, 8,
     [{"word":"42","count":1},{"word":"a","count":1},{"word":"b","count":1}]),
])
def test_json(tmp_path, payload, lines, words, chars, top):
    file = tmp_path / "input.txt"
    file.write_bytes(payload)
    p = cli("--json", str(file))
    expected = {"lines":lines,"words":words,"chars":chars,"top":top}
    assert p.returncode == 0 and p.stderr == b""
    assert p.stdout == (json.dumps(expected) + "\n").encode()


def test_text_and_large_top(tmp_path):
    file = tmp_path / "sample.txt"
    file.write_bytes(b"b a b")
    p = cli(str(file))
    assert p.stdout == b"lines: 1\nwords: 3\nchars: 5\ntop: b=2 a=1\n"
    p = cli("--json", "--top", "9" * 5000, str(file))
    assert p.returncode == 0 and len(json.loads(p.stdout)["top"]) == 2
    assert len(json.loads(cli("--json", "--top", "1", str(file)).stdout)["top"]) == 1


def test_empty_text(tmp_path):
    file = tmp_path / "empty"
    file.write_bytes(b"")
    assert cli(str(file)).stdout == b"lines: 0\nwords: 0\nchars: 0\ntop:\n"


@pytest.mark.parametrize("flags", [[], ["--unknown"], ["--top", "0", "x"],
    ["--top", "-1", "x"], ["--top", "1.5", "x"], ["--top", "no", "x"], ["a", "b"]])
def test_usage(flags):
    p = cli(*flags)
    assert p.returncode == 2 and not p.stdout
    assert p.stderr.splitlines()[-1].startswith(b"wordstat: error: ")
    assert b"Traceback" not in p.stderr


@pytest.mark.parametrize("kind,message", [("missing", "cannot read file"),
    ("directory", "cannot read file"), ("invalid", "not valid UTF-8")])
def test_file_errors(tmp_path, kind, message):
    file = tmp_path / kind
    if kind == "directory":
        file.mkdir()
    elif kind == "invalid":
        file.write_bytes(b"\xff")
    p = cli(str(file))
    assert p.returncode == 3 and not p.stdout
    assert p.stderr == f"wordstat: error: {file}: {message}\n".encode()
'''


def execute(workspace: Path, plugin: Path, prompt: str, report, write_kanban=None):
    """Run only in a disposable seeded candidate Git workspace, never the plugin."""
    workspace, plugin = workspace.resolve(), plugin.resolve()
    if workspace == plugin or plugin in workspace.parents or (workspace / "evals").exists():
        raise RuntimeError("c0-full requires a disposable candidate workspace")
    if not (workspace / ".git").is_dir():
        raise RuntimeError("c0-full requires the candidate Git repository")
    match = re.search(r"docs/spec-[\w.-]+", prompt)
    folder = workspace / (match.group(0) if match else "docs/spec-001-feature")
    if folder.exists() or (workspace / "wordstat").exists():
        raise RuntimeError("c0-full requires a fresh candidate")
    deadline = os.environ.get("GEEKY_DEADLINE_UTC", "")
    parsed = dt.datetime.fromisoformat(deadline.replace("Z", "+00:00"))
    reserve = int(os.environ.get("GEEKY_CLOSEOUT_RESERVE_S", "-1"))
    if parsed.utcoffset() != dt.timedelta(0) or reserve < 0:
        raise RuntimeError("c0-full requires UTC deadline and nonnegative reserve")
    sources = {name: (workspace / name).read_bytes().decode("utf-8-sig")
               for name in ("brief.md", "contract.md")}
    if "wordstat" not in sources["brief.md"] or "utf-8-sig" not in sources["contract.md"]:
        raise RuntimeError("c0-full supports only the public wordstat contract")
    evidence = folder / "mock-evidence"
    evidence.mkdir(parents=True)
    commands, stages, commits = [], [], []
    # The local regression suite uses this isolated negative control; runners never set it.
    split_tests = os.environ.get("GEEKY_MOCK_C0_SPLIT_TESTS") == "1"

    def write(path, body):
        if path.name == "kanban.md" and write_kanban:
            if not write_kanban(path.resolve(), body):
                raise RuntimeError(f"kanban write failed: {path}")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body.encode("utf-8"))  # == write_text(newline="\n"); 3.9 has no newline=

    def command(name, argv):
        started_at_ms = int(time.time() * 1000)
        result = subprocess.run([str(x) for x in argv], cwd=workspace, capture_output=True,
                                encoding="utf-8", errors="replace", timeout=90)
        item = {"name": name, "argv": [str(x) for x in argv], "exit_code": result.returncode,
                "stdout": result.stdout, "stderr": result.stderr, "started_at_ms": started_at_ms}
        commands.append(item)
        write(workspace / ".git/c0-full-commands.json", json.dumps(commands, indent=2) + "\n")
        report(name, item)
        if result.returncode:
            raise RuntimeError(f"{name}: exit {result.returncode}: {result.stdout}{result.stderr}")
        return result.stdout.strip()

    def gate(name, *args):
        return command(name, [sys.executable, plugin / "scripts" / f"{name}.py", *args])

    def commit(message, paths, tasks=None):
        msg = workspace / ".git" / "c0-mock-commit.txt"
        trailer = f"\n\nTasks: {tasks}\n" if tasks else "\n\nReferences T1 and T2 planning notes.\n"
        write(msg, message + trailer)
        gate("check-commit", str(msg))
        write(evidence / "commands.json", json.dumps(commands, indent=2) + "\n")
        command("git.add", ["git", "add", "--", *paths])
        command("git.commit", ["git", "commit", "-q", "-F", msg])
        commits.append(command("git.revision", ["git", "rev-parse", "HEAD"]))

    def stage(name, detail):
        stages.append({"stage": name, "label": LABEL, "detail": detail})
        actual = {**stages[-1], "skill": name, "args": ""}
        if report(name, actual) is False:
            raise RuntimeError(f"could not read stage skill: {name}")

    def board(lane):
        text = kanban_text({lane: ["T1", "T2"]}, {"T1": "Implement wordstat contract", "T2": "Test wordstat contract"})
        if lane == "In Progress":
            text = re.sub(r"(?m)^(- T\d+ .+)$", r"\1 in_progress", text)
        return text

    stage(STAGES[0], "Read only public brief.md and contract.md; copied requirements R1-R6.")
    write(folder / ".heartbeat", json.dumps({"ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                                             "task": "T1", "status": "running"}) + "\n")
    write(folder / "feature-specification.md", "# Feature specification\n\nSCRIPTED MOCK\n\n" + sources["brief.md"] + "\n" + sources["contract.md"])
    stage(STAGES[1], "Create the public-contract planning package with local validators.")
    for rel, body in package_files(plugin, "wordstat", {"Ready": ["T1", "T2"]}).items():
        body = body.replace("The real feature logic (mock stub only).", "Provider, network and deployment calls.")
        body = body.replace("Scaffold the package", "Implement wordstat contract").replace("Add smoke tests", "Test wordstat contract")
        body = body.replace("Mock-agent dry run:", "SCRIPTED MOCK public contract fixture:")
        write(folder / rel, body)
    gate("validate-planning-folder", "--path", folder)
    gate("validate-task-schema", "--path", folder)
    gate("validate-kanban", "--path", folder)
    commit("docs(plan): script public C0 planning fixture", [str(folder.relative_to(workspace))])
    stage(STAGES[2], "Review the plan and report the successful local gate results.")
    write(folder / "review-plan.md", "# Plan review\n\nSCRIPTED MOCK reviewer scripted-plan: PASS.\nReal planning/schema/kanban checks passed; no model was dispatched.\n")
    titles = {"T1": "Implement wordstat contract", "T2": "Test wordstat contract"}
    stage(STAGES[3], "Implement the Python contract, test it, and move both tasks through review and Done.")
    write(folder / "kanban.md", board("In Progress"))
    gate("validate-kanban", "--path", folder)
    write(workspace / "wordstat/__init__.py", '"""wordstat SCRIPTED MOCK C0 fixture."""\n')
    write(workspace / "wordstat/__main__.py", SOURCE)
    write(workspace / "tests/test_wordstat.py", TESTS)
    output = command("pytest", [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"])
    count = int(re.search(r"(\d+) passed", output).group(1))
    write(folder / "kanban.md", board("In Review"))
    gate("validate-kanban", "--path", folder)
    hashes = {p.relative_to(workspace).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted([*(workspace / "wordstat").glob("*.py"), *(workspace / "tests").glob("*.py")])}
    reviewed = json.dumps(hashes, sort_keys=True)
    phase = f"# Phase 1 review\n\nSCRIPTED MOCK reviewer: scripted-phase-1\nReviewed file hashes: {reviewed}\nT1: PASS; T2: PASS. pytest: {count} passed.\nPhase 1: READY TO ADVANCE\nNo real reviewer dispatched.\n"
    write(folder / "review-phase-1.md", phase)
    write(folder / "handoff.md", "# Handoff\n\nSCRIPTED MOCK\n\n## Phase 1\nReviewer: scripted-phase-1\nT1: PASS; T2: PASS\nREADY TO ADVANCE\nEvidence: review-phase-1.md; mock-evidence/commands.json\n")
    write(folder / "tasks/T1-scaffold-package.notes.md", f"# T1 notes\n\nSCRIPTED MOCK; public R1-R6 implemented; pytest {count} passed.\nHashes: {reviewed}\n")
    write(folder / "tasks/T2-smoke-tests.notes.md", f"# T2 notes\n\nSCRIPTED MOCK; python -m pytest -q -p no:cacheprovider tests: {count} passed.\n")
    write(folder / "kanban.md", board("Done"))
    gate("validate-kanban", "--path", folder)
    for task in ("T1", "T2"):
        gate("check-dod", "--path", folder, "--task", task)
    if split_tests:
        commit("feat(wordstat): implement public C0 contract before tests",
               ["wordstat", str(folder.relative_to(workspace))], "T1, T2")
        commit("test(wordstat): add required C0 contract tests", ["tests"], "T2")
    else:
        commit("feat(wordstat): implement public C0 contract with tests",
               ["wordstat", "tests", str(folder.relative_to(workspace))], "T1, T2")
    stage(STAGES[4], "Review the source and tests; save three scripted domain replies and close the run.")
    replies = []
    for domain, scope in (("cli-contract", "wordstat/__main__.py CLI and error paths"),
                          ("text-determinism", "wordstat/__main__.py decoding, counting and ordering"),
                          ("test-coverage", "tests/test_wordstat.py public R1-R6 subprocess assertions")):
        reply = f"## {domain}\nSCRIPTED MOCK reviewer: scripted-{domain}\nReviewed revision: {commits[-1]}\nReviewed hashes: {reviewed}\nScope: {scope}\nEvidence: mock-evidence/commands.json; pytest {count} passed.\nRequired findings: none (scripted fixture judgment).\nVerdict: PASS\n<!-- reviewer-complete: {domain} -->\n"
        write(evidence / f"review-{domain}.md", reply)
        replies.append(reply)
        write(folder / "review-impl.md", "# Implementation review\n\nSCRIPTED MOCK; no model compliance claim.\n\n" + "\n".join(replies))
    write(folder / "review-impl.md", (folder / "review-impl.md").read_text(encoding="utf-8") + "\nOverall: PASS\n")
    summary = {"label": LABEL, "model_calls": 0, "billing_metrics": "not applicable; scripted local execution",
               "deadline_environment": {"GEEKY_DEADLINE_UTC": deadline, "GEEKY_CLOSEOUT_RESERVE_S": reserve},
               "stages": stages, "tests_passed": count, "commits": commits,
               "source_hashes": hashes, "phase_verdict": "READY TO ADVANCE", "final_verdict": "PASS",
               "saved_domain_replies": [str((evidence / f"review-{d}.md").relative_to(workspace))
                   for d in ("cli-contract", "text-determinism", "test-coverage")],
               "planning_folder": str(folder.relative_to(workspace)), "closeout": "done"}
    write(folder / "run-summary.md", "# Run Summary\n\nSCRIPTED MOCK\n" + json.dumps(summary, indent=2) + "\n")
    with (folder / "handoff.md").open("a", encoding="utf-8", newline="\n") as file:
        file.write(f"\n## Run Summary\nAll five scripted stages complete; {count} tests passed; T1/T2 Done; final review PASS.\nNo model/provider/network calls. No deployment.\n")
    write(folder / ".heartbeat", json.dumps({"ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                                             "task": "T2", "status": "done"}) + "\n")
    commit("docs(handoff): close scripted C0 reviews and summary", [str(folder.relative_to(workspace))])
    summary["commits"] = list(commits)
    # Final manifest lives outside tracked artifacts to avoid a recursive commit hash.
    write(workspace / ".git/c0-full-summary.json", json.dumps(summary, indent=2) + "\n")
    summary["completion_report"] = (f"SCRIPTED MOCK C0 complete: five stages ({', '.join(STAGES)}); "
        f"{count} tests passed; {len(commits)} real candidate commits; one phase READY TO ADVANCE; "
        "three saved domain replies, Overall PASS; heartbeat done. Public contract only. "
        "Zero model calls; no instruction-obedience or billing evidence. " + "Commits: " + ", ".join(commits))
    return summary
