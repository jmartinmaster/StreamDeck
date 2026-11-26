# Windows Setup Guide

This branch (`StreamDeck-windows`) supports the Windows host over **USB Serial** and **Wi-Fi (TCP + mDNS)**, with native Win32 keyboard injection and active-window focus tracking.

## What works on Windows
* **Wi-Fi transport:** TCP port `4650` + `_mmdeckd._tcp` mDNS auto-discovery (`zeroconf`).
* **USB Serial transport:** Plug-and-play COM port auto-detection (`--port auto`, or explicit `--port COM4`).
* **Keyboard shortcuts (`keys`):** Native Win32 `SendInput` (no drivers or admin privileges needed).
* **Active window focus tracking:** Native Win32 foreground window polling (matches lowercase `.exe` stem and window title against `profiles.windows.toml`).
* **Shell commands (`shell`):** Detached background process launching via Windows `subprocess.Popen` flags.
* **Notification-area controller:** Starts with your Windows sign-in and provides start, stop, restart, and log controls for `deckd`.
* **Visual profile editor:** Available from **Open GUI** in the notification-area menu; edits profiles, shows daemon status, applies profile overrides, and controls USB/Wi-Fi transports.
* **Windows app launcher:** Discovers shortcuts in the user and all-users Start Menu folders; apps can be selected in the GUI or used in the device launcher.
* **Windows control channel:** The GUI communicates with `deckd` over a JSON API bound to `127.0.0.1:4651`; it is local-only and does not require a firewall rule.
* **Windows user data:** GUI settings and the optional unlock key use `%LOCALAPPDATA%\StreamDeck`.
* **Starter Windows profiles:** [`host/profiles.windows.toml`](../host/profiles.windows.toml) is loaded automatically on Windows.

## Not supported on Windows
* **BLE GATT server:** Linux BlueZ only for now.
* **Remote lock-screen unlock:** Blocked by Windows Secure Desktop (`Winlogon`).

Windows app IDs for launcher entries have the form `startmenu:user:...` or `startmenu:common:...`; the GUI writes these when an app is selected. Existing `shell = "..."` actions remain available for commands, URLs, and apps that are not in the Start Menu.

---

## Quick Start

### 1. Prerequisites
* **Python 3.11+** on Windows (`winget install Python.Python.3.12` or from python.org).
* *(For USB mode)* USB-UART driver if your board uses CP210x or CH340 (ESP32-S3 native USB CDC works with built-in Windows drivers).

### 2. Run with PowerShell
From the repository root:

```powershell
.\host\run-windows.ps1
```

On first run, this creates `host\.venv-win`, verifies Python 3.11+, installs the Windows dependencies, adds a StreamDeck shortcut to your user Startup folder, and launches the notification-area controller. The controller starts `deckd` with **USB (`--port auto`)** and **Wi-Fi (TCP 4650 + mDNS)** enabled. It starts automatically the next time you sign in. The script checks whether the daemon's local GUI-control listener opened; if not, it points to the log instead of claiming the host is running.

The GUI's Windows transport/profile controls become available once the updated daemon is running. If the daemon was already running during an update, restart it from the notification-area menu. If the tray controller itself changed, choose **Quit** from the tray and rerun the script so the updated controller is loaded.

Right-click the StreamDeck icon in the notification area to start, stop, restart, or quit the host, or open its log at `%LOCALAPPDATA%\StreamDeck\deckd.log`. Quitting stops the host for the current sign-in; it will start again at the next sign-in.

#### Useful options
```powershell
# Force a specific COM port
.\host\run-windows.ps1 --port COM4

# Wi-Fi only (leaves COM port free for PlatformIO flashing)
.\host\run-windows.ps1 --no-usb

# USB only
.\host\run-windows.ps1 --no-wifi

# Verbose logging (shows focus changes, COM port, and protocol traffic)
.\host\run-windows.ps1 --verbose

# Do not register automatic startup at sign-in
.\host\run-windows.ps1 -NoAutostart

# Install/update without launching the tray now
.\host\run-windows.ps1 -NoLaunch
```

Daemon flags are saved for future starts; running the script without flags resets them to the default USB + Wi-Fi configuration. `-NoAutostart` removes the Startup shortcut, while `-NoLaunch` only skips starting the tray for this run.

### 3. Allow Wi-Fi through Windows Firewall (one-time, Administrator PowerShell)
If the ESP32 panel cannot reach your PC over Wi-Fi, allow inbound TCP port `4650`:

```powershell
New-NetFirewallRule -DisplayName "StreamDeck deckd" -Direction Inbound -Protocol TCP -LocalPort 4650 -Action Allow -Profile Private
```

## Uninstall

From the repository root, run:

```powershell
.\host\uninstall-windows.ps1
```

The script stops `deckd` and the tray controller launched from this checkout, removes its Startup shortcut and `host\.venv-win`, and leaves the source files and `%LOCALAPPDATA%\StreamDeck` settings/logs in place. If the GUI is open, save any profile edits and close it before retrying.

To also delete the per-user settings, logs, and optional unlock key, explicitly run:

```powershell
.\host\uninstall-windows.ps1 -PurgeData
```

Use `-WhatIf` to preview the actions. A Windows Firewall rule added manually for Wi-Fi is not removed by the script.
