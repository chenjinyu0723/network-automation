$ErrorActionPreference = "Stop"

Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Get-Command pnpm -ErrorAction SilentlyContinue)) {
  throw "pnpm was not found. Install Node.js LTS, run 'corepack enable', then retry."
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  throw "uv was not found. Install uv, then retry."
}

# Do not invoke apps/web/node_modules/.bin directly. In a pnpm workspace the
# package-local link layout is implementation detail and may not exist on a
# newly cloned machine. pnpm resolves the workspace package and its scripts.
& pnpm install --frozen-lockfile
if ($LASTEXITCODE -ne 0) { throw "pnpm dependency installation failed." }

& pnpm --filter network-automation-web run build
if ($LASTEXITCODE -ne 0) { throw "Frontend TypeScript/Vite build failed." }

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
