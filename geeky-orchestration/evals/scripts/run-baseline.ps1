<#
.SYNOPSIS
  Run one O14 baseline: Claude Code in an isolated MXC sandbox, driven by the canonical
  end-to-end prompt, then oracle checks and a stored baseline record.

.DESCRIPTION
  Implements evals/baseline-runs.md:
    1. fresh seed workspace (git init, brief.md, contract.md, seed commit, no remote)
    2. oracle against the empty workspace (every test must fail)
    3. headless claude in MXC (--isolated --allow-win32k --host-loopback), stream captured
    4. oracle 3x against the result (flake check)
    5. stores solution.bundle, stream/session/stderr, oracle output and run.json under
       evals/baselines/<case>/<UTC stamp>/
  Fields that need the ATIF ingest tool (latency, idle gaps, stages, tasks) are left null.

  Requires: CLAUDE_CODE_OAUTH_TOKEN (process or User env), the mxc-sandbox plugin, Python with
  pytest on PATH, and <ToolsDir>/geeky-mcp (prebuilt venv for geeky_mcp, see D75). Runs on Windows
  (ProcessContainer) and on Linux/macOS (Bubblewrap/Seatbelt); ToolsDir and WorkRoot default to
  C:\repos\.eval-tools and C:\repos on Windows, ~/repos/.eval-tools and ~/repos elsewhere.

  Telemetry: with TRACE_TO_LANGFUSE=true on the host (and LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY,
  LANGFUSE_BASE_URL), the pinned Langfuse Stop/SessionEnd hook uploads every turn, subagent, tool
  call and thinking summary untruncated (evals/telemetry/README.md). -NoTelemetry turns it off.

.EXAMPLE
  pwsh -File run-baseline.ps1 -Case c1-csvledger -DryRun
  pwsh -File run-baseline.ps1 -Case c1-csvledger
  pwsh -File run-baseline.ps1 -Case c3-minisql -Model claude-opus-5-5
#>
param(
    [Parameter(Mandatory)][ValidateSet('c0-wordstat', 'c1-csvledger', 'c2-linkvault', 'c3-minisql')][string]$Case,
    [string]$Model,
    [int]$TimeoutSec,
    [int]$CloseoutReserveSec = 0,
    [string]$AuthorizationReference,
    [string]$ToolsDir,
    [string]$WorkRoot,
    [switch]$DryRun,
    # Materialize provider/model/MCP/telemetry configuration without oracle or MXC execution.
    [switch]$PrepareOnly,
    [switch]$NoTelemetry,
    # Route Claude Code through LiteLLM's Messages API (LITELLM_PROXY_URL, LITELLM_PROXY_API_KEY) with
    # -Model as the only model: every alias and subagent pins to it.
    [switch]$Litellm,
    # Run evals/mock-agent/mock_claude.py as the MXC workload instead of claude: no model calls,
    # no token needed. Scenarios are comma-separated (see mock_claude.py); default happy.
    [switch]$MockAgent,
    [string]$MockScenario = 'happy',
    # A provider content filter (stop_reason content_filter/refusal) ends `claude -p` mid-workflow. Resume
    # the same session with a neutral prompt up to this many times; each resume counts as an intervention.
    [int]$ResumeOnRefusal = 0,
    # Omit to preserve Claude's default effort; explicit effort applies to main and child requests.
    [ValidateSet('low', 'medium', 'high', 'xhigh', 'max')][string]$Effort
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'run-lifecycle.ps1')
if ($PrepareOnly -and ($DryRun -or $MockAgent)) { throw '-PrepareOnly cannot be combined with -DryRun or -MockAgent.' }
$onWindows = $IsWindows -or $PSVersionTable.PSEdition -eq 'Desktop'
if (-not $ToolsDir) { $ToolsDir = if ($onWindows) { 'C:\repos\.eval-tools' } else { Join-Path $HOME 'repos/.eval-tools' } }
if (-not $WorkRoot) { $WorkRoot = if ($onWindows) { 'C:\repos' } else { Join-Path $HOME 'repos' } }
$venvPython = { param($venv) if ($onWindows) { Join-Path $venv 'Scripts\python.exe' } else { Join-Path $venv 'bin/python' } }
$tar = if ($onWindows) { Join-Path $env:SystemRoot 'System32\tar.exe' } else { 'tar' }   # never MSYS tar for a C: path (O22)

$plugin = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path          # geeky-orchestration
$repoRoot = (Resolve-Path (Join-Path $plugin '..')).Path
$caseDir = Join-Path $plugin "evals/cases/$Case"
$mxc = Join-Path $repoRoot 'mxc-sandbox/scripts/mxc.ps1'
# Oracle needs a Python with pytest; geeky_mcp must run on the venv's own base interpreter (D75).
$python = @((Get-Command python, python3 -All -ErrorAction SilentlyContinue).Source; if ($onWindows) { 'C:\Python314\python.exe' }
    & $venvPython (Join-Path $ToolsDir 'agent-py')) |
    Where-Object { $_ -and (Test-Path $_) } | Where-Object { & $_ -c 'import pytest' 2>$null; -not $LASTEXITCODE } | Select-Object -First 1
