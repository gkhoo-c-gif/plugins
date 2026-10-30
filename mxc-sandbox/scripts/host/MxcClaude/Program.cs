// Runs the Claude Code CLI inside a Microsoft MXC sandbox: ProcessContainer on Windows, the native
// default backend elsewhere (Bubblewrap on Linux, Seatbelt on macOS).
//
//   MxcClaude probe                          report host support, tier, and the grants run mode would apply
//   MxcClaude run  [opts] -- <claude args>   headless claude in a ProcessContainer (filesystem + network policy)
//   MxcClaude tui  [opts] [-- <claude args>] interactive claude via IsolationSession PTY (no fs/net policy)
//
// Options:
//   --workspace <dir>   read-write project folder (default: current directory)
//   --isolated          run: use a private config dir instead of the host ~/.claude (auth via env token)
//   --deny-net          run: block all network egress
//   --allow-win32k      run: allow Win32k syscalls so node/npx/uv/pwsh can start, and enumerate-only access
//                       to the system drive for node's realpath (all folder names listable; weakens isolation)
//   --no-enumerate-root run: omit system-drive enumeration while retaining --allow-win32k and explicit grants
//   --ro <dir>          run: extra read-only grant (repeatable)
//   --rw <dir>          run: extra read-write grant (repeatable)
//   --timeout <sec>     kill the workload after this many seconds (default: none)
//   --host-loopback     run: allow the workload to reach host 127.0.0.1 (needed by Python asyncio / mcp-library
//                       stdio MCP servers on builds affected by microsoft/mxc#1505; opens every loopback port)
//   --env NAME=VALUE    set an environment variable in the workload (repeatable); a bare NAME copies
//                       the host's value. Applied last, so it overrides the host's pinned values.
//
// Environment overrides: MXC_CLAUDE_EXE (CLI path), MXC_SANDBOX_HOME (state root).

using System.Text;
using Microsoft.Mxc.Sdk;
using Microsoft.Mxc.Sdk.V1;

var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
// Not under %LOCALAPPDATA%: BaseContainer redirects that tree, so read-write grants there cannot be written.
var stateRoot = Environment.GetEnvironmentVariable("MXC_SANDBOX_HOME") ?? Path.Combine(home, ".mxc-sandbox");
var defaultClaudeExe = Path.Combine(home, ".local", "bin", OperatingSystem.IsWindows() ? "claude.exe" : "claude");
var claudeExe = Environment.GetEnvironmentVariable("MXC_CLAUDE_EXE")
    ?? (File.Exists(defaultClaudeExe) ? defaultClaudeExe : FindOnPath("claude").FirstOrDefault() ?? defaultClaudeExe);

// Shared mode: the host's real config dir is granted read-write so the existing login, settings,
// plugins and sessions apply. ~/.claude.json cannot be granted on its own (BaseContainer denies the
// single-file grant, and claude saves it via a temp file in the profile root), so the sandbox runs
// with CLAUDE_CONFIG_DIR=~/.claude and reads ~/.claude/.claude.json, refreshed one-way before each run.
var hostConfigDir = Path.Combine(home, ".claude");
var hostConfigFile = Path.Combine(home, ".claude.json");
var isolatedConfigDir = Path.Combine(stateRoot, "home");

if (args.Length == 0) return Usage("usage: MxcClaude probe | run [opts] -- <claude args> | tui [opts] [-- <claude args>]");

var mode = args[0];
Options opt;
try
{
    opt = Options.Parse(args[1..]);
}
catch (Exception e) when (e is ArgumentException or FormatException or IndexOutOfRangeException)
{
    return Usage($"bad options: {e.Message}");
}
var configDir = opt.Isolated ? isolatedConfigDir : hostConfigDir;

try
{
    return mode switch
    {
        "probe" => Probe(),
        "run" => await RunHeadlessAsync(),
        "tui" => await RunTuiAsync(),
        _ => Usage($"unknown mode '{mode}'"),
    };
}
catch (MxcException error)
{
    Console.Error.WriteLine($"MXC error [{error.Code}]: {error.Message}");
    return 1;
}

