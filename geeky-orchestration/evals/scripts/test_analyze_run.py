"""Claude analysis keeps unknown token usage distinct from zero usage."""
import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location('analyze_run', Path(__file__).with_name('analyze-run.py'))
AR = importlib.util.module_from_spec(spec)
spec.loader.exec_module(AR)


def run_dir(tmp_path, stream):
    (tmp_path / 'stream.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in stream), encoding='utf-8')
    (tmp_path / 'session.jsonl').write_text('', encoding='utf-8')
    return tmp_path


def test_timeout_without_result_reports_unknown_tokens(tmp_path):
    tokens = AR.analyze(run_dir(tmp_path, [{'type': 'system', 'subtype': 'init'}]), harness='claude-code')['tokens']
    assert tokens and all(v is None for v in tokens.values())


def test_result_model_usage_is_summed(tmp_path):
    usage = {'m': {'inputTokens': 10, 'cacheReadInputTokens': 5, 'cacheCreationInputTokens': 2,
                   'outputTokens': 3, 'costUSD': 0.5}}
    tokens = AR.analyze(run_dir(tmp_path, [{'type': 'result', 'modelUsage': usage}]), harness='claude-code')['tokens']
    assert tokens['total'] == 20 and tokens['cached_input'] == 5 and tokens['cost_usd_estimate'] == 0.5


def test_bash_commit_function_review_cases():
    # scorer-fix review, 2026-11-05 (C3 Opus run 20261104T083537Z shapes)
    fn = 'c(){ python3 $P/scripts/check-commit.py --message "$m"; git commit -m "$m"; }\n'
    assert AR.static_gate_runs(fn + 'for m in a b; do c "$m"; done')['check-commit'] == 2      # once per listed item
    assert AR.static_gate_runs(fn + 'if true; then c a; fi')['check-commit'] == 1              # a call after `then`
    assert AR.static_gate_runs(fn)['check-commit'] == 0                                        # defined, never called
    script = "cat > x.sh <<'EOF'\n" + fn + "c a\nEOF\nbash x.sh"
    assert AR.static_gate_runs(script)['check-commit'] == 1      # a function in a heredoc is a script's text: literal count
    # C3 Opus 09:24:19: the first call's gate failed and the call made no commit, so no hidden pass ran
    chain = ("commit() { m=$1; shift\n python $P/scripts/check-commit.py $m >/dev/null || { python $P/scripts/check-commit.py $m; return 1; }\n"
             " git commit -q -F $m; }\ncommit c1.txt a || exit 1\ncommit c2.txt b || exit 1\ncommit c3.txt c")
    assert AR.static_gate_runs(chain)['check-commit'] == 3
    fail = ("GATE FAIL c1.txt\nINVALID_COMMIT_MESSAGE: subject exceeds 72 chars (80)", True)
    assert AR.gate_outcomes('Bash', {'command': chain}, fail, 0) == [('check-commit', False)]


def test_runs_gate_needs_an_invocation():
    assert AR.runs_gate("import subprocess\nsubprocess.run(['python','/p/scripts/validate-kanban.py','--path','f'])", 'validate-kanban')
    assert not AR.runs_gate("# validate-kanban is run by the caller\nk=open('kanban.md','w')", 'validate-kanban')
    assert not AR.runs_gate("print('remember to run validate-kanban')", 'validate-kanban')


def test_bash_function_gate_runs_review_round_two():
    inner = 'inner() { python $P/scripts/check-commit.py $1; }\nouter() { inner $1 || return 1; git commit -q -F $1; }\n'
    assert AR.static_gate_runs(inner + 'outer a; outer b')['check-commit'] == 2            # nested helpers
    fn = 'commit() { python $P/scripts/check-commit.py "$1"; git commit -F "$1"; }\n'
    assert AR.static_gate_runs(fn + 'if commit a; then echo ok; fi\nwhile commit b; do :; done\ntime commit c')['check-commit'] == 3
    assert AR.static_gate_runs(fn + fn + 'commit a; commit b')['check-commit'] == 2         # redefined: counted once
    # a shown failure of a helper that hides its passes: the call's commits bound the hidden passes
    own = ('commit() { python $P/scripts/check-commit.py $1 >/dev/null || { python $P/scripts/check-commit.py $1; return 1; }\n'
           ' git commit -q -F $1; }\n')
    out = ("INVALID_COMMIT_MESSAGE: x", True)
    three = own + 'commit c1\ncommit c2\ncommit c3'
    assert AR.gate_outcomes('Bash', {'command': three}, out) == \
        [('check-commit', False), ('check-commit', None), ('check-commit', None)]       # commits not known: the calls count
    assert AR.gate_outcomes('Bash', {'command': three}, out, 2) == \
        [('check-commit', False), ('check-commit', None), ('check-commit', None)]
    assert AR.gate_outcomes('Bash', {'command': three}, out, 1) == [('check-commit', False), ('check-commit', None)]
    for stop in (own + 'commit c1 || exit 1\ncommit c2 || exit 1', 'set -e\n' + own + 'commit c1\ncommit c2',
                 'set -x -e\n' + own + 'commit c1\ncommit c2', own.replace('return 1', 'exit 1') + 'commit c1\ncommit c2'):
        assert AR.gate_outcomes('Bash', {'command': stop}, out, 0) == [('check-commit', False)], stop
    # c1 and c2 passed silently and committed, c3's failure is shown (round-7 review): 3 runs
    assert len(AR.gate_outcomes('Bash', {'command': own + 'commit c1 || exit 1\ncommit c2 || exit 1\ncommit c3 || exit 1'}, out, 2)) == 3
    # a commit outside the helpers: the commits are no evidence of helper passes, the calls count
    assert len(AR.gate_outcomes('Bash', {'command': own + 'git commit -m "x"\ncommit c1\ncommit c2'}, out, 0)) == 2


def test_runs_gate_round_two():
    assert not AR.runs_gate("GATES=['validate-kanban.py']\nimport subprocess\nsubprocess.run(['git','add','kanban.md'])", 'validate-kanban')
    assert not AR.runs_gate("import subprocess\nsubprocess.run(['git','diff','--','validate-kanban.py'])", 'validate-kanban')
    assert AR.runs_gate("import os\nos.system('python /p/scripts/validate-kanban.py --path x')", 'validate-kanban')
    assert AR.runs_gate("import subprocess,sys\nsubprocess.run([sys.executable, P+'/scripts/validate-kanban.py'])", 'validate-kanban')


def test_mask_quotes_is_linear():
    import time
    t0 = time.time()
    AR.mask_quotes('"\\' * 40000)
    assert time.time() - t0 < 2
    assert AR.mask_quotes('a "b c" \'d e\' "f\\"g"') == 'a "   " \'   \' "    "'


def test_commit_message_lines_are_not_function_calls():
    fn = 'commit(){ python check-commit.py $1; }\n'
    assert AR.static_gate_runs(fn + 'git commit -m "title\n\ncommit the thing\ncommit more"\ncommit a')['check-commit'] == 1
    assert AR.static_gate_runs(fn + "git commit -m 'title\n\ncommit the thing'\ncommit a")['check-commit'] == 1
    assert AR.static_gate_runs(fn + 'echo "calling\ncommit a\nnow"\ncommit b')['check-commit'] == 1


def test_command_substitution_inside_double_quotes_is_a_call():
    # round-5 review: res="$(c 'feat: add x')" runs the helper
    cmd = 'c(){ python3 $P/scripts/check-commit.py --message "$1" && git commit -q -m "$1"; }\nres="$(c \'feat: add x\')"; echo "$res"'
    assert AR.static_gate_runs(cmd)['check-commit'] == 1
    assert AR.gate_outcomes('Bash', {'command': cmd}, ('OK: commit message valid.', False)) == [('check-commit', True)]
    assert AR.mask_quotes('a "x $(c y) `d z` w" b') == 'a "  $(c y) `d z`  " b'


def test_function_keyword_helpers_count_like_bash_functions():
    # parity-path review, 2026-11-05: `function name {…}` went through an older count (a failure-branch rerun counted twice)
    kw = ('function commit { python $P/scripts/check-commit.py $1 >/dev/null || { python $P/scripts/check-commit.py $1; return 1; }; '
          'git commit -F $1; }\n')
    calls = 'commit c1 || exit 1\ncommit c2 || exit 1\ncommit c3'
    assert AR.static_gate_runs(kw + calls)['check-commit'] == 3
    assert AR.gate_outcomes('Bash', {'command': kw + calls}, ('INVALID_COMMIT_MESSAGE: x', True), 0) == [('check-commit', False)]
    assert AR.static_gate_runs(kw + 'git commit -m "title\n\ncommit the thing\ncommit more"\ncommit a')['check-commit'] == 1
    assert AR.static_gate_runs(kw + 'for m in a b c; do commit $m; done')['check-commit'] == 3
    assert AR.static_gate_runs(kw + 'if commit a; then echo ok; fi')['check-commit'] == 1
    ps = ("function Commit-Msg($f) { python $P\\scripts\\check-commit.py $f | Out-Null; git commit -F $f }\n"
          "foreach ($m in 'c1.txt','c2.txt','c3.txt') { Commit-Msg $m }")
    runs, _, gating, quiet = AR.helper_function_gate_runs(ps)
    assert runs['check-commit'] == 3 and quiet['check-commit'] == 3 and gating['check-commit'] == {'Commit-Msg'}


def test_a_helper_that_prints_its_verdict_is_counted_by_the_shown_verdicts():
    # parity-path review, 2026-11-05: whatever stopped the command, the shown verdicts are the runs
    c = 'c(){ python3 $P/scripts/check-commit.py --message "$1" || return 1; git commit -q -m "$1"; }\n'
    fail = ('INVALID_COMMIT_MESSAGE: subject exceeds 72 chars', True)
    for calls in ('c "feat: a" && c "feat: b" && c "feat: c"', 'cd /w; set -e\nc a\nc b\nc c', 'cd /w && set -euo pipefail\nc a\nc b',
                  'set -o errexit\nc a\nc b', 'if ! c a; then exit 1; fi\nif ! c b; then exit 1; fi',
                  'for m in a b c; do c "feat: $m" || break; done'):
        assert AR.gate_outcomes('Bash', {'command': c + calls}, fail) == [('check-commit', False)], calls
    assert AR.gate_outcomes('Bash', {'command': c + 'c "feat: a" || c "feat: b"'}, ('OK: commit message valid.', False)) == \
        [('check-commit', True)]
    assert len(AR.gate_outcomes('Bash', {'command': c + 'c a\nc b'}, ('', False))) == 2     # no verdict shown: the calls count
    # a helper that hides its passes, after a shown failure: one pass per commit, whatever the separators
    own = ('commit() { python $P/scripts/check-commit.py $1 >/dev/null || { python $P/scripts/check-commit.py $1; return 1; }\n'
           ' git commit -q -F $1; }\n')
    assert AR.gate_outcomes('Bash', {'command': own + 'commit c1 && commit c2 && commit c3'}, fail, 0) == [('check-commit', False)]
    assert len(AR.gate_outcomes('Bash', {'command': own + 'commit c1 && echo a\ncommit c2 && echo b\ncommit c3'}, fail, 2)) == 3
    # captured or redirected verdicts are quiet too (stored C1 csvledger 20261020T155628Z: `$r=$msg | python …check-commit.py`)
    for body in ('$r=$msg | python $P/scripts/check-commit.py; if($LASTEXITCODE -ne 0){"GATE FAIL $r"; return}',
                 'python3 $P/scripts/check-commit.py --message "$1" > "$T/cc.out" 2>&1 || { cat "$T/cc.out"; return 1; }',
                 'out=$(cd /w && python3 $P/scripts/check-commit.py --message "$1") || { echo "$out"; return 1; }'):
        _, _, _, q = AR.helper_function_gate_runs('function C($msg) { %s; git commit -q -m $msg }\nC a\nC b' % body)
        assert q['check-commit'] == 2, body
    # a function's output sent away at the call is hidden too
    _, _, _, quiet = AR.helper_function_gate_runs(c + 'c a >/dev/null\nc b')
    assert quiet['check-commit'] == 1


def test_gate_verdicts_read_prefixed_lines_not_source_lines():
    out = 'c1.txt: OK: commit message valid.\nc2.txt: INVALID_COMMIT_MESSAGE: x\n111: print("OK: commit message valid.")'
    assert AR.gate_verdicts(out, 'check-commit') == [True, False]


def test_comments_in_a_function_body_are_not_gate_runs():
    fn = 'c(){ # python $P/scripts/check-commit.py runs later\n git commit -m x; }\nc; c'
    assert AR.static_gate_runs(fn)['check-commit'] == 0


def test_heredoc_masking_is_linear_and_unchanged():
    import time
    t0 = time.time()
    AR.mask_heredocs('cat <<EOF\nx\n' * 20000)            # every `<<EOF` unterminated
    AR.mask_written_heredocs('a <<W\n' * 20000)
    assert time.time() - t0 < 2
    text = "cat > x.py <<'EOF'\nprint(1)\n  EOF\nEOF\necho done <<-X\n\tbody\nX\nls"
    assert AR.mask_heredocs(text) == "cat > x.py <<'EOF'\n        \n  EOF\nEOF\necho done <<-X\n     \nX\nls"  # an indented delimiter ends it
    assert AR.heredoc_scripts("cat > $T/a.py <<'EOF'\nimport os\nEOF\ncat <<EOF > b.sh\necho b\nEOF") == \
        [('a.py', 'import os'), ('b.sh', 'echo b')]


def test_round_seven_low_findings():
    # nested helper definitions count once; prefixes else / elif / until / command / backtick / PowerShell `$r =`
    assert AR.static_gate_runs('outer() { inner() { python check-commit.py $1; }; inner $1; }\nouter a')['check-commit'] == 1
    c = 'c() { python check-commit.py $1; git commit -F $1; }\n'
    for call in ('if false; then :; else c a; fi', 'if false; then :; elif c a; then :; fi', 'until c a; do :; done',
                 'command c a', 'r=`c a`'):
        assert AR.static_gate_runs(c + call)['check-commit'] == 1, call
    ps = 'function Commit-Msg($m) { python check-commit.py $m; git commit -F $m }\n'
    assert AR.static_gate_runs(ps + '$r = Commit-Msg a')['check-commit'] == 1
    assert AR.static_gate_runs(ps + "'a','b','c' | ForEach-Object { Commit-Msg $_ }")['check-commit'] == 3
    assert AR.static_gate_runs(ps + "foreach ($m in @('a','b')) { Commit-Msg $m }")['check-commit'] == 2
    # loop lists: quoted items are one each, unknown-size words add none
    assert AR.static_gate_runs(c + 'for m in "a b" c; do c $m; done')['check-commit'] == 2
    assert AR.static_gate_runs(c + 'for m in $(ls *.txt); do c $m; done')['check-commit'] == 1
    assert AR.static_gate_runs(c + 'for m in a *.txt; do c $m; done')['check-commit'] == 1
    # a literal run whose result is assigned
    assert AR.static_gate_runs('out=$(python3 $P/scripts/check-commit.py --message x)')['check-commit'] == 1
    # quiet forms: -q / --quiet, capture, &> and *> $null; and a function calling a quiet one is quiet
    for body in ('python check-commit.py $1 --quiet', 'r=$(python check-commit.py $1)', 'python check-commit.py $1 &>/dev/null',
                 'python check-commit.py $1 *> $null'):
        _, _, _, q = AR.helper_function_gate_runs('c() { %s; git commit -F $1; }\nc a' % body)
        assert q['check-commit'] == 1, body
    _, _, _, q = AR.helper_function_gate_runs('i() { python check-commit.py $1 >/dev/null; }\no() { i $1; git commit -F $1; }\no a')
    assert q['check-commit'] == 1
    _, _, _, q = AR.helper_function_gate_runs(c + 'c a')
    assert q['check-commit'] == 0


def test_round_seven_heredoc_and_source_masks():
    assert AR.mask_heredocs('echo "x <<EOF"\nfoo\nEOF') == 'echo "x <<EOF"\nfoo\nEOF'           # in quotes: no heredoc
    assert AR.mask_heredocs('# see <<EOF\nfoo\nEOF') == '# see <<EOF\nfoo\nEOF'                 # in a comment
    nested = "cat > a.sh <<'EOF'\ncat <<X\nhi\nX\nEOF\nls"
    assert AR.mask_heredocs(nested) == "cat > a.sh <<'EOF'\n       \n  \n \nEOF\nls"
    piped = "cat <<'EOF' | python3 -\nprint(1)\nEOF"
    assert AR.mask_written_heredocs(piped) == piped                                           # fed to an interpreter
    here = "@'\npython /p/scripts/validate-kanban.py\n'@ | Set-Content k.ps1\npwsh -File k.ps1"
    assert 'validate-kanban' not in AR.mask_written_sources(here) and len(AR.mask_written_sources(here)) == len(here)


def test_runs_gate_and_gating_helpers_need_the_right_shape():
    assert not AR.runs_gate("foo(['python','/p/scripts/validate-kanban.py'])", 'validate-kanban')      # not a run call
    src = "cat > $T/v.py <<'EOF'\nimport subprocess\nsubprocess.run(['python','/p/scripts/validate-kanban.py'])\nEOF\n"
    assert AR.gating_helper_scripts(src) == set()                                                     # no kanban.md
    assert AR.gating_helper_scripts(src.replace("import subprocess", "import subprocess\nk=open('kanban.md').read()")) == {'v.py'}


def test_silenced_is_linear_in_calls():
    import time
    c = 'c() { python check-commit.py $1; git commit -F $1; }\n'
    t0 = time.time()
    AR.helper_function_gate_runs(c + 'c a\n' * 3000)
    assert time.time() - t0 < 5


def test_round_eight_low_findings():
    # captured literal runs after local / export / if !
    for body in ('local out=$(python3 /p/scripts/check-commit.py --message "$1") || { echo "$out"; return 1; }',
                 'export out=$(python3 /p/scripts/check-commit.py --message "$1")',
                 'if ! out=$(python3 /p/scripts/check-commit.py --message "$1"); then echo "$out"; return 1; fi'):
        assert AR.static_gate_runs('c() { %s; git commit -q -m "$1"; }\nc a\nc b\nc c' % body)['check-commit'] == 3, body
    # literal bash for-lists: a quoted message is one item
    loop = ('for m in "feat(a): add x" "feat(b): add y"; do python3 /p/scripts/check-commit.py --message "$m" && '
            'git commit -q -m "$m"; done')
    assert AR.static_gate_runs(loop)['check-commit'] == 2
    assert AR.static_gate_runs('for m in $(cat list | head); do python3 /p/scripts/check-commit.py --message "$m"; done')['check-commit'] == 1
    # a heredoc after a multi-line quoted string is still a heredoc
    src = ("git commit -q -m \"docs: notes\n\nit's done\" && cat > $T/lanes.py <<'EOF'\nimport subprocess\nk=open('docs/f/kanban.md').read()\n"
           "subprocess.run(['python','/p/scripts/validate-kanban.py','--path','docs/f'])\nEOF\npython $T/lanes.py T1 Done")
    assert [n for n, _ in AR.heredoc_scripts(src)] == ['lanes.py'] and AR.gating_helper_scripts(src) == {'lanes.py'}
    # a here-string that is not piped does not run on into a later piped one
    two = ("$m = @'\nfeat: x\n'@\npython /p/scripts/validate-kanban.py --path docs/f\n@'\nparam($t)\n'@ | Set-Content $env:TEMP\\note.ps1")
    assert 'validate-kanban' in AR.mask_written_sources(two)
    # stderr is in the tool result: >&2 does not hide a verdict
    _, _, _, q = AR.helper_function_gate_runs('c() { python3 /p/scripts/check-commit.py --message "$1" >&2; git commit -q -m "$1"; }\nc a')
    assert q['check-commit'] == 0


def test_long_powershell_list_without_a_pipe_is_fast():
    import time
    t0 = time.time()
    cmd = 'function C($m) { python check-commit.py $m }\n$list = ' + ','.join("'m%d'" % i for i in range(6000)) + '\nC a'
    AR.helper_function_gate_runs(cmd)
    AR.static_gate_runs(cmd)
    assert time.time() - t0 < 3
