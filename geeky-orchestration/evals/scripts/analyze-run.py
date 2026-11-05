"""Derive baseline-run measures from a run folder and merge them into run.json.

Usage: python analyze-run.py <run-folder> [--token-ceiling N] [--analysis-only]

Reads stream.jsonl (claude stream-json) and session.jsonl (Claude session transcript, which
carries the timestamps the stream lacks). Writes analysis.json and updates these run.json fields:
tokens (summed over every model in result.modelUsage), model_calls, latency_median_ms,
max_idle_gap_s, stages, impl_review_rounds, token_ceiling, over_token_ceiling.
--analysis-only writes analysis.json and leaves run.json untouched (re-analysing a stored run).

Subagent transcripts: when the run folder has subagents/agent-<id>.jsonl (+ .meta.json), tool calls found
only there (e.g. calls of workers dispatched by a subagent, which the stream omits) are added to the tool,
dispatch, validation and gate counts, deduplicated by tool_use id, and analysis.json gains
"subagent_transcripts" with a main / orchestrator-subagent / worker split. Tokens, model calls and latency
still come from the result event and session.jsonl only.

Gate runs are counted from each tool call's result: every gate summary line (OK/INVALID_*,
DOD_OK/DOD_INCOMPLETE, the check-commit verdict) is one run with its pass/fail. When a command
executes a gate but its output hides the summary, the run is counted from the command text
(each <gate>.py/.ps1 execution, multiplied by literal foreach lists and helper-function calls)
with an unknown result. A helper function that prints its gate's verdict is counted by the
verdicts shown once any is shown. When a commit helper's gate failure is shown and the caller
knows how many commits the call made (score-run), the hidden runs are at most one pass per
commit, if every gate run and commit of the call is inside functions that run the gate.
Reading a gate script does not count. MCP gate tools count by their "ok" field.
Stdlib only.
"""
import argparse
import ast
import bisect
import collections
import datetime as dt
import json
import re
import statistics
import importlib.util
from pathlib import Path


def load_jsonl(path):
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("{"):
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # truncated last line
    return rows


def ts(event):
    t = event.get("timestamp")
    return dt.datetime.fromisoformat(t.replace("Z", "+00:00")) if t else None


def tool_uses(event):
    content = (event.get("message") or {}).get("content") or []
    return [c for c in content if isinstance(c, dict) and c.get("type") == "tool_use"]


def tool_results(event):
    """{tool_use_id: (text, is_error)} for the tool_result blocks of a user event."""
    content = (event.get("message") or {}).get("content")
    out = {}
    for c in content if isinstance(content, list) else []:
        if isinstance(c, dict) and c.get("type") == "tool_result":
            text = c.get("content")
            if isinstance(text, list):
                text = "\n".join(x.get("text", "") for x in text if isinstance(x, dict))
            out[c.get("tool_use_id")] = (text if isinstance(text, str) else "", bool(c.get("is_error")))
    return out


ORCHESTRATOR_AGENT = "geeky-implement-orchestrator"


def is_orchestrator_type(subagent_type):
    """True for the plugin's geeky-implement orchestrator subagent (plain or plugin-qualified name)."""
    return str(subagent_type or "").split(":")[-1] == ORCHESTRATOR_AGENT


def load_subagents(run_dir):
    """[(meta, events)] for each subagents/agent-<id>.jsonl (a Claude Code subagent session transcript) with
    its agent-<id>.meta.json (agentType, description, toolUseId of the spawning Agent call, spawnDepth)."""
    folder = Path(run_dir) / "subagents"
    out = []
    if not folder.is_dir():
        return out
    for f in sorted(folder.glob("agent-*.jsonl")):
        mp = f.with_name(f.name[:-len(".jsonl")] + ".meta.json")
        try:
            meta = json.loads(mp.read_text(encoding="utf-8-sig")) if mp.exists() else {}
        except ValueError:
            meta = {}
        meta.setdefault("agentId", f.name[len("agent-"):-len(".jsonl")])
        out.append((meta, load_jsonl(f)))
    return out


def transcript_calls(run_dir, known_ids=()):
    """Tool calls recorded only in subagent transcripts, with their results.

    Returns (calls, metas). Each call is a dict {id, name, input, parent, timestamp, result, result_timestamp}
    whose parent is the meta.toolUseId of the transcript, i.e. the Agent call that spawned the subagent, so
    calls nested under a subagent's own dispatches link to that dispatch. Calls whose id is in known_ids (the
    stream already carries them) are skipped, as is any id seen in an earlier transcript. metas maps
    toolUseId -> meta, with "_end" set to the transcript's last timestamp."""
    known = set(known_ids)
    calls, metas = [], {}
    for meta, events in load_subagents(run_dir):
        parent = meta.get("toolUseId")
        stamps = [e["timestamp"] for e in events if e.get("timestamp")]
        if parent:
            metas[parent] = dict(meta, _end=max(stamps) if stamps else None)
        results = {}
        for e in events:
            if e.get("type") == "user":
                for k, v in tool_results(e).items():
                    results[k] = (v, e.get("timestamp"))
        for e in events:
            if e.get("type") != "assistant":
                continue
            for c in tool_uses(e):
                if c.get("id") in known:
                    continue
                known.add(c.get("id"))
                res, rts = results.get(c.get("id"), (None, None))
                calls.append({"id": c.get("id"), "name": c.get("name"), "input": c.get("input") or {},
                              "parent": parent, "timestamp": e.get("timestamp"), "result": res,
                              "result_timestamp": rts})
    calls.sort(key=lambda c: c["timestamp"] or "")
    return calls, metas


