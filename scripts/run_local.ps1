<#
Runs the bot on this machine as a long-lived process.

Everything stays local: SQLite on disk, Telegram long polling (no public URL
needed), and the dashboard on http://localhost:<port>/dashboard. If uvicorn
exits for any reason the loop restarts it, so a crash or a dropped network
connection does not end the bot until the task itself is stopped.

Run directly to test, or install it to start with Windows:
  powershell -ExecutionPolicy Bypass -File scripts\run_local.ps1
  powershell -ExecutionPolicy Bypass -File scripts\install_startup_task.ps1
#>
[CmdletBinding()]
param(
    [int]$Port = 8010,
    [int]$RestartDelaySeconds = 5
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$logDir = Join-Path $repo "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

function Write-Log([string]$message) {
    $line = "{0} [run_local] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $message
    Write-Output $line
    Add-Content -Path (Join-Path $logDir "supervisor.log") -Value $line -Encoding utf8
}

if (-not (Test-Path (Join-Path $repo ".env"))) {
    Write-Log "No .env found. Copy .env.example to .env and fill it in first."
    exit 1
}

# app.main loads .env itself; this only checks the values it cannot start without
# so a missing secret fails here with a clear message instead of in a restart loop.
$envValues = @{}
foreach ($line in Get-Content (Join-Path $repo ".env")) {
    if ($line -match '^\s*#') { continue }
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
        $envValues[$matches[1]] = $matches[2].Trim().Trim('"')
    }
}
# Mirrors app.config.validate_settings: with TELEGRAM_MODE=off the dashboard is
# the only interface, so it needs a password but no bot token, and each
# dashboard user supplies their own AI key in the UI.
$mode = if ($envValues["TELEGRAM_MODE"]) { $envValues["TELEGRAM_MODE"].ToLower() } else { "polling" }
$required = @("ENCRYPTION_KEY")
if ($mode -eq "off") {
    $required += "DASHBOARD_PASSWORD"
} else {
    $provider = if ($envValues["AI_PROVIDER"]) { $envValues["AI_PROVIDER"] } else { "deepseek" }
    $providerKey = switch ($provider) {
        "openai" { "OPENAI_API_KEY" }
        "gemini" { "GEMINI_API_KEY" }
        default  { "DEEPSEEK_API_KEY" }
    }
    $required += @("TELEGRAM_SOLVER_BOT_TOKEN", "TELEGRAM_OWNER_ID", $providerKey)
}
$missing = $required | Where-Object { -not $envValues[$_] }
if ($missing) {
    Write-Log ("Missing required .env values: " + ($missing -join ", "))
    exit 1
}

$python = (Get-Command python).Source
Write-Log "Starting on port $Port using $python"

while ($true) {
    $stamp = Get-Date -Format "yyyy-MM-dd"
    $log = Join-Path $logDir "bot-$stamp.log"
    # cmd.exe does the redirection on purpose. Redirecting a native command's
    # stderr inside PowerShell 5.1 wraps each line in an ErrorRecord, and
    # uvicorn logs to stderr, so with -ErrorAction Stop its first log line
    # would terminate this script instead of being written to the file.
    $quoted = '"{0}" -m uvicorn app.main:app --host 127.0.0.1 --port {1} >> "{2}" 2>&1' -f $python, $Port, $log
    & cmd.exe /c $quoted
    $code = $LASTEXITCODE
    Write-Log "uvicorn exited with code $code; restarting in ${RestartDelaySeconds}s (log: $log)"
    Start-Sleep -Seconds $RestartDelaySeconds
}
