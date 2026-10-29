<#
.SYNOPSIS
  Build (when needed) and run the MxcClaude sandbox host.

.DESCRIPTION
  Copies the bundled .NET host source to ~/.mxc-sandbox/src (%USERPROFILE% on Windows), builds it
  into ~/.mxc-sandbox/bin when the source hash changed, then runs it with the given arguments.
  Windows uses ProcessContainer; Linux uses Bubblewrap (needs bwrap, slirp4netns, ip and iptables
  on PATH) and macOS uses Seatbelt. The plugin directory itself is never written to. Override the root with
  $env:MXC_SANDBOX_HOME (avoid %LOCALAPPDATA%: the container cannot write there).

.EXAMPLE
  & <plugin-root>/scripts/mxc.ps1 probe
  & <plugin-root>/scripts/mxc.ps1 run --workspace C:\repos\app -- -p "summarise this repo"
  & <plugin-root>/scripts/mxc.ps1 -Rebuild probe

.NOTES
  The `&` operator drops a bare `--`; `pwsh -File` keeps it. The host therefore treats the first
  argument that is not one of its own options as the start of claude's arguments, with or
  without `--`. Spell host options exactly.
#>
# No param() block on purpose: with one, `pwsh -File` binds `--` and `--flag` as PowerShell
# parameters ("parameter name '' is ambiguous"). Raw $args keeps the host's arguments intact.
$Rebuild = $args.Count -gt 0 -and $args[0] -eq '-Rebuild'
[string[]]$HostArgs = if ($Rebuild) { $args | Select-Object -Skip 1 } else { $args }
$ErrorActionPreference = 'Stop'

$onWindows = $IsWindows -or $PSVersionTable.PSEdition -eq 'Desktop'
$pluginHost = Join-Path (Join-Path $PSScriptRoot 'host') 'MxcClaude'
$userHome = if ($onWindows) { $env:USERPROFILE } else { $HOME }
$stateRoot = if ($env:MXC_SANDBOX_HOME) { $env:MXC_SANDBOX_HOME } else { Join-Path $userHome '.mxc-sandbox' }
$srcDir = Join-Path (Join-Path $stateRoot 'src') 'MxcClaude'
$binDir = Join-Path $stateRoot 'bin'
$exe = Join-Path $binDir $(if ($onWindows) { 'MxcClaude.exe' } else { 'MxcClaude' })
$stamp = Join-Path $binDir '.source-hash'

$sources = Get-ChildItem $pluginHost -File | Where-Object Extension -in '.cs', '.csproj' | Sort-Object Name
$hash = ($sources | ForEach-Object { (Get-FileHash $_.FullName -Algorithm SHA256).Hash }) -join ''
$current = [string](Get-Content $stamp -Raw -ErrorAction SilentlyContinue)

if ($Rebuild -or -not (Test-Path $exe) -or $current.Trim() -ne $hash) {
    # The SDK must be at least the project's TargetFramework major version (net10.0 -> 10).
    $csproj = $sources | Where-Object Extension -eq '.csproj' | Select-Object -First 1
    $needMajor = if ((Get-Content $csproj.FullName -Raw) -match '<TargetFramework>net(\d+)\.') { [int]$Matches[1] } else { 8 }
    if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) { throw "dotnet SDK not found on PATH (need .NET $needMajor or later)." }
    $sdks = & dotnet --list-sdks
    if (-not ($sdks | Where-Object { [int]($_ -split '\.')[0] -ge $needMajor })) {
        throw "No .NET $needMajor+ SDK installed (host targets net$needMajor.0; edit $($csproj.FullName) for an older SDK, 8 minimum). Found: $($sdks -join '; ')"
    }

    Write-Host "mxc-sandbox: building host into $binDir" -ForegroundColor DarkGray
    if (Test-Path $srcDir) { Remove-Item $srcDir -Recurse -Force }   # drop sources deleted from the plugin
    New-Item -ItemType Directory -Force $srcDir, $binDir | Out-Null
    $sources | Copy-Item -Destination $srcDir -Force
    $build = & dotnet build (Join-Path $srcDir 'MxcClaude.csproj') -c Release -o $binDir --nologo -v q 2>&1
    if ($LASTEXITCODE -ne 0) { $build | Write-Host; throw "dotnet build failed (exit $LASTEXITCODE)" }
    Set-Content $stamp $hash -NoNewline
}

# Outside Windows, start the framework-dependent host through dotnet. The apphost that dotnet
# build emits is only ad-hoc signed, which allowlisting tools such as Santa in Lockdown mode
# kill on launch, while the Microsoft-signed dotnet host is commonly allowed.
if ($onWindows) { & $exe @HostArgs } else { & dotnet (Join-Path $binDir 'MxcClaude.dll') @HostArgs }
exit $LASTEXITCODE