GATES = ("validate-planning-folder", "validate-task-schema", "validate-kanban", "check-dod", "check-commit")
MCP_PREFIXES = ("mcp__geeky__", "mcp__plugin_geeky-orchestration_geeky__")
# One summary line per gate run (scripts/*.py and *.ps1 print the same text); group 1 is the verdict.
GATE_LINES = {
    "validate-task-schema": re.compile(r"\b(OK|INVALID_TASK_SCHEMA): \d+ task file\(s\) checked"),
    "validate-kanban": re.compile(r"\b(OK|INVALID_KANBAN): (?:\d+ task\(s\) tracked|[^{\s].* not found\.)"),
    "validate-planning-folder": re.compile(r"\b(OK: planning folder valid\.|INVALID_PLANNING_FOLDER: )"),
    "check-dod": re.compile(r"\b(DOD_OK|DOD_INCOMPLETE): \S"),
    "check-commit": re.compile(r"\b(OK: commit message valid\.|INVALID_COMMIT_MESSAGE:)"),
}
CODE_LINE = re.compile(r"print\(|Write-Output|Write-Host|\bf\"")
# A statement starting with one of these only reads or lists a gate script.
READER = re.compile(r"^(?:Get-Content|gc|cat|type|head|tail|less|more|Select-String|sls|grep|rg|findstr|"
                    r"Get-Item|Get-ChildItem|ls|dir|Test-Path|wc|sed|awk|echo|Write-Output|Write-Host)\b", re.I)
GATE_FILE = re.compile(r"(%s)\.(?:py|ps1)\b" % "|".join(GATES))
RUNNER = re.compile(r"(?:^|[\s&])(?:python[\d.]*|py|pwsh|powershell|bash|sh|uv)(?:\.exe)?\b|^&|^[\"']?[\w$:./\\{}()-]*$", re.I)