if (-not $python) { throw 'no Python with pytest found' }
# GEEKY_EVAL_MCP_RUNTIME=node serves geeky_mcp from the dependency-free Node twin (mcp/server.mts),
# for hosts with no allowed Python that can install the mcp package (it needs Python 3.10+).
$nodeMcp = $env:GEEKY_EVAL_MCP_RUNTIME -eq 'node'
if ($nodeMcp) {
    $nodeExe = (Get-Command node -ErrorAction Stop).Source
} else {
# Windows: the venv launcher cannot start inside MXC, so geeky_mcp runs on the base interpreter. Elsewhere
# the venv's python is a symlink to a system interpreter the sandbox can see.
$mcpPython = if ($onWindows) {
    $venvHome = ((Get-Content (Join-Path $ToolsDir 'geeky-mcp\pyvenv.cfg')) -match '^home\s*=' -replace '^home\s*=\s*', '').Trim()
    Join-Path $venvHome 'python.exe'
} else { & $venvPython (Join-Path $ToolsDir 'geeky-mcp') }
$mcpSitePackages = (& (& $venvPython (Join-Path $ToolsDir 'geeky-mcp')) -c "import sysconfig; print(sysconfig.get_paths()['purelib'])").Trim()
}
$agentPy = Join-Path $ToolsDir 'agent-py'   # venv with pytest, first on the sandbox PATH
$agentPython = & $venvPython $agentPy
if (-not (Test-Path $agentPython)) { throw "missing $agentPy (uv venv + uv pip install --offline pytest)" }
$caseCfg = Get-Content (Join-Path $caseDir 'case.json') -Raw | ConvertFrom-Json   # D66 limits
if (-not $PSBoundParameters.ContainsKey('TimeoutSec')) { $TimeoutSec = [int]$caseCfg.wall_time_ceiling_s }
# The slow scenario exists to hit the timeout; keep it short unless one was given.
if ($MockAgent -and $MockScenario -match '\bslow\b' -and -not $PSBoundParameters.ContainsKey('TimeoutSec')) { $TimeoutSec = 30 }
if ($TimeoutSec -le 0) { throw 'TimeoutSec must be positive' }
if ($CloseoutReserveSec -lt 0 -or $CloseoutReserveSec -ge $TimeoutSec) { throw 'CloseoutReserveSec must be nonnegative and smaller than TimeoutSec' }
if ($ResumeOnRefusal -lt 0) { throw 'ResumeOnRefusal must be nonnegative' }
if ($Effort) { $Effort = $Effort.ToLowerInvariant() }
$configuredEffort = if ($Effort) { $Effort } else { 'default' }

if ($Litellm) {
    if (-not $Model) { throw '-Litellm needs -Model (the LiteLLM model name)' }
    if (-not $env:LITELLM_PROXY_URL -or -not $env:LITELLM_PROXY_API_KEY) { throw '-Litellm needs LITELLM_PROXY_URL and LITELLM_PROXY_API_KEY' }
    # LiteLLM takes its virtual key as an API key (x-api-key), not a bearer auth token. Process scope only,
    # so the MXC host can copy it by bare name; clearing the OAuth token keeps the run on the key.
    $env:ANTHROPIC_API_KEY = $env:LITELLM_PROXY_API_KEY
    Remove-Item Env:ANTHROPIC_AUTH_TOKEN, Env:CLAUDE_CODE_OAUTH_TOKEN -ErrorAction SilentlyContinue
} else {
    if (-not $MockAgent -and -not $env:CLAUDE_CODE_OAUTH_TOKEN) {
        $env:CLAUDE_CODE_OAUTH_TOKEN = [Environment]::GetEnvironmentVariable('CLAUDE_CODE_OAUTH_TOKEN', 'User')
    }
    if (-not $MockAgent -and -not $env:CLAUDE_CODE_OAUTH_TOKEN) { throw 'CLAUDE_CODE_OAUTH_TOKEN is not set (run claude setup-token).' }
}

function Invoke-Oracle([string]$target, [string]$outFile) {
    $env:ORACLE_TARGET = $target
    Push-Location (Join-Path $caseDir 'oracle')
    try { $out = & $python -m pytest -q -rA -p no:cacheprovider 2>&1 | Out-String } finally { Pop-Location }
    if ($outFile) { Add-Content -Path $outFile -Value $out }
    $last = ($out -split "`r?`n" | Where-Object { $_ -match '\d+ (passed|failed|error)' } | Select-Object -Last 1)
    $n = { param($k) if ($last -match "(\d+) $k") { [int]$Matches[1] } else { 0 } }
    [ordered]@{ passed = & $n 'passed'; failed = & $n 'failed'; errors = & $n 'error'; skipped = & $n 'skipped' }
}

# 1. Seed workspace
$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$ws = Join-Path $WorkRoot "eval-$Case-$stamp"
New-Item -ItemType Directory -Force $ws | Out-Null
Copy-Item (Join-Path $caseDir 'brief.md'), (Join-Path $caseDir 'contract.md') $ws
Set-Content (Join-Path $ws '.gitignore') "__pycache__/`n"
git -C $ws init -q
git -C $ws -c user.name=eval-seed -c user.email=eval-seed@local add -A
git -C $ws -c user.name=eval-seed -c user.email=eval-seed@local commit -q -m 'seed: brief and contract'
$baseSha = git -C $ws rev-parse HEAD
Write-Host "workspace: $ws (seed $baseSha)"