int Probe()
{
    var support = MxcPlatform.GetPlatformSupport();
    Console.WriteLine($"platform supported: {support.IsSupported}{(support.Reason is null ? "" : $" ({support.Reason})")}");
    foreach (var backend in MxcPlatform.GetAvailableBackends())
    {
        Console.WriteLine($"backend: {backend.Backend} tier={backend.Tier?.ToString() ?? "-"}");
        foreach (var warning in backend.Warnings) Console.WriteLine($"  warning: {warning}");
    }

    var request = BuildProcessContainerRequest(OperatingSystem.IsWindows() ? "cmd.exe /d /c echo probe" : "/bin/sh -c 'echo probe'");
    // The request-aware probe exists only for Windows ProcessContainer; elsewhere the backend list above is the report.
    string? probeError = null;
    if (OperatingSystem.IsWindows())
    {
        var probe = MxcContainer.Probe(request);
        Console.WriteLine($"processContainer tier: {probe.Tier?.ToString() ?? "-"}, needsDaclAugmentation: {probe.NeedsDaclAugmentation}");
        Console.WriteLine($"baseContainer api: {probe.Probes.BaseContainerApiPresent}, enumeratePaths: {probe.Probes.BaseContainerSupportsEnumeratePaths}, isolationSession: {probe.Probes.IsolationSessionAvailable}");
        foreach (var warning in probe.Warnings) Console.WriteLine($"probe warning: {warning}");
        probeError = probe.Error;
        if (probeError is not null) Console.Error.WriteLine($"probe error: {probeError}");
    }
    Console.WriteLine($"containment: {request.Containment.GetType().Name}");

    Console.WriteLine($"claude exe: {claudeExe} (exists: {File.Exists(claudeExe)})");
    Console.WriteLine($"config mode: {(opt.Isolated ? "isolated" : "shared")} (CLAUDE_CONFIG_DIR={configDir})");
    Console.WriteLine($"win32k: {(opt.AllowWin32k ? "allowed" : "blocked (node/npx/uv/pwsh cannot start)")}");
    var enumeratePaths = request.Containment is Containment.ProcessContainer processContainer
        ? processContainer.Filesystem?.EnumeratePaths : null;
    Console.WriteLine($"root enumeration: {(enumeratePaths is { Count: > 0 } ? string.Join(", ", enumeratePaths) : "disabled")}");
    foreach (var dir in request.Filesystem!.ReadonlyPaths) Console.WriteLine($"grant read-only:  {dir}");
    foreach (var dir in request.Filesystem!.ReadwritePaths) Console.WriteLine($"grant read-write: {dir}");
    foreach (var (name, value) in request.Environment!.Where(e => e.Key is "npm_config_cache" or "UV_CACHE_DIR" or "TEMP" or "TMPDIR" or "TMPPREFIX"))
        Console.WriteLine($"env pinned: {name}={value}");
    Console.WriteLine($"env token passthrough: {AuthSource() ?? "none"}");
    foreach (var name in opt.Env.Keys) Console.WriteLine($"env set: {name}");
    return support.IsSupported && probeError is null ? 0 : 1;
}

async Task<int> RunHeadlessAsync()
{
    if (opt.ClaudeArgs.Length == 0) return Usage("run needs claude arguments after --, e.g. -- -p \"hello\"");
    if (opt.Isolated)
    {
        Directory.CreateDirectory(isolatedConfigDir);
        if (Path.GetFileNameWithoutExtension(claudeExe).Equals("claude", StringComparison.OrdinalIgnoreCase)
            && AuthSource() is null)
            Console.Error.WriteLine("note: --isolated without CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN; claude will report 'Not logged in'.");
    }
    else if (File.Exists(hostConfigFile))
    {
        File.Copy(hostConfigFile, Path.Combine(hostConfigDir, ".claude.json"), overwrite: true);
    }

    var released = new ManualResetEventSlim();
    using var process = await SpawnPinnedAsync(BuildProcessContainerRequest(ClaudeCommand(claudeExe)), released);
    // claude -p treats piped stdin as extra prompt; close it so it never waits.
    process.StandardInput?.Close();
    var stdout = ForwardAsync(process.StandardOutput, Console.OpenStandardOutput());
    var stderr = ForwardAsync(process.StandardError, Console.OpenStandardError());
    var result = await process.WaitAsync();
    released.Set();
    await Task.WhenAll(stdout, stderr);
    return Finish(result, process.Warnings);
}