def _block(text, start):
    """End index (exclusive) of the {...} block whose opening brace is at text[start]."""
    depth = 0
    for i in range(start, len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return i + 1
    return len(text)


def _executed(text):
    """Counter of gate-script executions written literally in text (reads excluded)."""
    found = collections.Counter()
    for stmt in re.split(r"[;\n|{}]|&&", text):
        # an assignment of its result runs it too: PowerShell `$r = python …`, bash `out=$(python …)`, also after
        # local / export / declare and `if !`
        stmt = re.sub(r"^[\s(]*(?:if\s+!?\s*)?(?:(?:local|export|declare|typeset|readonly)(?:\s+-\w+)*\s+)?"
                      r"(?:\$[\w:]+\s*=\s*|\w+=\"?\$\(\s*)?", "", stmt)
        if READER.match(stmt):
            continue
        for m in GATE_FILE.finditer(stmt):
            # run by an interpreter or invoked directly as the statement's first word
            if RUNNER.search(stmt[:m.start()]):
                found[m.group(1)] += 1
    return found


def _loop_items(spec):
    quoted = re.findall(r"['\"]([^'\"]*)['\"]", spec)
    if quoted:
        return quoted
    bare = [s for s in re.split(r"[,\s]+", spec.strip()) if s]
    # a single variable is a collection of unknown size; a comma list of variables ($m1,$m2) has one item each
    return bare if bare and (len(bare) > 1 or not bare[0].startswith("$")) else [None]


def _heredoc_spans(text):
    """[(start, body_start, terminator_start, end)] of the bash heredocs in text: `<<'EOF'` (or <<-, "EOF", EOF) up to
    the first later line that is only the delimiter. Each delimiter's lines are indexed once, so a `<<word` without a
    terminator costs a lookup, not a rescan of the rest of the text. A `<<` inside quotes or a comment opens nothing:
    a first scan finds the bodies, and the quotes are then read with those bodies blanked (a quote in a heredoc body
    pairs with nothing outside it), double-quoted strings spanning lines (`git commit -m "…\\n\\nit's done" && cat >
    x <<'EOF'`)."""
    first = _heredoc_scan(text, None)
    return _heredoc_scan(text, mask_quotes(blank_comments(_blank_bodies(text, first)), multiline=True)) if first else first


def _heredoc_scan(text, code):
    starts, by_word, pos = [], collections.defaultdict(list), 0
    for i, line in enumerate(text.split("\n")):
        starts.append(pos)
        word = line.strip(" \t")
        if re.fullmatch(r"\w+", word):
            by_word[word].append(i)
        pos += len(line) + 1
    spans, after = [], 0
    for m in re.finditer(r"(?=(<<-?[ \t]*(['\"]?)(\w+)\2[^\n]*\n))", text):  # every `<<`, as a regex scan would try
        if m.start() < after or (code is not None and code[m.start()] != "<"):
            continue
        lines = by_word.get(m.group(3), [])
        k = bisect.bisect_right(lines, bisect.bisect_right(starts, m.start()) - 1)
        if k == len(lines):
            continue  # no terminator: not a heredoc
        term = starts[lines[k]]
        after = text.find("\n", term)
        after = len(text) if after < 0 else after  # the end of the terminator line: the scan goes on from there
        spans.append((m.start(), m.start() + len(m.group(1)), term, after))
    return spans


def _blank_bodies(text, spans):
    """text with each span's heredoc body (body_start to terminator_start) blanked, newlines kept."""
    parts, last = [], 0
    for _, body, term, _ in spans:
        parts += [text[last:body], re.sub(r"[^\n]", " ", text[body:term])]
        last = term
    return "".join(parts) + text[last:]


def mask_heredocs(text):
    """text with bash heredoc bodies blanked, same length: `cat > x <<'EOF'\\n…\\nEOF` keeps only its first and last line."""
    return _blank_bodies(text, _heredoc_spans(text))


def mask_written_heredocs(text):
    """text with the bodies of heredocs written to a file blanked, same length: `cat > $T/lanes.py <<'EOF'`,
    `cat <<'EOF' > notes.md`, `tee x <<'EOF'`. A script's source, notes or a usage comment are not commands of this
    shell; a heredoc fed to an interpreter (`python3 - <<'PY'`, `bash <<'EOF'`, `cat <<'EOF' | python3 -`) is left
    as it is."""
    interpreter = r"(?:sudo\s+)?(?:python3?(?:\.\d+)?|bash|sh|zsh|node|pwsh)\b"

    def written(start, body):
        stmt = re.split(r"[;&|]", text[text.rfind("\n", 0, start) + 1:start])[-1].strip()
        if re.search(r"\|\s*" + interpreter, text[start:body]):
            return False
        return re.match(r"(?:sudo\s+)?(?:cat|tee)\b", stmt) or (">" in stmt and not re.match(interpreter, stmt))
    return _blank_bodies(text, [s for s in _heredoc_spans(text) if written(s[0], s[1])])


def mask_written_sources(text):
    """mask_written_heredocs, and also PowerShell here-string bodies piped into Set-Content / Out-File (a helper's
    source, `@' … '@ | Set-Content kmove.ps1`): same length."""
    text = mask_written_heredocs(text)
    if not re.search(r"\|\s*(?:Set-Content|Out-File)\b", text, re.I):
        return text
    return _PS_HERESTRING_SCRIPT.sub(
        lambda m: text[m.start():m.start(2)] + re.sub(r"[^\n]", " ", m.group(2)) + text[m.end(2):m.end()], text)


def blank_comments(text):
    """text with shell comments blanked, same length: a # outside quotes at a word start, to the line end."""
    quoted = mask_quotes(text)
    return "\n".join(l[:m.start()] + " " * (len(l) - m.start()) if (m := re.search(r"(?:^|(?<=\s))#", q)) else l
                     for l, q in zip(text.split("\n"), quoted.split("\n")))


def mask_quotes(text, multiline=False):
    """text with the inside of quoted strings blanked, same length (one linear scan; a backslash escapes the next
    character inside double quotes). Strings end at the line end, except with multiline=True a double-quoted string, or
    a single-quoted one right after -m / --message (a commit message body), may span lines."""
    out, i, n = [], 0, len(text)
    failed = {"'": -1, "\"": -1}  # where the last unterminated scan of each quote ended: quotes before it stay text
    while i < n:
        ch = text[i]
        if ch in failed and i > failed[ch]:
            j = i + 1
            spans = multiline and (ch == "\"" or re.search(r"(?:^|\s)(?:-m|--message)[ \t=]*$", text[max(0, i - 12):i]))
            keep = []  # inside double quotes, $( … ) and ` … ` are commands: left as they are ("$(commit c1.txt)")
            while j < n and text[j] != ch and (spans or text[j] != "\n"):
                if ch == "\"" and text.startswith("$(", j):
                    depth, k = 0, j + 1
                    while k < n:
                        depth += {"(": 1, ")": -1}.get(text[k], 0)
                        if depth == 0:
                            break
                        k += 1
                    keep.append((j, min(k + 1, n)))
                    j = k + 1
                elif ch == "\"" and text[j] == "`" and (k := text.find("`", j + 1)) > 0:
                    keep.append((j, k + 1))
                    j = k + 1
                else:
                    j += 2 if ch == "\"" and text[j] == "\\" and j + 1 < n and text[j + 1] != "\n" else 1
            if j < n and text[j] == ch:
                seg = [" "] * (j - i - 1)
                for a, b in keep:
                    seg[a - i - 1:b - i - 1] = text[a:b]
                out.append(ch + "".join(seg) + ch)
                i = j + 1
                continue
            failed[ch] = j
        out.append(ch)
        i += 1
    return "".join(out)


# what may stand right before a command: a line start, a separator, `( { ! `` `, a keyword, or a PowerShell `$r =`
_CALL_PREFIX = r"(?:^|[;{(|&!`]|\$[\w:]+[ \t]*=|\b(?:do|then|else|elif|if|while|until|time|command)\b)[ \t]*"
# A helper function definition: bash / zsh `name() {`, or `function name {` / `function name() {` (bash), PowerShell
# `function Commit-Msg($m) {`. Both forms are read the same way.
_FUNC_DEF = re.compile(r"(?:^|[;&|{])[ \t]*(?P<def>(?i:function)[ \t]+(?P<kw>[A-Za-z_][\w-]*)[ \t]*(?:\([^)\n]*\))?"
                       r"|(?P<po>[A-Za-z_][\w-]*)[ \t]*\(\))\s*\{", re.M)
# standard output sent away or filtered: any > / >> / &> / *> redirect (to /dev/null, $null or a file; 2> and >&2 are
# not it: stderr is in the tool result), | Out-Null / Out-File / Set-Content / Select …, | tail / head / grep …, -q
_QUIET = re.compile(r"(?<!2)[1&*]?>>?(?![ \t]*&[12]\b)|\|[ \t]*(?:Out-Null|Out-File|Set-Content|Add-Content|Select(?:-Object)?|Select-String|"
                    r"tail|head|grep|sed|awk|wc|cut|sort|findstr)\b|[ \t](?:-q|--quiet)\b", re.I)
_FOREACH_PIPE = re.compile(r"\|\s*(?:ForEach-Object|%)\s*\{", re.I)
_LOOP_WORDS = re.compile(r"\"[^\"]*\"|'[^']*'|\S+")


def silenced(shell, pos):
    """True when the simple command at shell[pos] may not show its standard output: it is redirected or filtered
    (> /dev/null, > file, *> $null, | Out-Null, | Select …, | tail …, -q), or captured: a PowerShell assignment
    (`$r = $msg | python …`, `$null = …`, `[void]…`) or an unclosed $( … ) / `…` on its line (`out=$(cd /w && …)`).
    shell is the command with quotes masked."""
    line = shell[shell.rfind("\n", 0, pos) + 1:pos]
    end = shell.find("\n", pos)
    head = re.split(r";|&&|\|\|", line)[-1]
    tail = re.split(r";|&&|\|\|", shell[pos:end if end >= 0 else len(shell)])[0]  # this line only: linear in calls
    captured = line.count("$(") > line.count(")") or line.count("`") % 2 or \
        re.match(r"\s*(?:\{\s*)?(?:\[void\]|\$[\w:]+\s*=(?!=))", head, re.I)
    return bool(_QUIET.search(tail) or captured)


def _list_size(spec):
    """Items of a bash `for x in …` list: each word, a quoted string being one; a word whose size is unknown (a whole
    $( … ), `…`, a glob or a {a..b} brace) adds none, and the list counts at least once."""
    words = _LOOP_WORDS.findall(re.sub(r"\$\((?:[^()]|\([^()]*\))*\)", "$()", spec))
    return max(1, sum(1 for w in words if w[0] in "\"'" or not re.search(r"\$\(|`|[*?[{]", w)))


def helper_function_gate_runs(cmd):
    """Gate runs made by calling helper functions the command defines: (runs per gate; cmd with the definitions
    blanked; per gate, the names of the functions that run it; per gate, the runs whose verdict may not be shown).
    C3 Opus run 20261104T083537Z: `commit() { m=$1; …check-commit.py $m >/dev/null || { …; return 1; }; git commit
    …; }` then `commit c1.txt … || exit 1`. Both definition forms (bash `name() {…}`, bash or PowerShell
    `function name {…}`) count the same way: each gate a function runs (itself or through another function it calls)
    counts once per call, so a rerun in its failure branch is the same run; a call inside a literal `for x in a b c;
    do … done`, `foreach ($x in 'a','b') {…}` or `'a','b' | ForEach-Object {…}` list runs once per item. A function
    redefined keeps its last definition; one defined inside another is counted through that one's body. Definitions
    inside a heredoc are a script's text, so the literal count keeps them; comments are not code. A run's verdict
    may not be shown when the function sends the gate's output away (> /dev/null, | Out-Null, …), captures it,
    calls a function that does, or is called with its own output sent away."""
    code = blank_comments(cmd)
    shell = mask_quotes(mask_heredocs(code), multiline=True)  # a commit message line "commit the thing" is not a call
    defs = {}
    for m in _FUNC_DEF.finditer(shell):
        end = _block(shell, m.end() - 1)
        defs.setdefault(m.group("kw") or m.group("po"), []).append(
            (m.start("def"), end, code[m.end():end - 1], shell[m.end():end - 1]))
    rest = shell
    for spans in defs.values():
        for start, end, _, _ in spans:
            cmd = cmd[:start] + " " * (end - start) + cmd[end:]
            rest = rest[:start] + " " * (end - start) + rest[end:]

    def calls(name, text):
        """[(times, position)] of the calls of name in text (quote-masked); a call inside a literal loop list runs
        once per item."""
        pat = _CALL_PREFIX + r"(%s)(?![\w:.\\/()=-])" % re.escape(name)
        found, covered = [], 0
        for m in re.finditer(r"\bfor[ \t]+\w+[ \t]+in[ \t]+([^;\n]*?)[ \t]*(?:;|\n)\s*do\b(.*?)\bdone\b", text, re.S):
            n = _list_size(m.group(1))
            found += [(n, m.start(2) + c.start(1)) for c in re.finditer(pat, m.group(2), re.M)]
            text = text[:m.start()] + " " * (m.end() - m.start()) + text[m.end():]
        loops = [(m, m.group(1)) for m in re.finditer(
            r"\bforeach[ \t]*\([ \t]*\$\w+[ \t]+in[ \t]+((?:[^()]|\([^()]*\))*)\)\s*\{", text, re.I)]  # also @('a','b')
        if _FOREACH_PIPE.search(text):  # scan for the list only where a pipe into ForEach-Object exists (a long list is slow)
            loops += [(m, m.group(1)) for m in re.finditer(r"((?:['\"][^'\"\n]*['\"]\s*,\s*)*['\"][^'\"\n]*['\"])\s*\|\s*(?:ForEach-Object|%)\s*\{", text, re.I)]
        for m, spec in sorted(loops, key=lambda x: x[0].start()):
            if m.start() < covered:
                continue  # nested in a loop already counted
            covered = _block(text, m.end() - 1)
            n = len(_loop_items(spec))
            found += [(n, m.end() + c.start(1)) for c in re.finditer(pat, text[m.end():covered - 1], re.M)]
            text = text[:m.start()] + " " * (covered - m.start()) + text[covered:]
        return found + [(1, c.start(1)) for c in re.finditer(pat, text, re.M)]

    memo, quiet_memo = {}, {}

    def nested(other, name):  # `other` is defined inside `name`'s body: its calls there are in static_gate_runs(body)
        s, e = defs[name][-1][:2]
        return s < defs[other][-1][0] and defs[other][-1][1] <= e

    def fn_runs(name, seen):
        if name in memo or name in seen:
            return memo.get(name, collections.Counter())
        _, _, body, body_shell = defs[name][-1]
        total = collections.Counter({g: 1 for g, n in static_gate_runs(body).items() if n})
        for other in defs:
            k = sum(n for n, _ in calls(other, body_shell)) if other != name and not nested(other, name) else 0
            for g, n in (fn_runs(other, seen | {name}).items() if k else ()):
                total[g] += n * k
        memo[name] = total
        return total

    def fn_quiet(name, seen):
        """Gates whose verdict this function may not show."""
        if name in quiet_memo or name in seen:
            return quiet_memo.get(name, set())
        _, _, body, body_shell = defs[name][-1]
        quiet = {m.group(1) for m in GATE_FILE.finditer(body) if silenced(body_shell, m.start())}
        for other in defs:
            hits = calls(other, body_shell) if other != name else []
            if hits:
                inner = set(fn_runs(other, frozenset()))
                quiet |= inner & (fn_quiet(other, seen | {name}) | (inner if any(silenced(body_shell, p) for _, p in hits) else set()))
        quiet_memo[name] = quiet
        return quiet

    runs, quiet, gating = collections.Counter(), collections.Counter(), collections.defaultdict(set)
    for name in defs:
        per, hits, hush = fn_runs(name, frozenset()), calls(name, rest), fn_quiet(name, frozenset())
        for g, n in per.items():
            gating[g].add(name)
            runs[g] += n * sum(k for k, _ in hits)
            quiet[g] += n * sum(k for k, p in hits if g in hush or silenced(rest, p))
    return runs, cmd, dict(gating), quiet


_GIT_COMMIT = re.compile(r"\bgit\b[^\n;|&]*\bcommit\b")


def commits_only_in_helpers(cmd, gate="check-commit"):
    """True when cmd runs `gate` only inside helper functions it defines and makes every git commit inside functions
    that run the gate, so every commit the call made followed a gate run in a helper. For a helper that gates its
    commit (`check-commit.py $m >/dev/null || { …; return 1; }; git commit …`) that bounds the passes it hid by the
    commits made. A commit in a function that runs no gate (`wip() { git commit -qm wip; }`) is outside."""
    runs, blanked, gating, _ = helper_function_gate_runs(cmd)
    if not runs[gate] or static_gate_runs(blanked)[gate] or _GIT_COMMIT.search(mask_quotes(mask_heredocs(blank_comments(blanked)))):
        return False
    shell = mask_quotes(mask_heredocs(blank_comments(cmd)), multiline=True)
    return not any((m.group("kw") or m.group("po")) not in gating.get(gate, ()) and
                   _GIT_COMMIT.search(shell[m.end():_block(shell, m.end() - 1) - 1]) for m in _FUNC_DEF.finditer(shell))


def runs_gate(source, gate):
    """True when a script's source runs `gate`: Python `subprocess.run([...'…/validate-kanban.py'…])` /
    `os.system('python …/validate-kanban.py …')` (read with ast), or a shell line that runs it. A comment, a
    message, a list of names or an unrelated subprocess does not count."""
    script = re.compile(r"\b%s\.(?:py|ps1)\b" % re.escape(gate))
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        code = "\n".join(l for l in source.splitlines() if not l.lstrip().startswith("#"))
        return static_gate_runs(code)[gate] > 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        # names of calls that run a command in the *analysed* source (read only; this code never runs them)
        if name not in ("run", "call", "check_call", "check_output", "Popen", "system", "getoutput", "getstatusoutput"):
            continue
        first = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg in ("args", "command")), None)
        if isinstance(first, (ast.List, ast.Tuple)):
            parts = ["".join(c.value for c in ast.walk(e) if isinstance(c, ast.Constant) and isinstance(c.value, str))
                     + ("sys.executable" if any(isinstance(a, ast.Attribute) and a.attr == "executable" for a in ast.walk(e)) else "")
                     for e in first.elts]
            runner = parts and (re.fullmatch(r"(?:python3?(?:\.\d+)?|py|pwsh|powershell|sys\.executable)", parts[0].strip()) or script.search(parts[0]))
            if runner and any(script.search(p) for p in parts):
                return True
        elif isinstance(first, ast.Constant) and isinstance(first.value, str):
            if re.match(r"\s*(?:python3?(?:\.\d+)?|pwsh|bash|sh)\b[^\n]*" + script.pattern, first.value):
                return True
    return False