# 3. Harness config, outside the workspace
$cfg = Join-Path $WorkRoot ".eval-cfg-$stamp"
New-Item -ItemType Directory -Force $cfg | Out-Null
$out = Join-Path $plugin "evals/baselines/$Case/$stamp"
New-Item -ItemType Directory -Force $out | Out-Null
$recordPath = Join-Path $out 'run.json'
$execution = New-EvalExecution $TimeoutSec $CloseoutReserveSec $ResumeOnRefusal $AuthorizationReference $null `
    ([ordered]@{ model = $Model; reasoning = $configuredEffort; mock = [bool]$MockAgent; provider = $(if ($Litellm) { 'litellm' } else { 'anthropic' }) })
$record = [ordered]@{ schema = 'geeky-eval.baseline/1'; case = $Case; harness = 'claude-code'; model = $Model
    reasoning = $configuredEffort; workspace = $ws; config_directory = $cfg; seed_sha = $baseSha; started_utc = $null; ended_utc = $null
    exit_code = $null; exit_reason = 'prepared'; is_error = $false; reference_eligible = $false
    analysis_status = 'not-started'; execution = $execution }
Write-EvalRecord $record $recordPath
$finalizationStart = Get-EvalMonotonicSeconds
try {
# Plugin export at the committed HEAD without evals/ (D50: the contestant never sees oracles or specs).
# Granted read-write: under a read-only MXC grant Claude's directory scan of skills/agents/commands
# fails with EPERM and no plugin components load. The export is per run; its hash is checked after.
$pluginSha = git -C $repoRoot rev-parse HEAD
$export = Join-Path $cfg 'plugin'
New-Item -ItemType Directory -Force $export | Out-Null
$pluginTar = Join-Path $cfg 'plugin.tar'
git -C $repoRoot archive --format=tar "--output=$pluginTar" $pluginSha geeky-orchestration ':(exclude)geeky-orchestration/evals'
& $tar -xf $pluginTar -C $export
if ($LASTEXITCODE) { throw 'plugin export extraction failed' }
Remove-Item $pluginTar
$pluginRun = Join-Path $export 'geeky-orchestration'
Assert-EvalWorkflowBudgetExport $plugin $pluginRun
# Bytecode caches are not plugin changes (the agent's tooling may compile the gate scripts).
$hashTree = { param($dir) (Get-ChildItem $dir -Recurse -File | Where-Object { $_.FullName -notmatch '[\\/]__pycache__[\\/]' } | Sort-Object FullName | ForEach-Object { (Get-FileHash $_.FullName -Algorithm SHA256).Hash }) -join '' }
$exportHash = & $hashTree $pluginRun
$mcpDir = Join-Path $pluginRun 'mcp'
if ($nodeMcp) {
    @{ mcpServers = @{ geeky = @{ command = $nodeExe; args = @('--disable-warning=ExperimentalWarning', (Join-Path $mcpDir 'server.mts')) } } } |
        ConvertTo-Json -Depth 6 | Set-Content (Join-Path $cfg 'mcp.json')
} else {
$boot = "import site,runpy,sys; site.addsitedir(r'$mcpSitePackages'); sys.path.insert(0, r'$mcpDir'); sys.argv=[r'$(Join-Path $mcpDir 'server.py')']; runpy.run_path(sys.argv[0], run_name='__main__')"
@{ mcpServers = @{ geeky = @{ command = $mcpPython; args = @('-u', '-c', $boot) } } } | ConvertTo-Json -Depth 6 | Set-Content (Join-Path $cfg 'mcp.json')
}
. (Join-Path $plugin 'evals/telemetry/telemetry.ps1')
$telemetry = Resolve-EvalTelemetry -Harness claude-code -ToolsDir $ToolsDir -Case $Case -Stamp $stamp -PluginSha $pluginSha `
    -ConfigDir $cfg -StateDir (Join-Path $cfg 'langfuse') -Model $Model `
    -Reasoning $Effort -WorkerReasoning $Effort -ReviewerReasoning $Effort `
    -Provider $(if ($Litellm) { 'litellm' } else { 'anthropic' }) -Mock:$MockAgent -Disabled:$NoTelemetry
$settings = [ordered]@{ env = @{ MCP_TIMEOUT = '120000' } }
if ($telemetry) {
    $settings.hooks = $telemetry.SettingsHooks
    # claude -p otherwise requests display "omitted" thinking, leaving only signatures in the transcript.
    $settings.showThinkingSummaries = $true
}
$settings | ConvertTo-Json -Depth 8 | Set-Content (Join-Path $cfg 'settings.json')

