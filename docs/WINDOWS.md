# Windows Setup Guide (Bare-Minimum USB + Wi-Fi)

This branch (`StreamDeck-windows`) adds bare-minimum Windows host support for `deckd` over **USB Serial** and **Wi-Fi (TCP + mDNS)** with native Win32 keyboard injection and active-window focus tracking.

## What works on Windows
* **Wi-Fi transport:** TCP port `4650` + `_mmdeckd._tcp` mDNS auto-discovery (`zeroconf`).
* **USB Serial transport:** Plug-and-play COM port auto-detection (`--port auto`, or explicit `--port COM4`).
* **Keyboard shortcuts (`keys`):** Native Win32 `SendInput` (no drivers or admin privileges needed).
* **Active window focus tracking:** Native Win32 foreground window polling (matches lowercase `.exe` stem and window title against `profiles.windows.toml`).
* **Shell commands (`shell`):** Detached background process launching via Windows `subprocess.Popen` flags.
* **Starter Windows profiles:** [`host/profiles.windows.toml`](../host/profiles.windows.toml) is loaded automatically on Windows.

## Not yet included in bare-minimum Windows
* **BLE GATT server:** Linux BlueZ only for now.
* **System tray (`decktray`) & D-Bus IPC:** Not needed for CLI launch; `deckd` starts with `--usb` and `--wifi` enabled by default on Windows.
* **Remote lock-screen unlock:** Blocked by Windows Secure Desktop (`Winlogon`).
* **Linux `.desktop` App Launcher discovery:** Use `shell = "..."` buttons in `profiles.windows.toml` instead.

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

On first run, this creates `host\.venv-win`, installs `pyserial` and `zeroconf`, and starts `deckd.py` with both **USB (`--port auto`)** and **Wi-Fi (TCP 4650 + mDNS)** enabled.

#### Useful options
```powershell
# Force a specific COM port
.\host\run-windows.ps1 --port COM4

# Wi-Fi only (leaves COM port free for PlatformIO flashing)
.\host\run-windows.ps1 --no-usb

# USB only
.\host\run-windows.ps1 --no-wifi

# Verbose logging (shows focus changes, COM port, and protocol traffic)
.\host\run-windows.ps1 -v
```

### 3. Allow Wi-Fi through Windows Firewall (one-time, Administrator PowerShell)
If the ESP32 panel cannot reach your PC over Wi-Fi, allow inbound TCP port `4650`:

```powershell
New-NetFirewallRule -DisplayName "StreamDeck deckd" -Direction Inbound -Protocol TCP -LocalPort 4650 -Action Allow -Profile Private
```