def heredoc_scripts(raw):
    """Scripts a shell call writes from a bash heredoc, `cat > $T/lanes.py <<'EOF' … EOF` or
    `cat <<'EOF' > $T/lanes.py`: [(lower-case file name, source)]."""
    out = []
    for start, body, term, _ in _heredoc_spans(raw):
        before = raw[raw.rfind("\n", 0, start) + 1:start]
        m = (re.search(r">\s*[\"']?[^\s\"';]*?([\w-]+\.(?:py|sh))[\"']?\s*$", before) or
             re.match(r"<<-?\s*[\"']?\w+[\"']?\s*>\s*[\"']?[^\s\"';]*?([\w-]+\.(?:py|sh))", raw[start:body]))
        if m:
            out.append((m.group(1).lower(), raw[body:max(body, term - 1)]))
    return out


# a here-string piped into a file; its body stops at its own closing line (it never runs on into a later here-string)
_PS_HERESTRING_SCRIPT = re.compile(r"@(['\"])\r?\n((?:(?!\r?\n\1@).)*?)\r?\n\1@\s*\|\s*(?:Set-Content|Out-File)\s+[^\n;|]*?([\w-]+\.(?:ps1|py))", re.S)


def gating_helper_scripts(raw):
    """Lane helpers a shell call writes whose own source runs validate-kanban, so each move they make runs that gate:
    a PowerShell here-string piped into Set-Content / Out-File (`@' … '@ | Set-Content kmove.ps1`), or a bash heredoc
    script that touches kanban.md (`cat > $T/lanes.py <<'EOF'`, C3 Opus run 20261104T083537Z). Shared by score-run
    (kanban_gating) and parity-eval (a.kanban_gate). Lower-case file names."""
    text = raw.replace("`r", "").replace("`n", "\n")
    names = {m.group(3).lower() for m in _PS_HERESTRING_SCRIPT.finditer(text) if "validate-kanban" in m.group(2)}
    names |= {n for n, src in heredoc_scripts(raw) if "kanban.md" in src and runs_gate(src, "validate-kanban")}
    return names


