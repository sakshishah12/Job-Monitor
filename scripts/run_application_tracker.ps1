$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $ProjectRoot "logs"
$LogPath = Join-Path $LogDir "application_tracker.log"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Set-Location $ProjectRoot

$Timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"[$Timestamp] Starting application tracker" | Tee-Object -FilePath $LogPath -Append

python application_tracker.py --days-back 2 --max-emails 50 --use-llm 2>&1 |
    Tee-Object -FilePath $LogPath -Append

$Timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"[$Timestamp] Finished application tracker" | Tee-Object -FilePath $LogPath -Append
