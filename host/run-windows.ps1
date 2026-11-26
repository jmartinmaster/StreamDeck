<#
.SYNOPSIS
    Bare-minimum Windows launcher for deckd (USB Serial + Wi-Fi TCP/mDNS).

.DESCRIPTION
    Creates host\.venv-win on first run, installs host\requirements-windows.txt,
    and starts deckd.py with USB serial auto-detection and Wi-Fi enabled.
    Any extra arguments are forwarded directly to deckd.py.

.EXAMPLE
    .\host\run-windows.ps1
    .\host\run-windows.ps1 --port COM4
    .\host\run-windows.ps1 --no-usb -v
#>

param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$DeckdArgs
)

$ErrorActionPreference = "Stop"

$HostDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir = Join-Path $HostDir ".venv-win"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$ReqFile = Join-Path $HostDir "requirements-windows.txt"
$DeckdPy = Join-Path $HostDir "deckd.py"

if (-not (Test-Path $VenvPython)) {
    Write-Host "Creating Windows virtual environment in $VenvDir ..." -ForegroundColor Cyan
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -m venv $VenvDir
    } else {
        & python -m venv $VenvDir
    }
    & $VenvPython -m pip install --upgrade pip
    & $VenvPython -m pip install -r $ReqFile
}

Write-Host ""
Write-Host "Starting deckd (USB + Wi-Fi on TCP 4650 / mDNS _mmdeckd._tcp)..." -ForegroundColor Green
Write-Host "Tip: If Wi-Fi cannot connect, allow inbound TCP 4650 in Windows Defender Firewall:" -ForegroundColor DarkGray
Write-Host '     New-NetFirewallRule -DisplayName "StreamDeck deckd" -Direction Inbound -Protocol TCP -LocalPort 4650 -Action Allow' -ForegroundColor DarkGray
Write-Host ""

& $VenvPython $DeckdPy @DeckdArgs
