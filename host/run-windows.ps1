<#
.SYNOPSIS
    Installs and starts the Windows StreamDeck notification-area controller.

.DESCRIPTION
    Creates host\.venv-win when needed, installs host\requirements-windows.txt,
    registers the controller to start at user sign-in, and starts it now.
    Any extra arguments are forwarded to deckd.py and saved for future starts.

.EXAMPLE
    .\host\run-windows.ps1
    .\host\run-windows.ps1 --port COM4
    .\host\run-windows.ps1 --no-usb --verbose
    .\host\run-windows.ps1 -NoAutostart
    .\host\run-windows.ps1 -NoLaunch
#>

param(
    [switch]$NoAutostart,
    [switch]$NoLaunch,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$DeckdArgs
)

$ErrorActionPreference = "Stop"

$HostDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir = Join-Path $HostDir ".venv-win"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$VenvPythonw = Join-Path $VenvDir "Scripts\pythonw.exe"
$ReqFile = Join-Path $HostDir "requirements-windows.txt"
$TrayPy = Join-Path $HostDir "win_tray.py"
$AppDir = Join-Path $env:LOCALAPPDATA "StreamDeck"
$StartupDir = [Environment]::GetFolderPath("Startup")
$StartupShortcut = Join-Path $StartupDir "StreamDeck.lnk"

if (-not (Test-Path $VenvPython)) {
    Write-Host "Creating Windows virtual environment in $VenvDir ..." -ForegroundColor Cyan
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -m venv $VenvDir
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -m venv $VenvDir
    } else {
        throw "Python 3.11 or newer was not found. Install Python and run this script again."
    }
    if ($LASTEXITCODE -ne 0) { throw "Could not create the Python virtual environment." }
}

$PythonVersion = (& $VenvPython -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
if ($LASTEXITCODE -ne 0) { throw "Could not run Python from the Windows virtual environment." }
if ([version]$PythonVersion -lt [version]"3.11") {
    throw "Python 3.11 or newer is required; the virtual environment uses Python $PythonVersion."
}

& $VenvPython -m pip install -r $ReqFile
if ($LASTEXITCODE -ne 0) { throw "Could not install Windows dependencies." }

New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
$SavedArgsPath = Join-Path $AppDir "deckd-args.json"
if ($DeckdArgs -and $DeckdArgs.Count -gt 0) {
    $SavedArgs = ConvertTo-Json -InputObject @($DeckdArgs) -Compress
} else {
    $SavedArgs = "[]"
}
Set-Content -Path $SavedArgsPath -Value $SavedArgs -Encoding utf8

if ($NoAutostart) {
    if (Test-Path $StartupShortcut) {
        Remove-Item -LiteralPath $StartupShortcut
    }
} else {
    $WshShell = New-Object -ComObject WScript.Shell
    $Shortcut = $WshShell.CreateShortcut($StartupShortcut)
    $Shortcut.TargetPath = $VenvPythonw
    $Shortcut.Arguments = "`"$TrayPy`""
    $Shortcut.WorkingDirectory = $HostDir
    $Shortcut.Description = "StreamDeck Windows tray controller"
    $Shortcut.Save()
}

if (-not $NoLaunch) {
    $Process = Start-Process -FilePath $VenvPythonw `
        -ArgumentList "`"$TrayPy`"" `
        -WorkingDirectory $HostDir `
        -PassThru

    $ControlReady = $false
    for ($Attempt = 0; $Attempt -lt 15; $Attempt++) {
        $Listener = Get-NetTCPConnection -LocalAddress "127.0.0.1" `
            -LocalPort 4651 -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($Listener) {
            $ControlReady = $true
            break
        }
        $Process.Refresh()
        if ($Process.HasExited) { break }
        Start-Sleep -Seconds 1
    }

    if ($ControlReady) {
        Write-Host "StreamDeck's local control service is listening on 127.0.0.1:4651." -ForegroundColor Green
        Write-Host "If the tray was already open during this update, use Restart to reload deckd; if win_tray.py changed, choose Quit and run this script again."
    } else {
        Write-Warning "The StreamDeck daemon did not open its local control service. Check $AppDir\deckd.log. If the tray was already running, choose Restart from its menu; if the controller itself was updated, choose Quit and run this installer again."
    }
    if (-not $NoAutostart) {
        Write-Host "The tray controller is registered to start at sign-in." -ForegroundColor Green
    }
    Write-Host "Use the tray icon to start, stop, restart, or open the GUI. Log: $AppDir\deckd.log"
} else {
    Write-Host "Dependencies and startup settings are updated. The tray was not launched (-NoLaunch)." -ForegroundColor Cyan
}