_LANES = r"Backlog|Ready|In Progress|In Review|Blocked|Done"
_NOT_A_CALL = re.compile(r"\s*(?:(?:do|then|else)\s+)?(?:\w+=\S*\s+)*(?:#|print\(|(?:echo|printf|cat|grep|rg|sed|less|more|head|tail|"
                         r"ls|git|Write-Host|Write-Output|Out-Host)\b)", re.I)


def helper_moves(raw, helpers):
    """Lane moves made by calling a kanban helper script with `<task> <lane>` arguments, e.g. `python $kb $f T2 Done`,
    bash `L=$T/lanes.py; python "$L" T5 Done` (also `${L}`, and inside `for t in T5 T6; do … $t Done …; done`), or
    `foreach($t in 'T4','T5'){ python $kb $f $t "In Progress" }`, or with named arguments, e.g.
    `pwsh -File kmove.ps1 -Task $t -To Done`. Only statements that invoke a helper (by file name or a variable
    holding its path) count: not a heredoc body written to a file (the helper's own source), a comment, an
    echo/printf/cat/grep/sed/head/tail/ls/git or print(/Write-Host/Write-Output command, or a --dry-run / -WhatIf.
    Shared by score-run (lane moves, and gate runs inside a helper that runs validate-kanban) and parity-eval
    (a.kanban_gate)."""
    text = raw.replace("`r", "").replace("`n", "\n")
    text = blank_comments(mask_written_heredocs(re.sub(r"\$\{(\w+)\}", r"$\1", text)))
    names = [re.escape(h) for h in helpers]
    if not names:
        return []
    named = r"(?<![\w.-])(?:%s)(?![\w.-])" % "|".join(names)
    hvars = set(re.findall(r"\$(\w+)\s*=\s*[\"'][^\"'\n]*(?:%s)" % "|".join(names), text, re.I))
    hvars |= set(re.findall(r"(?:^|[\s;&|(])(?:export\s+)?(\w+)=[\"']?[^\s\"';&|]*%s" % named, text, re.I))
    # bash functions that wrap the helper (C2 20261101T192141Z: `lane(){ python3 …/lane.py docs/f "$@"; }`, then
    # `lane T1 "In Progress"`): their calls are helper calls
    wraps = set()
    for m in _FUNC_DEF.finditer(text):
        body = text[m.end():_block(text, m.end() - 1) - 1]
        if re.search(named, body, re.I) or any(re.search(r"\$%s\b" % v, body) for v in hvars):
            wraps.add(m.group("kw") or m.group("po"))
    loops = [(m.start(), m.group(1), re.findall(r"\bT\d+\b", m.group(2)))
             for m in re.finditer(r"foreach\s*\(\s*\$(\w+)\s+in\s+([^)]*)\)", text, re.I)]
    loops += [(m.start(), m.group(1), re.findall(r"\bT\d+\b", m.group(2)))  # bash: `for t in T1 T2; do` (or do on the next line)
              for m in re.finditer(r"\bfor\s+(\w+)\s+in\s+([^;\n]*?)\s*(?:;|\n)\s*do\b", text)]
    loops.sort()
    moves = []
    for stmt in re.finditer(r"[^;\n{}&|]+", text):
        s = stmt.group(0)
        word = re.match(r"\s*(?:(?:do|then|else)\s+)?([A-Za-z_][\w-]*)(?![\w-]*[ \t]*\(\))", s)
        wrapped = bool(word) and word.group(1) in wraps
        if not (re.search(named, s, re.I) or any(re.search(r"\$%s\b" % v, s) for v in hvars) or wrapped):
            continue
        if _NOT_A_CALL.match(s) or "--dry-run" in s or re.search(r"(?i)-WhatIf\b", s):
            continue
        found = [(m.group(1), m.group(2), m.group(3))
                 for m in re.finditer(r"(?:\b(T\d+)(?:-[\w-]+)?\b|\$(\w+))['\"]?\s+['\"]?(%s)(?![\w-])" % _LANES, s)]
        task = re.search(r"-Task\s+['\"]?(?:\b(T\d+)\b|\$(\w+))", s, re.I)
        lane = re.search(r"-(?:To|Lane)\s+['\"]?(%s)\b" % _LANES, s, re.I)
        if task and lane:
            found = [(task.group(1), task.group(2), lane.group(1))]
        for tid, var, ln in found:
            if tid:
                ids = [tid]
            else:  # the loop variable's nearest enclosing foreach / for
                ids = next((i for p, v, i in reversed(loops) if v == var and p < stmt.start()), [])
            moves += [(t, ln) for t in ids]
    return moves


