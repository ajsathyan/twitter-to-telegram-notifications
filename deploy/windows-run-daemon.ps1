param(
    [string]$InstallDir = "C:\twitter-tg-notifs",
    [string]$ConfigPath = "C:\ProgramData\twitter-tg-notifs\config.toml",
    [string]$EnvPath = "C:\ProgramData\twitter-tg-notifs\.env",
    [string]$StateDb = "C:\ProgramData\twitter-tg-notifs\state.sqlite3",
    [string]$LogDir = "C:\ProgramData\twitter-tg-notifs\logs"
)

$ErrorActionPreference = "Stop"

$Executable = Join-Path $InstallDir ".venv\Scripts\twitter-tg-notifs.exe"
if (-not (Test-Path $Executable)) {
    throw "Cannot find $Executable. Create the venv and install the package first."
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir ("daemon-{0}.log" -f (Get-Date -Format "yyyyMMdd"))

& $Executable run `
    --config $ConfigPath `
    --env-file $EnvPath `
    --state-db $StateDb `
    *>> $LogFile

exit $LASTEXITCODE