async Task<int> RunTuiAsync()
{
    if (Console.IsInputRedirected || Console.IsOutputRedirected)
        return Usage("tui needs an interactive terminal");
    if (AuthSource() is null)
        Console.Error.WriteLine("note: no CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY on host; the agent account has no login, so run /login inside the session.");

    // IsolationSession runs as a separate, ephemeral agent account that cannot read the host user's
    // profile, so the CLI is staged under %ProgramData%. The agent keeps its own profile and config.
    var tuiEnvironment = BaseEnvironment();
    ApplyUserEnvironment(tuiEnvironment);
    var request = new ContainerRequest(ClaudeCommand(StageForAgentUser(claudeExe)))
    {
        Containment = new Containment.IsolationSession(),
        // IsolationSession cannot restrict networking and requires this posture to be explicit.
        Network = new NetworkPolicy
        {
            Egress = new NetworkEgressPolicy { Default = NetworkAction.Allow },
            Ingress = new NetworkIngressPolicy { Default = NetworkAction.Allow, HostLoopback = NetworkAction.Allow },
        },
        WorkingDirectory = opt.Workspace,
        Environment = tuiEnvironment,
        InheritDefaultEnvironment = true,
        TimeoutMs = TimeoutMs(),
    };

    var size = new MxcPtySize((ushort)Math.Max(Console.WindowHeight, 10), (ushort)Math.Max(Console.WindowWidth, 40));
    using var terminal = MxcContainer.SpawnWithPty(request, new SpawnWithPtyOptions { Size = size });
    using var inputCancellation = new CancellationTokenSource();
    var input = Console.OpenStandardInput().CopyToAsync(terminal.Input, inputCancellation.Token);
    var output = terminal.Output.CopyToAsync(Console.OpenStandardOutput());

    var result = await terminal.WaitAsync();
    await output;
    inputCancellation.Cancel();
    terminal.Input.Close();
    _ = input.ContinueWith(static _ => { }, TaskScheduler.Default);
    return Finish(result, terminal.Warnings);
}

