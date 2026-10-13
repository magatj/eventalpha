<#
.SYNOPSIS
  EventAlpha local helper for Windows (PowerShell 5.1+ / 7+).

.EXAMPLE
  .\scripts\eventalpha.ps1 setup        # first-time setup
  .\scripts\eventalpha.ps1 start        # db + migrations + api + worker + scheduler, then Vite
  .\scripts\eventalpha.ps1 stop
  .\scripts\eventalpha.ps1 migrate
  .\scripts\eventalpha.ps1 test
  .\scripts\eventalpha.ps1 reset-demo
  .\scripts\eventalpha.ps1 seed-demo
  .\scripts\eventalpha.ps1 logs [service]
  .\scripts\eventalpha.ps1 frontend     # Vite dev server only (http://localhost:5173)
  .\scripts\eventalpha.ps1 tf-validate  # terraform fmt/validate in Docker (never applies)
#>
param(
  [Parameter(Position = 0)][ValidateSet("setup", "start", "stop", "migrate", "test", "reset-demo", "seed-demo", "logs", "frontend", "tf-validate", "status")]
  [string]$Command = "status",
  [Parameter(Position = 1)][string]$Arg = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Need($cmd, $hint) {
  if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { throw "$cmd not found. $hint" }
}

function Wait-Api {
  Write-Host "Waiting for API on http://localhost:8000 ..." -NoNewline
  for ($i = 0; $i -lt 90; $i++) {
    try { $r = Invoke-RestMethod -Uri "http://localhost:8000/api/health" -TimeoutSec 2; if ($r.status) { Write-Host " ok"; return } } catch {}
    Start-Sleep -Seconds 2; Write-Host "." -NoNewline
  }
  throw "API did not become healthy. Check: .\scripts\eventalpha.ps1 logs api"
}

switch ($Command) {
  "setup" {
    Need docker "Install Docker Desktop and start it."
    Need node "Install Node.js 20+ (https://nodejs.org)."
    if (-not (Test-Path ".env")) {
      Copy-Item ".env.example" ".env"
      Write-Host "Created .env from .env.example (demo mode). Add API keys there for live mode." -ForegroundColor Yellow
    }
    docker compose build
    Push-Location frontend; npm install --no-audit --no-fund; Pop-Location
    Write-Host "Setup complete. Next: .\scripts\eventalpha.ps1 start" -ForegroundColor Green
  }
  "start" {
    docker compose up -d --build db migrate api worker scheduler
    Wait-Api
    Write-Host "Backend:  http://localhost:8000   API docs: http://localhost:8000/docs" -ForegroundColor Green
    Write-Host "Starting frontend (Ctrl+C stops only the frontend)..." -ForegroundColor Green
    Push-Location frontend; try { npm run dev } finally { Pop-Location }
  }
  "frontend" { Push-Location frontend; try { npm run dev } finally { Pop-Location } }
  "stop" { docker compose --profile frontend down }
  "migrate" { docker compose run --rm migrate }
  "test" {
    docker compose up -d db
    docker compose run --rm --no-deps -e TEST_DATABASE_URL=postgresql+psycopg://eventalpha:eventalpha_local_dev@db:5432/eventalpha_test api sh -c "ruff check app tests && pytest -q"
    if ($LASTEXITCODE -ne 0) { throw "backend tests failed" }
    Push-Location frontend; try { npm run lint; npm run typecheck; npm test } finally { Pop-Location }
  }
  "reset-demo" { Invoke-RestMethod -Method Post -Uri "http://localhost:8000/api/demo/reset" | ConvertTo-Json }
  "seed-demo" {
    Invoke-RestMethod -Method Put -Uri "http://localhost:8000/api/settings" -ContentType "application/json" -Body '{"data_mode":"demo"}' | Out-Null
    $job = Invoke-RestMethod -Method Post -Uri "http://localhost:8000/api/discovery/run"
    Write-Host "Demo discovery job queued: $($job.job_id) (takes ~1 minute)"
  }
  "logs" { if ($Arg) { docker compose logs -f --tail 200 $Arg } else { docker compose logs -f --tail 100 } }
  "tf-validate" {
    $tf = "hashicorp/terraform:1.10.5"
    $mount = "$((Resolve-Path infra/terraform).Path):/tf"
    docker run --rm -v $mount $tf -chdir=/tf fmt -recursive -check
    foreach ($e in "dev", "prod") {
      docker run --rm -v $mount $tf "-chdir=/tf/environments/$e" init -backend=false -input=false | Out-Null
      docker run --rm -v $mount $tf "-chdir=/tf/environments/$e" validate
    }
  }
  "status" { docker compose ps; try { Invoke-RestMethod http://localhost:8000/api/health | ConvertTo-Json } catch { Write-Host "API not reachable" } }
}
