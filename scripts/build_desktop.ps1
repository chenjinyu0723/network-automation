$ErrorActionPreference = "Stop"

Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
  throw "Node.js LTS was not found. Install Node.js LTS, then retry."
}

$nodeHelp = & node --help 2>&1
if ($nodeHelp -match '(?m)^\s*--use-system-ca') {
  if ([string]::IsNullOrWhiteSpace($env:NODE_OPTIONS)) {
    $env:NODE_OPTIONS = "--use-system-ca"
  }
  elseif ($env:NODE_OPTIONS -notmatch '(^|\s)--use-system-ca(\s|$)') {
    $env:NODE_OPTIONS = "$env:NODE_OPTIONS --use-system-ca"
  }
}

$pnpmCommand = $null
foreach ($candidate in @(Get-Command pnpm.cmd -CommandType Application -ErrorAction SilentlyContinue)) {
  $pnpmText = Get-Content -LiteralPath $candidate.Source -Raw -ErrorAction SilentlyContinue
  if ($pnpmText -notmatch '(?i)corepack') {
    $pnpmCommand = $candidate
    break
  }
}

$useNpmExec = $null -eq $pnpmCommand -or $env:NETWORK_AUTOMATION_FORCE_NPM_EXEC -eq "1"

if ($useNpmExec -and -not (Get-Command npm.cmd -CommandType Application -ErrorAction SilentlyContinue)) {
  throw "Neither a non-Corepack pnpm.cmd nor npm.cmd was found. Reinstall Node.js LTS with npm included, then retry."
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  throw "uv was not found. Install uv, then retry."
}

function Invoke-Pnpm {
  param([Parameter(ValueFromRemainingArguments = $true)][string[]]$PnpmArgs)

  if ($useNpmExec) {
    # npm exec runs pnpm directly and never dispatches through a Corepack shim.
    Write-Host "Using npm exec pnpm@11.9.0"
    & npm.cmd exec --yes --package=pnpm@11.9.0 -- pnpm @PnpmArgs
  }
  else {
    Write-Host "Using pnpm at $($pnpmCommand.Source)"
    & $pnpmCommand.Source @PnpmArgs
  }

  if ($LASTEXITCODE -ne 0) {
    throw "pnpm command failed: $($PnpmArgs -join ' ')"
  }
}

# Do not invoke apps/web/node_modules/.bin directly. In a pnpm workspace the
# package-local link layout is implementation detail and may not exist on a
# newly cloned machine. pnpm resolves the workspace package and its scripts.
Invoke-Pnpm install --frozen-lockfile
Invoke-Pnpm --filter network-automation-web run build

uv sync --extra dev --extra desktop
uv run --extra desktop pyinstaller --noconfirm --clean --windowed --onedir `
  --name NetworkAutomation `
  --paths apps/api `
  --add-data "apps/web/dist;web_dist" `
  --collect-all webview `
  --distpath release `
  --workpath build/pyinstaller `
  apps/api/app/desktop.py

Write-Host "Desktop application built at release\NetworkAutomation\NetworkAutomation.exe"
