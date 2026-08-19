$ErrorActionPreference = "Stop"

Set-Location (Split-Path -Parent $PSScriptRoot)

Push-Location apps/web
try {
  & .\node_modules\.bin\tsc.cmd -b
  if ($LASTEXITCODE -ne 0) { throw "TypeScript build failed." }
  & .\node_modules\.bin\vite.cmd build
  if ($LASTEXITCODE -ne 0) { throw "Vite build failed." }
}
finally {
  Pop-Location
}

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
