$ErrorActionPreference = "Stop"

Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Get-Command pnpm -ErrorAction SilentlyContinue)) {
  throw "未找到 pnpm。请先安装 Node.js LTS，并执行 corepack enable 后重试。"
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  throw "未找到 uv。请先安装 uv 后重试。"
}

# Do not invoke apps/web/node_modules/.bin directly. In a pnpm workspace the
# package-local link layout is implementation detail and may not exist on a
# newly cloned machine. pnpm resolves the workspace package and its scripts.
& pnpm install --frozen-lockfile
if ($LASTEXITCODE -ne 0) { throw "pnpm 依赖安装失败。" }

& pnpm --filter network-automation-web run build
if ($LASTEXITCODE -ne 0) { throw "前端 TypeScript/Vite 构建失败。" }

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