$prompt = (Get-Content (Join-Path $plugin 'evals/prompts/end-to-end.md') -Raw) -replace '\{\{PLANNING_FOLDER\}\}', 'docs/spec-001-feature/'
$promptSha = [BitConverter]::ToString([Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($prompt))).Replace('-', '').ToLower()
$sid = [guid]::NewGuid().ToString()
$claudeArgs = @('-p', $prompt, '--session-id', $sid, '--plugin-dir', $pluginRun,
    '--settings', (Join-Path $cfg 'settings.json'), '--strict-mcp-config', '--mcp-config', (Join-Path $cfg 'mcp.json'),
    '--permission-mode', 'bypassPermissions', '--output-format', 'stream-json', '--verbose')
# Git Bash cannot start in Windows MXC (microsoft/mxc#1061), so every Bash call fails with exit 66; hide the
# tool there. Elsewhere Bash is the shell. WebSearch is an Anthropic server tool a LiteLLM route cannot serve.
$disallowed = @(if ($onWindows) { 'Bash' }) + @(if ($Litellm) { 'WebSearch' })
if ($disallowed) { $claudeArgs += @('--disallowedTools', ($disallowed -join ',')) }
if ($Model) { $claudeArgs += @('--model', $Model) }
if ($Effort) { $claudeArgs += @('--effort', $Effort) }
if ($MockAgent) {
    # MXC_CLAUDE_EXE must be an .exe: the agent-py python runs the mock, copied under the read-only
    # config grant so the sandbox can read it (the repo itself is not granted).
    $mockDir = Join-Path $cfg 'mock-agent'
    New-Item -ItemType Directory -Force $mockDir | Out-Null
    Copy-Item (Join-Path $plugin 'evals/mock-agent/mock_claude.py') $mockDir
    if ($MockScenario -eq 'c0-full') {
        if ($Case -ne 'c0-wordstat' -or -not $NoTelemetry) { throw 'c0-full requires c0-wordstat and -NoTelemetry' }
        Copy-Item (Join-Path $plugin 'evals/mock-agent/c0_fixture.py') $mockDir
    }
    $claudeArgs = @((Join-Path $mockDir 'mock_claude.py')) + $claudeArgs + @('--mock-scenario', $MockScenario, '--mock-case', $Case)
}
# Git sees the workspace as owned by another identity inside the container ("dubious ownership").
# Trust only this workspace via env-scoped config; the global git config is untouched.
# MXC forces TEMP/TMP to the AppContainer's AC\Temp, where mkdtemp()-style folders (pytest tmp_path,
# git temp repos) are not writable. TMPDIR takes precedence for Python, pytest and git and MXC leaves
# it alone, so point it at a per-run writable folder.
$runTmp = Join-Path $cfg 'tmp'
New-Item -ItemType Directory -Force $runTmp | Out-Null
$hostArgs = @('run', '--workspace', $ws, '--isolated', '--allow-win32k', '--host-loopback',
    '--rw', $export, '--ro', $cfg, '--rw', $runTmp, '--ro', $ToolsDir, '--timeout', $TimeoutSec,
    '--env', "TMPDIR=$runTmp",
    '--env', 'PYTHONDONTWRITEBYTECODE=1',   # keep __pycache__ out of the read-write plugin export and the workspace
    '--env', 'GIT_CONFIG_COUNT=1', '--env', 'GIT_CONFIG_KEY_0=safe.directory', '--env', "GIT_CONFIG_VALUE_0=$($ws -replace '\\','/')",
    '--env', "XDG_CONFIG_HOME=$cfg",   # host ~/.config/git is unreadable in the container; avoid per-command warnings
    # Claude's Windows shell launcher makes every PowerShell tool call report "Exit code 5" inside MXC
    # even on success; without it the tool returns the command's real exit code.
    '--env', 'CLAUDE_CODE_DISABLE_WINDOWS_SHELL_LAUNCHER=1',
    # claude -p stops background subagents still running 10 min after the main agent's last turn; the
    # main agent is resumed when they finish, so wait indefinitely (the MXC --timeout still bounds the run).
    '--env', 'CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0',
    # The workload may run as root inside the container (Bubblewrap maps uid 0), where claude refuses
    # bypassPermissions unless told it is already sandboxed, which MXC is.
    '--env', 'IS_SANDBOX=1',
    # pytest for the agents' own tests: the sandbox Python has none and pip cannot install inside MXC.
    '--env', "PATH=$(Split-Path $agentPython)$([IO.Path]::PathSeparator)$env:PATH", '--env', "VIRTUAL_ENV=$agentPy",
    # Neutral commit identity (the seed repo has none); set via env so no repo or global config changes.
    '--env', 'GIT_AUTHOR_NAME=geeky-eval-agent', '--env', 'GIT_AUTHOR_EMAIL=agent@geeky-eval.local',
    '--env', 'GIT_COMMITTER_NAME=geeky-eval-agent', '--env', 'GIT_COMMITTER_EMAIL=agent@geeky-eval.local')
