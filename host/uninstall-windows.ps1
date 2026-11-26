<#
.SYNOPSIS
    Stops and removes the Windows StreamDeck runtime installation.

.DESCRIPTION
    Stops deckd and the tray controller for this checkout, removes the
    user Startup shortcut and host\.venv-win. User data is preserved unless
    -PurgeData is specified.

.EXAMPLE
    .\host\uninstall-windows.ps1
    .\host\uninstall-windows.ps1 -PurgeData
    .\host\uninstall-windows.ps1 -WhatIf
#>

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "Medium")]
param(
    [switch]$PurgeData
)

$ErrorActionPreference = "Stop"

$HostDir = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$VenvDir = Join-Path $HostDir ".venv-win"
$TrayScript = Join-Path $HostDir "win_tray.py"
$VenvPythonw = Join-Path $VenvDir "Scripts\pythonw.exe"
$StartupShortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "StreamDeck.lnk"
if (-not $env:LOCALAPPDATA) {
    throw "LOCALAPPDATA is not set; cannot identify StreamDeck user data."
}
$AppDataDir = Join-Path $env:LOCALAPPDATA "StreamDeck"

function Get-StreamDeckProcesses {
    $scriptPaths = @(
        (Join-Path $HostDir "deckd.py"),
        $TrayScript,
        (Join-Path $HostDir "deckgui.py")
    )
    $records = @()
    $pythonProcesses = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'"
    foreach ($process in $pythonProcesses) {
        if (-not $process.CommandLine) { continue }
        foreach ($scriptPath in $scriptPaths) {
            if ($process.CommandLine.IndexOf($scriptPath, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
                $records += [pscustomobject]@{
                    ProcessId = [int]$process.ProcessId
                    ParentProcessId = [int]$process.ParentProcessId
                    Script = [IO.Path]::GetFileName($scriptPath)
                }
                break
            }
        }
    }
    return $records
}

$processes = @(Get-StreamDeckProcesses)
$guiProcesses = @($processes | Where-Object Script -eq "deckgui.py")
if ($guiProcesses.Count -gt 0) {
    $pids = ($guiProcesses.ProcessId -join ", ")
    throw "The StreamDeck GUI is still open (PID $pids). Save any profile edits, close the GUI, then run this script again."
}

foreach ($process in @($processes | Where-Object Script -eq "deckd.py")) {
    $target = "deckd.py process $($process.ProcessId)"
    if ($PSCmdlet.ShouldProcess($target, "Stop")) {
        Stop-Process -Id $process.ProcessId -ErrorAction SilentlyContinue
        Wait-Process -Id $process.ProcessId -Timeout 10 -ErrorAction SilentlyContinue
    }
}

foreach ($process in @($processes | Where-Object Script -eq "win_tray.py")) {
    $target = "tray controller process $($process.ProcessId)"
    if ($PSCmdlet.ShouldProcess($target, "Stop")) {
        Stop-Process -Id $process.ProcessId -ErrorAction SilentlyContinue
        Wait-Process -Id $process.ProcessId -Timeout 10 -ErrorAction SilentlyContinue
    }
}

if (-not $WhatIfPreference -and @(Get-StreamDeckProcesses).Count -gt 0) {
    $remaining = @(Get-StreamDeckProcesses)
    $remaining | Format-Table ProcessId, Script -AutoSize
    throw "StreamDeck processes are still running; close them before removing the virtual environment."
}

if (Test-Path -LiteralPath $StartupShortcut) {
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($StartupShortcut)
    $shortcutTargetsThisInstall = $false
    if ($shortcut.TargetPath) {
        $shortcutTargetsThisInstall =
            [IO.Path]::GetFullPath($shortcut.TargetPath) -eq [IO.Path]::GetFullPath($VenvPythonw)
    }
    if ($shortcut.Arguments -and $shortcut.Arguments.IndexOf($TrayScript, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
        $shortcutTargetsThisInstall = $true
    }
    if ($shortcutTargetsThisInstall) {
        if ($PSCmdlet.ShouldProcess($StartupShortcut, "Remove Startup shortcut")) {
            Remove-Item -LiteralPath $StartupShortcut -Force
        }
    } else {
        Write-Warning "Leaving $StartupShortcut in place because it does not point to this checkout."
    }
}

if (Test-Path -LiteralPath $VenvDir) {
    $venvItem = Get-Item -LiteralPath $VenvDir -Force
    if ($venvItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "The virtual environment path is a reparse point; refusing to remove it: $VenvDir"
    }
    $resolvedVenv = [IO.Path]::GetFullPath($venvItem.FullName)
    if ($resolvedVenv -ne [IO.Path]::GetFullPath($VenvDir)) {
        throw "Virtual environment path mismatch; refusing to remove $resolvedVenv."
    }
    if ($PSCmdlet.ShouldProcess($resolvedVenv, "Remove virtual environment")) {
        Remove-Item -LiteralPath $resolvedVenv -Recurse -Force
    }
}

if ($PurgeData -and (Test-Path -LiteralPath $AppDataDir)) {
    $appDataItem = Get-Item -LiteralPath $AppDataDir -Force
    if ($appDataItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "The user data path is a reparse point; refusing to remove it: $AppDataDir"
    }
    if ([IO.Path]::GetFullPath($appDataItem.FullName) -ne [IO.Path]::GetFullPath($AppDataDir)) {
        throw "User data path mismatch; refusing to remove $($appDataItem.FullName)."
    }
    if ($PSCmdlet.ShouldProcess("$AppDataDir (settings, logs, and optional unlock key)", "Remove user data")) {
        Remove-Item -LiteralPath $AppDataDir -Recurse -Force
    }
}

if ($WhatIfPreference) {
    Write-Host "WhatIf complete; no changes were made."
} else {
    Write-Host "StreamDeck runtime uninstalled for this checkout. User data was $(if ($PurgeData) { 'removed if present' } else { 'preserved' })."
    Write-Host "The source files remain in $HostDir; remove the checkout separately if desired."
}
