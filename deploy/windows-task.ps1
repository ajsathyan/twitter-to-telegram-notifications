param(
    [string]$InstallDir = "C:\twitter-tg-notifs",
    [string]$ConfigPath = "C:\ProgramData\twitter-tg-notifs\config.toml",
    [string]$EnvPath = "C:\ProgramData\twitter-tg-notifs\.env",
    [string]$StateDb = "C:\ProgramData\twitter-tg-notifs\state.sqlite3",
    [string]$TaskName = "TwitterTgNotifs"
)

$ErrorActionPreference = "Stop"

$Executable = Join-Path $InstallDir ".venv\Scripts\twitter-tg-notifs.exe"
if (-not (Test-Path $Executable)) {
    throw "Cannot find $Executable. Create the venv and install the package first."
}
if (-not (Test-Path $ConfigPath)) {
    throw "Cannot find config file: $ConfigPath"
}
if (-not (Test-Path $EnvPath)) {
    throw "Cannot find env file: $EnvPath"
}

$StateDir = Split-Path -Parent $StateDb
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

$Arguments = @(
    "run",
    "--config", "`"$ConfigPath`"",
    "--env-file", "`"$EnvPath`"",
    "--state-db", "`"$StateDb`""
) -join " "

$Action = New-ScheduledTaskAction -Execute $Executable -Argument $Arguments -WorkingDirectory $InstallDir
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Runs the X/Twitter to Telegram notification daemon." `
    -Force | Out-Null

Write-Host "Registered scheduled task: $TaskName"
Write-Host "Start it now with: Start-ScheduledTask -TaskName $TaskName"