if ($MockAgent -and $MockScenario -eq 'c0-full') { $hostArgs += '--deny-net' }
# GEEKY_EVAL_EXTRA_RO: extra read-only grants (PATH-separated), e.g. an interpreter's standard library
# that sits outside the folders the host derives from PATH.
foreach ($dir in @(($env:GEEKY_EVAL_EXTRA_RO ?? '') -split [IO.Path]::PathSeparator | Where-Object { $_ })) { $hostArgs += @('--ro', $dir) }
if ($Effort) {
    # The environment override also reaches subagent/reviewer requests and any child Claude process.
    $hostArgs += @('--env', "CLAUDE_CODE_EFFORT_LEVEL=$Effort")
}
if ($telemetry) { $hostArgs += @('--rw', $telemetry.StateDir) + @($telemetry.RoPaths | ForEach-Object { '--ro', $_ }) + $telemetry.EnvArgs }
if ($Litellm) {
    $hostArgs += @('--env', "ANTHROPIC_BASE_URL=$($env:LITELLM_PROXY_URL.TrimEnd('/'))", '--env', 'ANTHROPIC_API_KEY',
        # One model everywhere: main loop, aliases subagents may name, and background/small-model calls.
        '--env', "ANTHROPIC_DEFAULT_OPUS_MODEL=$Model", '--env', "ANTHROPIC_DEFAULT_SONNET_MODEL=$Model",
        '--env', "ANTHROPIC_DEFAULT_HAIKU_MODEL=$Model", '--env', "CLAUDE_CODE_SUBAGENT_MODEL=$Model",
        '--env', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1')
}

# GEEKY_EVAL_OTEL=1: Claude Code's built-in OpenTelemetry export (runs inside the CLI on Node, no
# Python hook), sent to Langfuse's OTLP endpoint. The Basic-auth header is derived here from
# LANGFUSE_PUBLIC_KEY/SECRET_KEY and every value reaches the sandbox by name only, never on a
# command line. Observes the run; adds nothing the contestant can read.
$nativeOtel = $null
if ($env:GEEKY_EVAL_OTEL -in '1', 'true') {
    if (-not $env:LANGFUSE_PUBLIC_KEY -or -not $env:LANGFUSE_SECRET_KEY -or -not $env:OTEL_EXPORTER_OTLP_ENDPOINT) {
        throw 'GEEKY_EVAL_OTEL needs LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY and OTEL_EXPORTER_OTLP_ENDPOINT'
    }
    $basic = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("$($env:LANGFUSE_PUBLIC_KEY):$($env:LANGFUSE_SECRET_KEY)"))
    $env:OTEL_EXPORTER_OTLP_HEADERS = "Authorization=Basic $basic"
    $runAttributes = "geeky.case=$Case,geeky.stamp=$stamp,geeky.harness=claude-code,langfuse.session.id=$sid"
    $env:OTEL_RESOURCE_ATTRIBUTES = (@($env:OTEL_RESOURCE_ATTRIBUTES, $runAttributes) | Where-Object { $_ }) -join ','
    $otelNames = @('CLAUDE_CODE_ENABLE_TELEMETRY', 'CLAUDE_CODE_ENHANCED_TELEMETRY_BETA', 'OTEL_EXPORTER_OTLP_ENDPOINT',
        'OTEL_EXPORTER_OTLP_PROTOCOL', 'OTEL_EXPORTER_OTLP_HEADERS', 'OTEL_LOGS_EXPORTER', 'OTEL_METRICS_EXPORTER',
        'OTEL_TRACES_EXPORTER', 'OTEL_RESOURCE_ATTRIBUTES', 'OTEL_SERVICE_NAME', 'LANGFUSE_TRACING_ENVIRONMENT') |
        Where-Object { [Environment]::GetEnvironmentVariable($_) }
    foreach ($name in $otelNames) { $hostArgs += @('--env', $name) }
    $nativeOtel = [ordered]@{ enabled = $true; channel = 'claude-code-otel'; endpoint = $env:OTEL_EXPORTER_OTLP_ENDPOINT
        protocol = $env:OTEL_EXPORTER_OTLP_PROTOCOL; service_name = $env:OTEL_SERVICE_NAME; session_attribute = $sid
        exporters = [ordered]@{ traces = $env:OTEL_TRACES_EXPORTER; logs = $env:OTEL_LOGS_EXPORTER; metrics = $env:OTEL_METRICS_EXPORTER }
        variables = @($otelNames) }
}

$record = [ordered]@{
    schema = 'geeky-eval.baseline/1'; case = $Case; harness = 'claude-code'; model = $Model; reasoning = $configuredEffort
    workspace = $ws; config_directory = $cfg; seed_sha = $baseSha; plugin_sha = $pluginSha; prompt_sha256 = $promptSha
    native_otel = $(if ($nativeOtel) { $nativeOtel } else { [ordered]@{ enabled = $false } })
    session_id = $sid; started_utc = $null; ended_utc = $null; wall_time_s = $null
    exit_code = $null; exit_reason = 'prepared'; is_error = $false; reference_eligible = $false
    token_ceiling = $caseCfg.token_ceiling; token_ceiling_policy = 'informational'
    analysis_status = 'not-started'; telemetry = $(if ($telemetry) { $telemetry.Record } else { @{ enabled = $false } })
}
$record['execution'] = $execution
$hostArgs += @('--env', "GEEKY_DEADLINE_UTC=$($record.execution.initial_deadline_utc)", '--env', "GEEKY_CLOSEOUT_RESERVE_S=$CloseoutReserveSec",
    '--env', "GEEKY_HARD_WALL_BUDGET_S=$TimeoutSec")