ContainerRequest BuildProcessContainerRequest(string command)
{
    var (runtimeDirs, caches) = RuntimeGrants(stateRoot);
    foreach (var dir in caches.Values) Directory.CreateDirectory(dir);
    var filesystem = new FilesystemPolicy();
    // Never grant a drive root: on BaseContainer a read-only grant on C:\ exposes every child folder.
    AddDistinct(filesystem.ReadonlyPaths, [Path.GetDirectoryName(claudeExe)!, Path.GetDirectoryName(ResolveLink(claudeExe))!, .. runtimeDirs, .. opt.ReadOnly]);
    AddDistinct(filesystem.ReadwritePaths, [opt.Workspace, configDir, .. caches.Values, .. opt.ReadWrite]);

    var environment = BaseEnvironment();
    environment["CLAUDE_CONFIG_DIR"] = configDir;
    // Inside the container %LOCALAPPDATA% is redirected to the AppContainer profile, so tools would
    // otherwise write their caches somewhere ungranted. Pin them to the granted folders.
    foreach (var (variable, dir) in caches) environment[variable] = dir;
    ApplyUserEnvironment(environment);
    // Seatbelt does not make /tmp writable, and zsh keeps here-document, here-string and =(...) temp files in
    // $TMPPREFIX (default /tmp/zsh), so every heredoc in Claude Code's Bash tool fails with "can't create temp
    // file for here document". TMPPREFIX is a file-name prefix, not a folder: keep it inside the final TMPDIR
    // (after --env, so a caller's TMPDIR or TMPPREFIX wins). Bubblewrap gives the workload a private /tmp.
    if (OperatingSystem.IsMacOS() && !environment.ContainsKey("TMPPREFIX") && environment.TryGetValue("TMPDIR", out var tmpDir))
        environment["TMPPREFIX"] = Path.Combine(tmpDir, "zsh");

    // Outside Windows the native default backend applies (Bubblewrap on Linux, Seatbelt on macOS); Win32k
    // and drive-root enumeration are Windows concepts. Bubblewrap hides /usr/local, /opt and /home unless
    // granted, which RuntimeGrants covers for the CLI's own runtimes.
    if (!OperatingSystem.IsWindows())
        return new ContainerRequest(command)
        {
            Filesystem = filesystem,
            Network = new NetworkPolicy
            {
                Egress = new NetworkEgressPolicy { Default = opt.DenyNet ? NetworkAction.Deny : NetworkAction.Allow },
                // --host-loopback reaches host 127.0.0.1, e.g. a local gateway or certificate proxy.
                // An omitted HostLoopback is Deny. Seatbelt ties inbound loopback to the inbound
                // default, and the SDK rejects allow-loopback with deny-default, so on macOS the
                // option opens both.
                Ingress = new NetworkIngressPolicy
                {
                    Default = opt.HostLoopback && OperatingSystem.IsMacOS() ? NetworkAction.Allow : NetworkAction.Deny,
                    HostLoopback = opt.HostLoopback ? NetworkAction.Allow : NetworkAction.Deny,
                },
            },
            WorkingDirectory = opt.Workspace,
            Environment = environment,
            InheritDefaultEnvironment = true,
            TimeoutMs = TimeoutMs(),
        };

    // node's realpath lstat()s the drive root and fails with EPERM without it. Enumerate-only access lets
    // it read the root's attributes and child names without granting file contents.
    var driveRoot = Path.GetPathRoot(Environment.SystemDirectory)!;
    // Enumerate on the root lets every folder's names be listed (contents stay denied), so it is only
    // applied together with --allow-win32k, the mode for node-based tooling, unless explicitly
    // disabled. Keep an explicit ProcessContainer policy when omitting enumeration: Win32k UI
    // access and filesystem grants are independent of root-directory visibility.
    Containment containment = opt.NoEnumerateRoot
        ? new Containment.ProcessContainer { Filesystem = new ProcessContainerFilesystemPolicy() }
        : opt.AllowWin32k && SupportsEnumeratePaths()
            ? new Containment.ProcessContainer { Filesystem = new ProcessContainerFilesystemPolicy { EnumeratePaths = { driveRoot } } }
            : new Containment.Process();

    return new ContainerRequest(command)
    {
        Containment = containment,
        Filesystem = filesystem,
        Network = new NetworkPolicy
        {
            // MXC has no hostname allow-lists; egress is all-or-CIDR. Inbound is always closed.
            Egress = new NetworkEgressPolicy { Default = opt.DenyNet ? NetworkAction.Deny : NetworkAction.Allow },
            // --host-loopback: Python asyncio on Windows builds its self-pipe from a 127.0.0.1 socketpair,
            // which hangs under HostLoopback=Deny (microsoft/mxc#1261, #1505), so mcp-library stdio servers
            // never connect. Allowing it also lets the workload reach every host loopback port.
            Ingress = new NetworkIngressPolicy
            {
                Default = NetworkAction.Deny,
                HostLoopback = opt.HostLoopback ? NetworkAction.Allow : NetworkAction.Deny,
            },
        },
        // Default UiPolicy blocks Win32k syscalls, which makes user32.dll fail to initialise
        // (0xC0000142) in node, npx, uv and pwsh. Opt-in only; clipboard and input injection stay blocked.
        Ui = opt.AllowWin32k ? new UiPolicy { Disable = false } : null,
        WorkingDirectory = opt.Workspace,
        Environment = environment,
        InheritDefaultEnvironment = true,
        TimeoutMs = TimeoutMs(),
    };
}

