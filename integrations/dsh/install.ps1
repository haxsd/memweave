<#
.SYNOPSIS
  Build the local DeepSeek Harness plugin that mounts MemWeave's stdio MCP server.

.DESCRIPTION
  Copies the plugin template from this directory into a user-owned install
  directory, writes config.json (python + vault paths), and prints the two
  remaining steps: install the Skill and register the bundle in DSH.

  DSH MCP servers run as Low-integrity child processes. If the shared vault is
  still Medium-integrity, MemWeave writes fail with LOCK_BUSY. Pass
  -SetVaultIntegrity to add the Low mandatory label to the vault; the script
  prints the exact command when the switch is omitted.

.PARAMETER Python
  Absolute path to the Python executable that has MemWeave installed.

.PARAMETER HomeDir
  Absolute path to the shared MemWeave vault (the same --home used by other clients).

.PARAMETER InstallDir
  Directory that will contain the DSH plugin package. Defaults to
  "%LOCALAPPDATA%\MemWeave\dsh-plugin".

.PARAMETER SetVaultIntegrity
  Adds a Low mandatory label to HomeDir recursively. Required for the current
  DSH sandbox; this is a security tradeoff documented in README.md.

.EXAMPLE
  .\install.ps1 -Python 'C:\src\memweave\.venv\Scripts\python.exe' -HomeDir "$env:USERPROFILE\.memweave" -SetVaultIntegrity
#>
param(
    [string]$Python = '',
    [string]$HomeDir = '',
    [string]$InstallDir = '',
    [switch]$SetVaultIntegrity
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if ([string]::IsNullOrWhiteSpace($Python)) {
    $Python = Join-Path $repoRoot '.venv\Scripts\python.exe'
}
if ([string]::IsNullOrWhiteSpace($HomeDir)) {
    $HomeDir = Join-Path $env:USERPROFILE '.memweave'
}
if ([string]::IsNullOrWhiteSpace($InstallDir)) {
    $InstallDir = Join-Path $env:LOCALAPPDATA 'MemWeave\dsh-plugin'
}

$Python = [System.IO.Path]::GetFullPath($Python)
$HomeDir = [System.IO.Path]::GetFullPath($HomeDir)
$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python executable not found: $Python"
}
if (-not (Test-Path -LiteralPath $HomeDir -PathType Container)) {
    throw "MemWeave home not found: $HomeDir. Run 'python -m memweave init --home <dir>' first."
}

$template = Join-Path $PSScriptRoot 'plugin'
foreach ($name in @('package.json', 'index.js', 'cordis.patch.yml')) {
    if (-not (Test-Path -LiteralPath (Join-Path $template $name) -PathType Leaf)) {
        throw "Plugin template file missing: $name"
    }
}

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
foreach ($name in @('package.json', 'index.js', 'cordis.patch.yml')) {
    Copy-Item -LiteralPath (Join-Path $template $name) -Destination (Join-Path $InstallDir $name) -Force
}

$config = [ordered]@{
    python           = $Python
    home             = $HomeDir
    toolCallTimeoutMs = 120000
    failOnStartupError = $false
}
$configJson = $config | ConvertTo-Json -Depth 4
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Join-Path $InstallDir 'config.json'), $configJson + "`n", $utf8NoBom)

if ($SetVaultIntegrity) {
    Write-Host 'Setting Low mandatory label on the shared vault (DSH sandbox requirement)...'
    & icacls $HomeDir /setintegritylevel '(OI)(CI)Low' /T | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "icacls failed with exit code $LASTEXITCODE"
    }
    & icacls $HomeDir | Select-Object -First 6
} else {
    Write-Host ''
    Write-Host 'NOTE: current DSH MCP servers run as Low-integrity child processes.' -ForegroundColor Yellow
    Write-Host 'If MemWeave tools return LOCK_BUSY, add the Low mandatory label to the vault:' -ForegroundColor Yellow
    Write-Host ('  icacls "{0}" /setintegritylevel (OI)(CI)Low /T' -f $HomeDir) -ForegroundColor Yellow
}

$linkSpec = 'link:' + ($InstallDir -replace '\\', '/')
Write-Host ''
Write-Host 'Plugin package prepared:' -ForegroundColor Green
Write-Host ("  InstallDir : {0}" -f $InstallDir)
Write-Host ("  Python     : {0}" -f $Python)
Write-Host ("  HomeDir    : {0}" -f $HomeDir)
Write-Host ''
Write-Host 'Next steps:'
Write-Host '  1) Install the always-on Skill into the DSH skill catalog:'
Write-Host ('     Copy-Item -Recurse -Force "{0}\src\memweave\skills\memweave-continuity\*" "$env:USERPROFILE\.agents\skills\memweave-continuity\"' -f $repoRoot)
Write-Host '  2) In a DSH session, ask the agent to install this bundle:'
Write-Host ('     plugin_manager action=install_bundle target="{0}"' -f $linkSpec)
Write-Host '     (or add the local link from the DSH plugin UI, if available)'
Write-Host '  3) Verify inside DSH: 22 mcp__memweave__* tools; memweave_system_status; workspace_prepare(resume).'
Write-Host '  4) The bundle can be removed with plugin_manager action=remove_bundle target="@local/dsh-memweave".'
