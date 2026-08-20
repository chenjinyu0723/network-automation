$ErrorActionPreference = "Stop"

Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
  throw "Node.js LTS was not found. Install Node.js LTS, then retry."
}

$pnpmCommand = @(Get-Command pnpm.cmd -CommandType Application -ErrorAction SilentlyContinue)[0]
$useNpxPnpm = $null -eq $pnpmCommand

if ($useNpxPnpm -and -not (Get-Command npx.cmd -CommandType Application -ErrorAction SilentlyContinue)) {
  throw "Neither pnpm.cmd nor npx.cmd was found. Reinstall Node.js LTS with npm/npx included, then retry."
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  throw "uv was not found. Install uv, then retry."
}

function Invoke-Pnpm {
  param([Parameter(ValueFromRemainingArguments = $true)][string[]]$PnpmArgs)

  if ($useNpxPnpm) {
    # npx downloads this exact pnpm version without Corepack or admin rights.
    & npx.cmd --yes pnpm@11.9.0 @PnpmArgs
  }
  else {
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
