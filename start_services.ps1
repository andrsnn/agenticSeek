# AgenticSeek Service Manager (Windows PowerShell)
# Usage:
#   .\start_services.ps1           # Start all services (full mode)
#   .\start_services.ps1 start     # Same as above
#   .\start_services.ps1 stop      # Stop all services
#   .\start_services.ps1 restart   # Restart all services
#   .\start_services.ps1 logs      # Follow all logs
#   .\start_services.ps1 logs backend   # Follow backend logs only
#   .\start_services.ps1 status    # Show container status

param(
  [Parameter(Position = 0)]
  [string]$Action = "start",

  [Parameter(Position = 1)]
  [string]$Service = ""
)

$ErrorActionPreference = "Stop"

# Always run from repo root
Set-Location -LiteralPath $PSScriptRoot

# ───────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────
function Write-Err($msg) { Write-Host "[ERROR] $msg" -ForegroundColor Red; exit 1 }
function Write-Ok($msg)  { Write-Host "[OK] $msg" -ForegroundColor Green }
function Write-Info($msg){ Write-Host $msg -ForegroundColor Cyan }

function Check-Docker {
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Err "Docker not found. Install Docker Desktop: https://docs.docker.com/desktop/install/windows-install/"
  }
  try { docker info 2>$null | Out-Null }
  catch { Write-Err "Docker is not running. Start Docker Desktop first." }

  try { docker compose version 2>$null | Out-Null }
  catch { Write-Err "docker compose not available. Update Docker Desktop." }

  if (-not (Test-Path ".env")) {
    Write-Err ".env file not found. Run: copy .env.example .env"
  }
}

function New-Secret {
  $bytes = New-Object byte[] 32
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  $rng.GetBytes($bytes)
  $rng.Dispose()
  return (-join ($bytes | ForEach-Object { $_.ToString("x2") }))
}

function Compose {
  param([string[]]$ComposeArgs)
  Write-Host "> docker compose --env-file .env $($ComposeArgs -join ' ')" -ForegroundColor DarkGray
  & docker compose --env-file .env @ComposeArgs
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

# ───────────────────────────────────────────────────────────────
# Commands
# ───────────────────────────────────────────────────────────────
function Do-Start {
  $env:SEARXNG_SECRET_KEY = New-Secret
  $env:DOCKER_BUILDKIT = "1"

  Write-Info "Starting AgenticSeek services..."

  # Start backend first for faster UI response
  Compose @("up", "-d", "--no-build", "backend")
  Start-Sleep -Seconds 3

  # Start remaining services
  Compose @("--profile", "full", "up", "-d", "--no-build")

  Write-Host ""
  Write-Ok "Services started!"
  Write-Host ""
  Write-Host "  Frontend:  http://localhost:3000" -ForegroundColor Yellow
  Write-Host "  Backend:   http://localhost:7777" -ForegroundColor Yellow
  Write-Host ""
  Write-Host "Tip: Run '.\start_services.ps1 logs' to follow logs" -ForegroundColor DarkGray
  Write-Host ""

  Compose @("ps")
}

function Do-Stop {
  Write-Info "Stopping services..."
  Compose @("stop")
  Write-Ok "Stopped."
}

function Do-Restart {
  Write-Info "Stopping services..."
  Compose @("down")

  Write-Info "Starting services with fresh containers..."
  $env:SEARXNG_SECRET_KEY = New-Secret
  $env:DOCKER_BUILDKIT = "1"

  Compose @("up", "-d", "--force-recreate", "backend")
  Start-Sleep -Seconds 3
  Compose @("--profile", "full", "up", "-d", "--force-recreate")

  Write-Host ""
  Write-Ok "Services restarted!"
  Write-Host ""
  Write-Host "  Frontend:  http://localhost:3000" -ForegroundColor Yellow
  Write-Host "  Backend:   http://localhost:7777" -ForegroundColor Yellow
  Write-Host ""

  Compose @("ps")
}

function Do-Status {
  Compose @("ps")
}

function Do-Logs([string]$svc) {
  if ([string]::IsNullOrWhiteSpace($svc)) {
    Compose @("logs", "-f", "--tail=100")
  } else {
    Compose @("logs", "-f", "--tail=100", $svc)
  }
}

# ───────────────────────────────────────────────────────────────
# Main
# ───────────────────────────────────────────────────────────────
Check-Docker

switch ($Action.ToLower()) {
  "start"   { Do-Start }
  "full"    { Do-Start }  # alias
  "stop"    { Do-Stop }
  "restart" { Do-Restart }
  "status"  { Do-Status }
  "logs"    { Do-Logs $Service }
  "ps"      { Do-Status }  # alias
  default   { Do-Start }   # default to start
}