Write-EvalRecord $record $recordPath

if ($PrepareOnly) {
    $preparedExportManifest = @(Get-ChildItem -LiteralPath $pluginRun -Force -Recurse -File |
        Where-Object { $_.FullName -notmatch '[\\/]__pycache__[\\/]' } | Sort-Object FullName |
        ForEach-Object { [ordered]@{
            path = [IO.Path]::GetRelativePath($pluginRun, $_.FullName).Replace('\', '/')
            sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        } })
    $preparedManifestJson = ConvertTo-Json -InputObject $preparedExportManifest -Compress -Depth 4
    $preparedManifestPath = Join-Path $cfg 'plugin-export-manifest.json'
    Set-Content -LiteralPath $preparedManifestPath -Value $preparedManifestJson -NoNewline -Encoding utf8NoBOM
    $preparedExportSha = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData(
        [Text.Encoding]::UTF8.GetBytes($preparedManifestJson))).ToLowerInvariant()
    $prepared = [ordered]@{
        schema = 'geeky-eval.prepared-launch/1'; status = 'prepared'; harness = 'claude-code'; case = $Case
        model = $Model; reasoning = $configuredEffort; provider = $(if ($Litellm) { 'litellm' } else { 'anthropic' })
        workspace = $ws; config_directory = $cfg; seed_sha = $baseSha; plugin_sha = $pluginSha
        plugin_export_sha256 = $preparedExportSha; plugin_export_manifest = $preparedManifestPath
        prompt_sha256 = $promptSha; timeout_s = $TimeoutSec
        native_session_id = $sid; telemetry = $(if ($telemetry) { $telemetry.Record } else { @{ enabled = $false } })
        host_script = $mxc; host_args = $hostArgs; harness_args = $claudeArgs
        oracle_executed = $false; mxc_workload_started = $false; contestant_started = $false; model_calls = 0
    }
    $prepared['execution'] = $record.execution
    Write-EvalRecord $prepared (Join-Path $cfg 'prepared-launch.json')
    Write-Host "prepared: $cfg"
    Write-Host 'Preparation complete; no oracle, MXC workload or contestant was started.'
    return
}

# Legacy DryRun executes the seed oracle; PrepareOnly above never does.
Set-EvalStep $record $recordPath 'seed-oracle'
$empty = Invoke-Oracle $ws $null
if ($empty.passed -gt 0) { throw "oracle passed $($empty.passed) tests against the empty workspace" }
if ($DryRun) {
    Write-Host "DRY RUN. Would launch (timeout ${TimeoutSec}s):`n  $mxc $($hostArgs -join ' ') -- <claude args: session $sid, plugin $pluginRun>"
    Write-Host "Workspace $ws and config $cfg were created; delete them when done."
    return
}

# Run
$started = (Get-Date).ToUniversalTime()
$resumes = 0
$exitCode = $null
if ($MockAgent) { $savedExe = $env:MXC_CLAUDE_EXE; $env:MXC_CLAUDE_EXE = $agentPython }
try {
    if (Start-EvalInvocation $record $recordPath 'initial' ([ref]$hostArgs)) {
        & $mxc @hostArgs -- @claudeArgs > (Join-Path $out 'stream.jsonl') 2> (Join-Path $out 'stderr.txt')
        $exitCode = $LASTEXITCODE
        Complete-EvalInvocation $record $recordPath $exitCode
    }
} finally { if ($MockAgent) { $env:MXC_CLAUDE_EXE = $savedExe } }
while ($null -ne $exitCode -and $exitCode -eq 0 -and $resumes -lt $ResumeOnRefusal) {
    $last = Get-Content (Join-Path $out 'stream.jsonl') | Where-Object { $_ -match '"type":"result"' } | Select-Object -Last 1 | ConvertFrom-Json
    if ($last.stop_reason -notin 'content_filter', 'refusal') { break }
    if (-not (Start-EvalInvocation $record $recordPath 'refusal-recovery' ([ref]$hostArgs))) { break }
    $resumes++
    Write-Host "provider refusal: resuming session $sid ($resumes/$ResumeOnRefusal)"
    # Same flags, with a neutral prompt in place of the canonical one and --resume for --session-id.
    $resumeArgs = @($claudeArgs)
    $resumeArgs[[array]::IndexOf($resumeArgs, '-p') + 1] = 'Continue the workflow from where you stopped.'
    $resumeArgs[[array]::IndexOf($resumeArgs, '--session-id')] = '--resume'
    if ($MockAgent) { $env:MXC_CLAUDE_EXE = $agentPython }
    try { & $mxc @hostArgs -- @resumeArgs >> (Join-Path $out 'stream.jsonl') 2>> (Join-Path $out 'stderr.txt') }
    finally { if ($MockAgent) { $env:MXC_CLAUDE_EXE = $savedExe } }
    $exitCode = $LASTEXITCODE
    Complete-EvalInvocation $record $recordPath $exitCode
}
$ended = (Get-Date).ToUniversalTime()
$finalizationStart = Get-EvalMonotonicSeconds
if ($null -eq $exitCode) { throw 'Attempt deadline exhausted before launch' }
Set-EvalStep $record $recordPath 'native-capture'
if ($MockAgent) {
    $mockSummary = Get-Content (Join-Path $out 'stream.jsonl') | Where-Object { $_ -match '"subtype":"mock_summary"' } | Select-Object -Last 1
    if ($mockSummary) { $mockSummary | ConvertFrom-Json | ConvertTo-Json -Depth 6 | Set-Content (Join-Path $out 'mock-summary.json') }
}

