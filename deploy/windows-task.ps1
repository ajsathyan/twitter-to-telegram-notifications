param(
    [string]$InstallDir = "C:\twitter-tg-notifs",
    [string]$ConfigPath = "C:\ProgramData\twitter-tg-notifs\config.toml",
    [string]$EnvPath = "C:\ProgramData\twitter-tg-notifs\.env",
    [string]$StateDb = "C:\ProgramData\twitter-tg-notifs\state.sqlite3",
    [string]$LogDir = "C:\ProgramData\twitter-tg-notifs\logs",
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

$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$ConfigDir = Split-Path -Parent $ConfigPath
$EnvDir = Split-Path -Parent $EnvPath
$StateDir = Split-Path -Parent $StateDb
$WritableDirs = @($ConfigDir, $EnvDir, $StateDir, $LogDir) | Select-Object -Unique
foreach ($Dir in $WritableDirs) {
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    icacls $Dir /grant "${CurrentUser}:(OI)(CI)M" /T | Out-Null
}

$Arguments = @(
    "-ExecutionPolicy", "Bypass",
    "-File", "`"$(Join-Path $InstallDir 'deploy\windows-run-daemon.ps1')`"",
    "-InstallDir", "`"$InstallDir`"",
    "-ConfigPath", "`"$ConfigPath`"",
    "-EnvPath", "`"$EnvPath`"",
    "-StateDb", "`"$StateDb`"",
    "-LogDir", "`"$LogDir`""
) -join " "

$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $Arguments -WorkingDirectory $InstallDir
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
Write-Host "Logs: $LogDir"