def static_gate_runs(cmd):
    """Gate executions implied by a shell command: literal calls, loops and helper functions."""
    runs, cmd, _, _ = helper_function_gate_runs(cmd)
    # Loops over literal lists: PowerShell foreach / ForEach-Object, bash for.
    loops = [(m, m.group(2), m.group(1)) for m in re.finditer(r"\bforeach\s*\(\s*\$(\w+)\s+in\s+([^)]*)\)\s*\{", cmd, re.I)]
    if _FOREACH_PIPE.search(cmd):  # scan for the list only where a pipe into ForEach-Object exists (a long list is slow)
        loops += [(m, m.group(1), "_") for m in re.finditer(r"((?:['\"][^'\"\n]*['\"]\s*,\s*)*['\"][^'\"\n]*['\"])\s*\|\s*(?:ForEach-Object|%)\s*\{", cmd, re.I)]
    for m, spec, var in sorted(loops, key=lambda x: x[0].start(), reverse=True):
        end = _block(cmd, m.end() - 1)
        body, items = cmd[m.end():end - 1], _loop_items(spec)
        if re.search(r"\$(?:\{%s\}|\(\$%s\)|%s)\.(?:py|ps1)\b" % ((re.escape(var),) * 3), body):
            runs.update(i for i in items if i in GATES)  # foreach($s in 'validate-kanban',...){ python "$p/$s.py" }
        for gate, n in _executed(body).items():
            runs[gate] += n * len(items)
        cmd = cmd[:m.start()] + " " * (end - m.start()) + cmd[end:]
    for m in reversed(list(re.finditer(r"\bfor\s+(\w+)\s+in\s+([^;\n]*?)\s*;?\s*do\b(.*?)\bdone\b", cmd, re.S))):
        items = [None] * _list_size(m.group(2))  # sized as helper loops are ("a b" is one item, $(ls) unknown)
        if re.search(r"\$\{?%s\}?\.(?:py|ps1)\b" % re.escape(m.group(1)), m.group(3)):
            runs.update(i for i in m.group(2).split() if i in GATES)
        for gate, n in _executed(m.group(3)).items():
            runs[gate] += n * len(items)
        cmd = cmd[:m.start()] + " " * (m.end() - m.start()) + cmd[m.end():]
    # Inline Python that runs gates as argument lists, e.g.
    # `for script,args in [('check-dod.py',['--task','T1']),('validate-kanban.py',[])]: subprocess.run(['python',root+script,...])`
    # (gpt-6.1-sol, C2 20261102T053248Z): each quoted gate-script literal is one run.
    if re.search(r"\bsubprocess\.(?:run|call|check_call|check_output|Popen)\(|\bos\.system\(", cmd):
        for gate in GATES:
            runs[gate] += len(re.findall(r"['\"][^'\"\n]*?\b%s\.py['\"]" % re.escape(gate), cmd))
    runs.update(_executed(cmd))
    return runs