$pluginTampered = (& $hashTree $pluginRun) -ne $exportHash
$mxcStateRoot = if ($env:MXC_SANDBOX_HOME) { $env:MXC_SANDBOX_HOME } else { Join-Path $(if ($onWindows) { $env:USERPROFILE } else { $HOME }) '.mxc-sandbox' }
$session = Get-ChildItem (Join-Path $mxcStateRoot 'home/projects') -Recurse -Filter "$sid.jsonl" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($session) {
    Copy-Item $session.FullName (Join-Path $out 'session.jsonl')
    # Each subagent's transcript (with a meta.json naming the spawning tool call); nested subagents appear only here.
    $subDir = Join-Path $session.DirectoryName "$sid/subagents"
    if (Test-Path $subDir) { Copy-Item $subDir (Join-Path $out 'subagents') -Recurse }
}

# 4. Oracle 3x
Set-EvalStep $record $recordPath 'oracle'
$oracleFile = Join-Path $out 'oracle-output.txt'
$runs = 1..3 | ForEach-Object { Invoke-Oracle $ws $oracleFile }
$stable = ($runs | ForEach-Object { "$($_.passed)/$($_.failed)/$($_.errors)/$($_.skipped)" } | Select-Object -Unique).Count -eq 1

# 5. Store
Set-EvalStep $record $recordPath 'parsing'
git -C $ws bundle create (Join-Path $out 'solution.bundle') --all 2>$null
$lines = Get-Content (Join-Path $out 'stream.jsonl') | Where-Object { $_ -match '^\{' }
$init = $lines | Where-Object { $_ -match '"subtype":"init"' } | Select-Object -First 1 | ConvertFrom-Json
$results = @($lines | Where-Object { $_ -match '"type":"result"' } | ForEach-Object { $_ | ConvertFrom-Json })
$result = $results | Select-Object -Last 1
# One result line per invocation; a resumed run sums them.
$u = [pscustomobject]@{ output_tokens_details = [pscustomobject]@{ thinking_tokens = 0 } }
foreach ($field in 'input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens') {
    $u | Add-Member $field ([long]($results | ForEach-Object { [long]$_.usage.$field } | Measure-Object -Sum).Sum)
}
$u.output_tokens_details.thinking_tokens = [long]($results | ForEach-Object { [long]$_.usage.output_tokens_details.thinking_tokens } | Measure-Object -Sum).Sum
$inputTotal = [long]$u.input_tokens + [long]$u.cache_creation_input_tokens + [long]$u.cache_read_input_tokens
$pkg = $Case.Substring(3)
$py = Get-ChildItem $ws -Recurse -Filter *.py -File | Where-Object { $_.FullName -notmatch '[\\/]\.git[\\/]' }
$isTest = { param($f) $f.Name -like 'test_*' -or $f.Name -like '*_test.py' -or $f.FullName -match '[\\/]tests?[\\/]' }
$pkgDir = (Join-Path $ws $pkg) + [IO.Path]::DirectorySeparatorChar
# Get-ChildItem reports on-disk casing (C:\Repos) that may differ from $ws (C:\repos); compare case-insensitively.
$srcLoc = ($py | Where-Object { $_.FullName.StartsWith($pkgDir, [StringComparison]::OrdinalIgnoreCase) -and -not (& $isTest $_) } | Get-Content | Measure-Object -Line).Lines
$testLoc = ($py | Where-Object { & $isTest $_ } | Get-Content | Measure-Object -Line).Lines
$bgKilled = Select-String -Path (Join-Path $out 'stderr.txt') -Pattern 'Background tasks still running .* stopping them' -Quiet
# A provider-side refusal (content filter) ends the session cleanly, so it must not read as completed.
$exitReason = if ($exitCode -eq 124) { 'timeout' } elseif ($bgKilled) { 'bg_killed' } elseif ($result.stop_reason -in 'content_filter', 'refusal') { 'provider_refusal' } elseif ($result -and -not $result.is_error) { 'completed' } elseif (-not $result) { 'crash' } else { 'aborted' }
$o = $runs[0]
$clean = $stable -and $o.failed -eq 0 -and $o.errors -eq 0 -and $o.passed -gt 0
$sha = { param($p) if (Test-Path $p) { (Get-FileHash $p -Algorithm SHA256).Hash.ToLower() } }
Set-EvalStep $record $recordPath 'telemetry'
if ($telemetry -and $session -and -not $MockAgent) { Invoke-ClaudeTelemetryFlush -Telemetry $telemetry -SessionId $sid -TranscriptPath $session.FullName }
if ($telemetry) { Save-EvalTelemetry -Telemetry $telemetry -SessionId $(if ($session) { $sid }) -OutDir $out -Python $python }
if ($telemetry -and -not $telemetry.Record.delivered) { $record.execution.postprocessing['telemetry'] = 'pending-or-incomplete' }