// Runtimes used by stdio MCP servers and settings hooks: install folders are read-only,
// package caches are read-write because npx/uv download into them on first launch.
static (List<string> RuntimeDirs, Dictionary<string, string> Caches) RuntimeGrants(string stateRoot)
{
    var roaming = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);

    var runtimeDirs = new[] { "node", "npx", "uv", "uvx", "python", "python3", "pwsh" }
        .SelectMany(FindOnPath)
        // A symlinked launcher (/usr/local/bin/pwsh -> /opt/microsoft/powershell/7/pwsh) needs its target too.
        .SelectMany(p => new[] { p, ResolveLink(p) })
        .Select(p => Path.GetDirectoryName(p)!)
        .Append(Path.Combine(roaming, "npm"))
        .Append(Environment.GetEnvironmentVariable("UV_PYTHON_INSTALL_DIR") ?? Path.Combine(roaming, "uv", "python"))
        .Where(Directory.Exists)
        .Distinct(StringComparer.OrdinalIgnoreCase)
        .ToList();

    // Sandbox-private package caches (env var -> folder). Sharing the host caches fails: the container
    // cannot replace files the host already wrote there, and it would let sandboxed runs poison them.
    var caches = new Dictionary<string, string>
    {
        ["npm_config_cache"] = Path.Combine(stateRoot, "cache", "npm"),
        ["UV_CACHE_DIR"] = Path.Combine(stateRoot, "cache", "uv"),
        // The container's own %TEMP% is not writable for executables (uv's trampoline build fails there).
        ["TEMP"] = Path.Combine(stateRoot, "tmp"),
        ["TMP"] = Path.Combine(stateRoot, "tmp"),
    };
    // Bubblewrap otherwise sets HOME to the working directory, so the CLI and pwsh write
    // .cache/ into the workspace, where a `git add -A` commits it. A caller's --env HOME still wins.
    if (!OperatingSystem.IsWindows())
    {
        caches["HOME"] = Path.Combine(stateRoot, "posix-home");
        // TEMP/TMP are Windows names. POSIX tools read TMPDIR, and the claude CLI reads
        // CLAUDE_CODE_TMPDIR, falling back to /tmp/claude, which the sandbox cannot write.
        caches["TMPDIR"] = caches["TEMP"];
        caches["CLAUDE_CODE_TMPDIR"] = caches["TEMP"];
    }
    return (runtimeDirs, caches);
}

static IEnumerable<string> FindOnPath(string name)
{
    var extensions = OperatingSystem.IsWindows()
        ? (Environment.GetEnvironmentVariable("PATHEXT") ?? ".EXE;.CMD").Split(';').Prepend("")
        : [""];
    foreach (var dir in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries))
    {
        foreach (var ext in extensions)
        {
            var candidate = Path.Combine(dir.Trim(), name + ext);
            if (File.Exists(candidate)) yield return candidate;
        }
    }
}

static bool SupportsEnumeratePaths() =>
    MxcContainer.Probe(new ContainerRequest("cmd.exe /d /c rem")).Probes.BaseContainerSupportsEnumeratePaths;

static string ResolveLink(string path) =>
    File.ResolveLinkTarget(path, returnFinalTarget: true)?.FullName ?? path;

static void AddDistinct(List<string> target, IEnumerable<string> paths)
{
    foreach (var path in paths)
        if (!target.Contains(path, StringComparer.OrdinalIgnoreCase)) target.Add(path);
}

Dictionary<string, string> BaseEnvironment()
{
    var env = new Dictionary<string, string> { ["DISABLE_AUTOUPDATER"] = "1" };
    foreach (var name in new[] { "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY" })
    {
        var value = Environment.GetEnvironmentVariable(name);
        if (!string.IsNullOrEmpty(value)) env[name] = value;
    }
    return env;
}

void ApplyUserEnvironment(Dictionary<string, string> env)
{
    foreach (var (name, value) in opt.Env) env[name] = value;
}

string? AuthSource() =>
    new[] { "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN" }
        .FirstOrDefault(n => opt.Env.ContainsKey(n) || !string.IsNullOrEmpty(Environment.GetEnvironmentVariable(n)));

string ClaudeCommand(string exe) =>
    string.Join(' ', new[] { exe }.Concat(opt.ClaudeArgs).Select(OperatingSystem.IsWindows() ? QuoteArg : (Func<string, string>)QuotePosix));

// Copy the CLI under %ProgramData% (Users: read+execute by default) when missing or stale.
static string StageForAgentUser(string source)
{
    var target = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
        "mxc-sandbox", "bin", Path.GetFileName(source));
    var src = new FileInfo(source);
    var dst = new FileInfo(target);
    if (!dst.Exists || dst.Length != src.Length || dst.LastWriteTimeUtc < src.LastWriteTimeUtc)
    {
        Console.Error.WriteLine($"staging {source} -> {target}");
        Directory.CreateDirectory(dst.DirectoryName!);
        File.Copy(source, target, overwrite: true);
    }
    return target;
}

uint? TimeoutMs() => opt.TimeoutSec is { } s ? (uint)(s * 1000) : null;

