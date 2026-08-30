# Publish crawl progress from THIS Windows box, until the Lightsail instance
# takes over. Same projection as the systemd timer -- status only.
#
#   .\deploy\publish-windows.ps1              # publish once
#   .\deploy\publish-windows.ps1 -DryRun      # print what would be sent
#   .\deploy\publish-windows.ps1 -Every 60    # keep publishing every 60s
#
# Reads the endpoint and token from .env in the repo root, which is
# gitignored. Keeping them in a file rather than on the command line means the
# token stays out of your shell history and out of `ps`-style process listings
# -- the same reason the loader was changed to take Neo4j creds from the
# environment.
#
# ASCII only, deliberately: PowerShell 5.1 reads a BOM-less .ps1 as ANSI, so a
# stray smart quote from a UTF-8 dash ends a string early.

[CmdletBinding()]
param(
  [switch]$DryRun,
  [int]$Every = 0,
  [string]$Source = 'windows'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Repo = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $Repo '.env'
$Py = 'C:\Users\stock\AppData\Local\Python\pythoncore-3.12-64\python.exe'
if (-not (Test-Path $Py)) { $Py = 'python' }

if (-not $DryRun) {
  if (-not (Test-Path $EnvFile)) {
    Write-Host "no $EnvFile" -ForegroundColor Red
    Write-Host 'Create it with these two lines (no quotes, no spaces around =):'
    Write-Host ''
    Write-Host '  CATTLE_ENDPOINT=https://cattle-registrations.netlify.app/api/publish'
    Write-Host '  CATTLE_INGEST_TOKEN=<the token you set in Netlify>'
    exit 1
  }
  foreach ($line in Get-Content $EnvFile) {
    if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
    $k, $v = $line -split '=', 2
    Set-Item -Path ("env:" + $k.Trim()) -Value $v.Trim()
  }
}

$env:APP_DIR = $Repo
$script = Join-Path $Repo 'deploy\publish_status.py'

function Publish {
  $publishArgs = @($script, '--source', $Source)
  if ($DryRun) { $publishArgs += '--dry-run' }
  & $Py @publishArgs
  if ($LASTEXITCODE -ne 0) { Write-Host "publish exited $LASTEXITCODE" -ForegroundColor Yellow }
}

if ($Every -gt 0) {
  Write-Host "publishing every ${Every}s -- Ctrl+C to stop"
  while ($true) { Publish; Start-Sleep -Seconds $Every }
} else {
  Publish
}