def gate_outcomes(name, inp, result, commits=None):
    """[(gate, passed)] for one tool call; passed is None when the run happened but its verdict is hidden. commits,
    when known, is the number of commits the call made (score-run maps commits to calls)."""
    text, is_error = result if result else ("", False)
    if name and name.startswith(MCP_PREFIXES):
        gate = name.split("__")[-1].removeprefix("geeky_").replace("_", "-")
        m = re.search(r'"ok"\s*:\s*(true|false)', text)
        return [(gate, None if is_error or not m else m.group(1) == "true")]
    cmd = inp.get("command", "") if isinstance(inp, dict) else ""
    if not isinstance(cmd, str) or not cmd:
        return []
    static = static_gate_runs(cmd)
    fn, _, _, quiet = helper_function_gate_runs(cmd)
    out = []
    for gate in GATES:
        if not static[gate]:
            continue  # only results of gates this command executes (not a cat of handoff.md, not a read of the script)
        seen = gate_verdicts(text, gate)
        # A helper function that prints its gate's verdict shows every run it makes: once any verdict is shown, its
        # runs are among the shown ones, and the rest of its static count (a call after a stop, the right side of
        # `a || b`, an `if` not taken) never ran. Literal calls and helpers that send the output away may be hidden.
        loud = fn[gate] - quiet[gate] if seen else 0
        hidden = max(0, static[gate] - max(len(seen), loud))
        # A shown commit-gate failure from helpers that gate every commit of the call: which calls ran is not in the
        # output, but each passing one made a commit, so the hidden runs are the call's commits less the shown passes
        # (C3 Opus 20261104T083537Z: the first of four `commit … || exit 1` failed and nothing was committed).
        if gate == "check-commit" and commits is not None and False in seen and commits_only_in_helpers(cmd):
            hidden = min(hidden, max(0, commits - sum(seen)))
        out += [(gate, p) for p in seen] + [(gate, None)] * hidden
    return out


def gate_verdicts(text, gate):
    """The verdicts of `gate` shown in a tool result, in order (True for a pass): one summary line per run, also
    after a prefix ("c1.txt: OK: commit message valid."). A source line that prints the verdict text (print(,
    Write-Host, f") is not a run. Shared by score-run (gate_outcomes) and parity-eval (commit_gate_runs)."""
    return [m.group(1).startswith(("OK", "DOD_OK")) for line in text.splitlines() if not CODE_LINE.search(line)
            for m in [GATE_LINES[gate].search(line)] if m]


def thread_split(flat):
    """Tool calls, dispatches and gate runs split by thread: main (no parent), orchestrator (made by a
    geeky-implement-orchestrator subagent) and workers (any other subagent). flat: [(id, name, input, parent, result)]."""
    orch = {cid for cid, name, inp, _, _ in flat if name in ("Agent", "Task") and is_orchestrator_type(inp.get("subagent_type"))}
    out = {k: {"tool_calls": collections.Counter(), "dispatches": collections.Counter(), "gates": collections.Counter()}
           for k in ("main", "orchestrator", "workers")}
    for cid, name, inp, parent, res in flat:
        t = out["main" if not parent else "orchestrator" if parent in orch else "workers"]
        t["tool_calls"][name] += 1
        if name in ("Agent", "Task"):
            t["dispatches"][str(inp.get("subagent_type") or "general-purpose")] += 1
        for gate, _ in gate_outcomes(name, inp, res):
            t["gates"][gate] += 1
    return {k: {kk: dict(vv.most_common()) for kk, vv in v.items()} for k, v in out.items()}


def harness_name(run_dir, override=None):
    if override:
        return override
    path = Path(run_dir) / 'run.json'
    return json.loads(path.read_text(encoding='utf-8-sig')).get('harness', 'claude-code') if path.exists() else 'claude-code'