$analysisRecord = [ordered]@{
    schema = 'geeky-eval.baseline/1'; case = $Case
    harness = 'claude-code'; harness_version = $init.claude_code_version; model = $init.model; reasoning = $configuredEffort
    approach = $(if ($MockAgent) { "mock-agent:$MockScenario" } else { 'canonical-prompt' }); plugin_sha = $pluginSha; plugin_modified_during_run = $pluginTampered; prompt_sha256 = $promptSha
    geeky_skills_loaded = @($init.skills | Where-Object { "$_" -like 'geeky-orchestration:*' }).Count
    workspace = $ws; seed_sha = $baseSha; session_id = $sid
    started_utc = $started.ToString('o'); ended_utc = $ended.ToString('o'); wall_time_s = [int]($ended - $started).TotalSeconds
    # Without a result line (timeout/crash) usage is unknown: record nulls, never zeros.
    tokens = $(if ($results.Count) { [ordered]@{ input = $inputTotal; cached_input = [long]$u.cache_read_input_tokens; output = [long]$u.output_tokens
        reasoning = [long]$u.output_tokens_details.thinking_tokens; total = $inputTotal + [long]$u.output_tokens } }
        else { [ordered]@{ input = $null; cached_input = $null; output = $null; reasoning = $null; total = $null } })
    model_calls = ($results | ForEach-Object { [int]$_.num_turns } | Measure-Object -Sum).Sum; latency_median_ms = $null; max_idle_gap_s = $null
    stages = $null; tasks = $null; impl_review_rounds = $null
    oracle = [ordered]@{ collected = $o.passed + $o.failed + $o.errors + $o.skipped; passed = $o.passed; failed = $o.failed
        errors = $o.errors; skipped = $o.skipped; stable_over_3 = $stable }
    loc = [ordered]@{ source_non_test = $srcLoc; test = $testLoc
        files_changed = @(git -C $ws diff --name-only $baseSha HEAD).Count }
    commits = [int](git -C $ws rev-list --count "$baseSha..HEAD")
    exit_code = $exitCode; exit_reason = $exitReason; stop_reason = $result.stop_reason; is_error = $result.is_error
    raw_sha256 = [ordered]@{ stream = & $sha (Join-Path $out 'stream.jsonl'); session = & $sha (Join-Path $out 'session.jsonl') }
    interventions = $resumes; resumed_after_refusal = $resumes
    telemetry = $(if ($telemetry) { $telemetry.Record } else { [ordered]@{ enabled = $false } })
    provider = $(if ($Litellm) { [ordered]@{ name = 'litellm'; base_url = $env:LITELLM_PROXY_URL; model = $Model } } else { $null })
    reference_eligible = (-not $MockAgent -and $clean -and $exitReason -eq 'completed' -and $resumes -eq 0 -and -not $pluginTampered -and @($init.skills | Where-Object { "$_" -like 'geeky-orchestration:*' }).Count -gt 0)
}
foreach ($key in $analysisRecord.Keys) { $record[$key] = $analysisRecord[$key] }
Write-EvalRecord $record $recordPath

Set-Content (Join-Path $out 'notes.md') "# Baseline $Case $stamp`n`nApproach: canonical prompt, Claude Code isolated in MXC.`nInterventions: none.`nOracle verdicts: (fill in for any failure)`n"
# Derived measures (tokens summed over all models, stages, latency, idle gap) -> run.json + analysis.json
Set-EvalStep $record $recordPath 'analysis'
$record.analysis_status = 'in-progress'
$analysisOut = Join-Path $out 'derived'
& $python (Join-Path $PSScriptRoot 'analyze-run.py') $out --token-ceiling $caseCfg.token_ceiling --out $analysisOut
if ($LASTEXITCODE -ne 0) { throw "Trace analysis failed: $out" }
# Preserve fields written by the existing analyzer.
$analyzed = Get-Content -LiteralPath (Join-Path $analysisOut 'run.json') -Raw | ConvertFrom-Json -AsHashtable
foreach ($key in $analyzed.Keys) { if ($key -ne 'execution') { $record[$key] = $analyzed[$key] } }
Copy-Item -LiteralPath (Join-Path $analysisOut 'analysis.json') -Destination (Join-Path $out 'analysis.json')
$record.analysis_status = 'complete'
} catch {
    if ($record.execution.active_step -eq 'capture' -or $record.execution.process_outcome -eq 'not-started') { $finalizationStart = Get-EvalMonotonicSeconds }
    Set-EvalFailure $record $_
    throw
} finally {
    if ($record.execution.state -eq 'prepared') { Write-EvalRecord $record $recordPath }
    else { Save-EvalFinalRecord $record $recordPath $ws $finalizationStart }
}
Write-Host "stored: $out"