// Bubblewrap starts bwrap with --die-with-parent (PR_SET_PDEATHSIG), and Linux treats the forking
// *thread* as the parent. MxcContainer.SpawnAsync forks from a thread-pool worker, so when the pool
// retires that worker the kernel SIGKILLs the sandbox mid-run (exit 255, seconds to minutes in).
// Spawn from a dedicated thread instead and park it until the caller sets `released`.
static Task<MxcProcess> SpawnPinnedAsync(ContainerRequest request, ManualResetEventSlim released)
{
    var spawned = new TaskCompletionSource<MxcProcess>(TaskCreationOptions.RunContinuationsAsynchronously);
    var thread = new Thread(() =>
    {
        MxcProcess process;
        try { process = MxcContainer.Spawn(request); }
        catch (Exception e) { spawned.SetException(e); return; }
        spawned.SetResult(process);
        released.Wait();
    }) { IsBackground = true, Name = "mxc-sandbox-parent" };
    thread.Start();
    return spawned.Task;
}

static int Finish(WaitResult result, IReadOnlyList<string> warnings)
{
    foreach (var warning in warnings) Console.Error.WriteLine($"mxc warning: {warning}");
    if (result.TimedOut)
    {
        Console.Error.WriteLine("workload timed out");
        return 124;
    }
    return result.ExitCode;
}

static int Usage(string message)
{
    Console.Error.WriteLine(message);
    return 2;
}

static async Task ForwardAsync(Stream? source, Stream destination)
{
    if (source is null) return;
    await source.CopyToAsync(destination);
    await destination.FlushAsync();
}

// POSIX shell-word quoting, which the Bubblewrap and Seatbelt backends use to split the command.
static string QuotePosix(string arg) =>
    arg.Length > 0 && arg.All(c => char.IsAsciiLetterOrDigit(c) || "-_./:=@%+,".Contains(c))
        ? arg : "'" + arg.Replace("'", "'\\''") + "'";

// Windows command-line quoting (CommandLineToArgvW rules).
static string QuoteArg(string arg)
{
    if (arg.Length > 0 && arg.IndexOfAny([' ', '\t', '"']) < 0) return arg;
    var sb = new StringBuilder("\"");
    var backslashes = 0;
    foreach (var c in arg)
    {
        if (c == '\\') { backslashes++; continue; }
        if (c == '"') sb.Append('\\', backslashes * 2 + 1).Append('"');
        else sb.Append('\\', backslashes).Append(c);
        backslashes = 0;
    }
    return sb.Append('\\', backslashes * 2).Append('"').ToString();
}

sealed record Options(
    string Workspace, bool Isolated, bool DenyNet, bool AllowWin32k,
    List<string> ReadOnly, List<string> ReadWrite, int? TimeoutSec, string[] ClaudeArgs,
    Dictionary<string, string> Env, bool HostLoopback = false, bool NoEnumerateRoot = false)
{
    public static Options Parse(string[] rest)
    {
        var o = new Options(Environment.CurrentDirectory, false, false, false, [], [], null, [], []);
        for (var i = 0; i < rest.Length; i++)
        {
            switch (rest[i])
            {
                case "--workspace": o = o with { Workspace = Path.GetFullPath(rest[++i]) }; break;
                case "--isolated": o = o with { Isolated = true }; break;
                case "--deny-net": o = o with { DenyNet = true }; break;
                case "--allow-win32k": o = o with { AllowWin32k = true }; break;
                case "--no-enumerate-root": o = o with { NoEnumerateRoot = true }; break;
                case "--ro": o.ReadOnly.Add(Path.GetFullPath(rest[++i])); break;
                case "--rw": o.ReadWrite.Add(Path.GetFullPath(rest[++i])); break;
                case "--timeout": o = o with { TimeoutSec = int.Parse(rest[++i]) }; break;
                case "--env": AddEnv(o.Env, rest[++i]); break;
                case "--host-loopback": o = o with { HostLoopback = true }; break;
                case "--": return o with { ClaudeArgs = rest[(i + 1)..] };
                default: return o with { ClaudeArgs = rest[i..] };
            }
        }
        return o;
    }

    static void AddEnv(Dictionary<string, string> env, string spec)
    {
        var eq = spec.IndexOf('=');
        var name = eq < 0 ? spec : spec[..eq];
        if (name.Length == 0) throw new ArgumentException($"--env needs NAME or NAME=VALUE, got '{spec}'");
        var value = eq < 0 ? Environment.GetEnvironmentVariable(name) : spec[(eq + 1)..];
        if (value is null) throw new ArgumentException($"--env {name}: not set on the host");
        env[name] = value;
    }
}