def trace_adapter():
    spec = importlib.util.spec_from_file_location('codex_trace', Path(__file__).with_name('codex-trace.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def codex_trace(run_dir, process_path=None):
    return trace_adapter().load(run_dir, process_path)


def analyze(run_dir: Path, harness=None, process_path=None):
    codex = codex_trace(run_dir, process_path) if harness_name(run_dir, harness) == 'codex' else None
    stream = codex['stream'] if codex is not None else load_jsonl(run_dir / "stream.jsonl")
    session = codex['session'] if codex is not None else load_jsonl(run_dir / "session.jsonl")
    result = next((e for e in reversed(stream) if e.get("type") == "result"), {})

    # Tokens: modelUsage covers every model and every turn; result.usage is the last turn only.
    usage = result.get("modelUsage") or {}
    fresh = sum(u.get("inputTokens", 0) for u in usage.values())
    cache_read = sum(u.get("cacheReadInputTokens", 0) for u in usage.values())
    cache_write = sum(u.get("cacheCreationInputTokens", 0) for u in usage.values())
    output = sum(u.get("outputTokens", 0) for u in usage.values())
    tokens = {
        "input": fresh + cache_read + cache_write,
        "cached_input": cache_read,
        "cache_creation": cache_write,
        "output": output,
        "total": fresh + cache_read + cache_write + output,
        "by_model": {m: {k: u.get(k, 0) for k in ("inputTokens", "cacheReadInputTokens",
                                                  "cacheCreationInputTokens", "outputTokens")}
                     for m, u in usage.items()},
        "cost_usd_estimate": round(sum(u.get("costUSD", 0) for u in usage.values()), 4),
    }
    if not usage:  # no final result (timeout/crash) or no accounting: usage is unknown, not zero
        tokens = {k: None for k in tokens}

    # Tool calls, subagents, skills and validations from the stream
    main_tools, sub_tools, subagents, skills = (collections.Counter() for _ in range(4))
    validations = collections.Counter()
    results = {}
    for e in stream:
        if e.get("type") == "user":
            results.update(tool_results(e))
    gates = collections.defaultdict(lambda: dict.fromkeys(("pass", "fail", "unknown", "main", "sub"), 0))
    # (id, name, input, parent, result) for every call: the stream's, then calls only in subagent transcripts
    flat = [(c.get("id"), c.get("name"), c.get("input") or {}, e.get("parent_tool_use_id"), results.get(c.get("id")))
            for e in stream if e.get("type") == "assistant" for c in tool_uses(e)]
    extra, metas = ([], codex['metas']) if codex is not None else transcript_calls(run_dir, {f[0] for f in flat})
    flat += [(c["id"], c["name"], c["input"], c["parent"], c["result"]) for c in extra]
    for cid, name, inp, parent, res in flat:
        sub = bool(parent)
        (sub_tools if sub else main_tools)[name] += 1
        if name in ("Agent", "Task"):
            subagents[inp.get("subagent_type") or "general-purpose"] += 1
        if name == "Skill":
            skills[str(inp.get("skill", "")).split(":")[-1]] += 1
        cmd = inp.get("command", "") if isinstance(inp, dict) else ""
        if isinstance(cmd, str) and re.search(r"pytest|unittest", cmd):
            validations["sub" if sub else "main"] += 1
        for gate, passed in gate_outcomes(name, inp, res):
            g = gates[gate]
            g[{True: "pass", False: "fail", None: "unknown"}[passed]] += 1
            g["sub" if sub else "main"] += 1
    threads = thread_split(flat) if metas else None

    # Stages: Skill invocations on the main thread, timed from the session file
    stamps = [t for t in (ts(e) for e in session) if t]
    end = max(stamps) if stamps else None
    starts = [(str(c["input"].get("skill", "")).split(":")[-1], ts(e))
              for e in session if e.get("type") == "assistant" and not e.get("isSidechain")
              for c in tool_uses(e) if c.get("name") == "Skill" and ts(e)]
    stages = []
    for i, (name, start) in enumerate(starts):
        stop = starts[i + 1][1] if i + 1 < len(starts) else end
        stages.append({"name": name, "started_utc": start.isoformat(), "ended_utc": stop.isoformat(),
                       "minutes": round((stop - start).total_seconds() / 60, 1)})

    # Latency: main-thread user event -> next assistant event; idle: largest gap between events
    latencies, gaps, pending, prev = [], [], None, None
    for e in session:
        t = ts(e)
        if not t:
            continue
        if prev:
            gaps.append((t - prev).total_seconds())
        prev = t
        if e.get("isSidechain"):
            continue
        if e.get("type") == "user":
            pending = t
        elif e.get("type") == "assistant" and pending:
            latencies.append((t - pending).total_seconds() * 1000)
            pending = None

    model_calls = len({(e.get("message") or {}).get("id") for e in session
                       if e.get("type") == "assistant" and (e.get("message") or {}).get("id")})

    out = {
        "tokens": tokens,
        "model_calls": model_calls or None,
        "latency_median_ms": round(statistics.median(latencies)) if latencies else None,
        "latency_samples": len(latencies),
        "max_idle_gap_s": round(max(gaps)) if gaps else None,
        "stages": stages or None,
        "impl_review_rounds": skills.get("impl-review") or None,
        "skill_invocations": dict(skills),
        "tool_calls_main": dict(main_tools.most_common()),
        "tool_calls_subagents": dict(sub_tools.most_common()),
        "subagent_dispatches": dict(subagents),
        "validation_runs": dict(validations),
        "gate_invocations": {k: v["main"] + v["sub"] for k, v in gates.items()},
        "gate_results": dict(gates),
        "duration_api_ms": result.get("duration_api_ms"),
    }
    if metas:  # subagent transcripts present: the counts above include their calls
        out["subagent_transcripts"] = {"transcripts": len(metas), "calls_only_in_transcripts": len(extra),
                                       "by_type": dict(collections.Counter(str(m.get("agentType")) for m in metas.values())),
                                       "threads": threads}
    if codex is not None:
        out.update(tokens=codex['tokens'] or {k: None for k in
                   ('input', 'cached_input', 'cache_creation', 'output', 'reasoning', 'total', 'by_model', 'cost_usd_estimate')},
                   model_calls=codex['model_calls'], latency_median_ms=None, latency_samples=0,
                   max_idle_gap_s=codex['max_idle_gap_s'], trace_complete=codex['trace_complete'],
                   trace_errors=codex['trace_errors'], trace_warnings=codex['trace_warnings'],
                   harness='codex', usage_by_thread=codex['usage_by_thread'],
                   skill_evidence='successful explicit SKILL.md reads; no workflow completion inferred')
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--token-ceiling", type=int)
    ap.add_argument("--analysis-only", action="store_true", help="write analysis.json only; leave run.json as stored")
    ap.add_argument('--harness', choices=('claude-code', 'codex'))
    ap.add_argument('--out', type=Path, help='write derived files here, preserving the input run folder')
    ap.add_argument('--process-evidence', type=Path)
    args = ap.parse_args()

    analysis = analyze(args.run_dir, harness=args.harness, process_path=args.process_evidence)
    output = args.out or args.run_dir
    output.mkdir(parents=True, exist_ok=True)
    (output / "analysis.json").write_text(json.dumps(analysis, indent=2), encoding="utf-8")
    if args.analysis_only:
        print(json.dumps({k: analysis[k] for k in ("gate_invocations", "gate_results")}, indent=2))
        return

    run_path = args.run_dir / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8-sig"))
    tokens = {k: v for k, v in analysis["tokens"].items() if k != "by_model"}
    tokens["reasoning"] = analysis['tokens'].get('reasoning') if analysis.get('harness') == 'codex' else (run.get("tokens") or {}).get("reasoning")
    run["tokens"] = tokens
    for key in ("model_calls", "latency_median_ms", "max_idle_gap_s", "stages", "impl_review_rounds"):
        run[key] = analysis[key]
    if args.token_ceiling:
        run["token_ceiling"] = args.token_ceiling
        run["over_token_ceiling"] = tokens["total"] > args.token_ceiling if tokens['total'] is not None else None
    if analysis.get('harness') == 'codex':
        run['analysis_status'] = 'complete' if analysis['trace_complete'] else 'incomplete'
        run['trace_complete'] = analysis['trace_complete']
        run['trace_errors'] = analysis['trace_errors']
        run['token_ceiling_policy'] = 'informational-until-codex-baseline'
        if not analysis['trace_complete']:
            run['reference_eligible'] = False
    (output / 'run.json').write_text(json.dumps(run, indent=2), encoding="utf-8")
    print(json.dumps({k: run.get(k) for k in ("tokens", "model_calls", "latency_median_ms",
                                              "max_idle_gap_s", "impl_review_rounds",
                                              "token_ceiling", "over_token_ceiling")}, indent=2))


if __name__ == "__main__":
    main()
