# Context-Aware Stream Deck System

An open-source, wireless, context-aware macro pad and Stream Deck system designed for Linux workstations and embedded hardware (ESP32 / ESP32-S3).

Unlike conventional macro pads that only simulate key combinations blindly, this system maintains a **continuous two-way synchronization link** between your PC and the device. It automatically detects which application currently has window focus (e.g., VS Code, Firefox, Terminal, CAD), dynamically pushes the relevant button labels, icons, and colors to the device screen, and confirms every button action.

No external cloud services, bridges, or Raspberry Pi servers are required. The device communicates directly with your Linux PC over **Bluetooth Low Energy (BLE)** or **USB Serial**.

---

## Key Capabilities

* **Context-Aware Dynamic Switching:** As you switch windows on your computer, the deck immediately adapts its button grid to match the active application (via a native GNOME Shell D-Bus focus reporter).
* **Direct Peer-to-Peer Link:** Connects directly to the workstation running the daemon. No intermediate servers or Raspberry Pi gateways needed.
* **Dual Transport:**
  * **Wireless Bluetooth LE (BLE):** Implements the standard Nordic UART Service (NUS) via BlueZ GATT.
  * **USB CDC Serial:** Plug-and-play over a standard USB-C cable at 115200 baud.
* **Two-Way Synchronization:**
  * **Host → Device:** Streams active window titles, group headers, button labels, icons, semantic color codes, and multi-page numbers.
  * **Device → Host:** Sends button presses with sequential tracking; the host confirms dispatch with `DECK ACK` or reports errors with `DECK ERR`.
* **Multi-Computer Scanning:** If you run the daemon on multiple computers (e.g., desktop and laptop), tap **Scan** on the device to discover all nearby hosts (`DeckD-<hostname>`) and switch target machines on the fly.
* **Multi-Page Grids:** Supports multiple pages (`<` / `>`) for applications with more actions than available physical buttons.
* **Profile Overrides:** Easily toggle between dynamic **Follow focus** and pinning a specific app layout using the touchscreen dropdown or the PC system tray applet.
* **System Tray Applet (`decktray`):** Pure D-Bus `StatusNotifierItem` tray icon (GTK-free to guarantee crash-free operation on Wayland).
* **Visual Profile & Macro Editor (`deckgui`):** Full PySide6/Qt desktop configurator with an interactive 16-button preview, live keyboard shortcut recorder, and syntax-highlighted TOML editor.
* **Hardware Portability:** Clean PlatformIO firmware base that compiles for ESP32-S3 capacitive touch displays (LVGL 8.3), physical pushbutton matrices, or headless serial controllers.

---

## Architecture Overview

```mermaid
flowchart TD
    subgraph PC["Linux Workstation"]
        subgraph DaemonSuite["Host Daemon & Tools"]
            D["deckd (Python Daemon)"]
            T["decktray (D-Bus Status Applet)"]
            G["deckgui (PySide6 Config Editor)"]
            C["deckcontrol (CLI Utility)"]
            P["profiles.toml"]
        end

        Focus["GNOME Shell Focus Extension\n(org.mmesp32.DeckFocus)"] -->|FocusChanged(app, title)| D
        UInput["/dev/uinput (evdev)"] <---|Simulate Key Chords| D
        Shell["Subprocess Exec"] <---|Run Commands| D

        T <-->|org.mmesp32.Deckd1| D
        G <-->|Edit Profiles & Overrides| P
        P <-->|Load / Hot-reload| D
    end

    subgraph Device["ESP32 / ESP32-S3 Device"]
        Firmware["Firmware Entry (main.cpp)"]
        Client["deck_client (C++ Protocol Engine)"]
        UI["deck_ui_lvgl (16-Button LVGL Screen)\nor Physical Pushbuttons"]

        Firmware --> Client
        Client <--> UI
    end

    D <===>|"Bluetooth LE (Nordic UART Service)\nOR USB CDC Serial (115200)"| Client
```

---

## Directory Structure

```
Stream_Deck/
├── README.md                      # This documentation
├── docs/
│   ├── PROTOCOL.md                # Complete DECK newline protocol specification
│   └── HARDWARE_GUIDE.md          # Wiring, display setup, and board porting notes
├── host/                          # Linux host daemon, tray, GUI, and services
│   ├── deckd.py                   # Core background daemon
│   ├── ble_deck.py                # BlueZ BLE GATT server (NUS profile)
│   ├── decktray.py                # System tray status applet (pure D-Bus)
│   ├── deckgui.py                 # PySide6 visual configuration editor
│   ├── deckcontrol.py             # CLI control utility
│   ├── profiles.toml              # Button profiles & action definitions
│   ├── requirements.txt           # Python dependencies
│   ├── install.sh                 # Automated installer script
│   ├── deckd.service.in           # Systemd user service unit template
│   ├── decktray.desktop.in        # Tray autostart launcher
│   ├── deckgui.desktop.in         # Desktop application launcher
│   ├── 99-deckd-uinput.rules      # udev rule for /dev/uinput permissions
│   ├── gnome-extension/           # GNOME Shell active window focus tracker
│   │   └── deckfocus@mmesp32/
│   ├── packaging/                 # Debian (.deb) package scripts
│   └── tests/                     # Automated test suite
└── firmware/                      # Device-side PlatformIO project
    ├── platformio.ini             # Multi-target build configuration
    ├── include/
    │   ├── deck_config.h          # Hardware configuration & transport flags
    │   ├── deck_client.h          # Protocol client header
    │   └── deck_ui_lvgl.h         # LVGL touchscreen UI header
    ├── src/
    │   ├── main.cpp               # Setup, loop, and hardware integration
    │   ├── deck_client.cpp        # Full protocol parser, BLE client, sequence tracking
    │   └── deck_ui_lvgl.cpp       # 16-button LVGL touch interface
    └── test/
        └── test_deck_parser.py    # Host-side unit test for protocol parser
```

---

## System Requirements

### Host (PC)
* **OS:** Linux (Ubuntu 22.04+, Debian 12+, Fedora, Arch).
* **Desktop Environment:** GNOME (Wayland or X11) recommended for automated focus tracking. Other desktop environments work with manual profile selection via the tray or GUI.
* **Bluetooth:** Bluetooth 4.0+ adapter (BlueZ) for BLE wireless mode, or an available USB port for wired serial mode.
* **Python:** Python 3.10+ with `venv`.

### Target Device (Microcontroller)
* **MCU:** ESP32 or ESP32-S3 (with Bluetooth LE support).
* **Optional Display:** Capacitive touchscreen (e.g. CrowPanel 5.0"/7.0" ESP32-S3, CYD Cheap Yellow Display, Sunton ESP32-S3), or physical pushbuttons.
* **Build Tools:** [PlatformIO](https://platformio.org/) (CLI `pio` or the VS Code extension).

---

## Setup & Installation

### Step 1: Host Installation (Linux Workstation)

1. **Navigate to the host directory and run the installer:**
   ```bash
   cd /home/jamie/Documents/Github/Stream_Deck/host
   chmod +x install.sh
   ./install.sh
   ```
   *This automatically sets up a Python virtual environment in `host/.venv`, installs dependencies (`pyserial`, `evdev`, `dbus-next`, `PyQt6`), renders the systemd unit file, and configures the desktop launchers.*

2. **Configure `/dev/uinput` Permissions (Required for Key Emulation):**
   To simulate keyboard shortcuts without requiring root permissions, install the udev rule:
   ```bash
   sudo install -m 0644 99-deckd-uinput.rules /etc/udev/rules.d/
   sudo udevadm control --reload-rules
   sudo udevadm trigger --name-match=uinput
   sudo usermod -aG input "$USER"
   ```
   *(Log out and back in to apply the group change)*

3. **Enable the GNOME Shell Window Focus Extension:**
   ```bash
   gnome-extensions enable deckfocus@mmesp32
   ```
   *(On Wayland, log out and back in once so GNOME Shell loads the newly copied extension)*

4. **Enable and Start the Daemon:**
   ```bash
   systemctl --user enable --now deckd.service
   ```
   Check daemon status:
   ```bash
   systemctl --user status deckd.service
   ```

5. **Start the System Tray Applet and Config GUI:**
   ```bash
   # Launch the tray applet (runs in background):
   ./.venv/bin/python decktray.py &

   # Launch the visual profile editor:
   ./.venv/bin/python deckgui.py &
   ```

---

### Step 2: Device Firmware Setup (PlatformIO)

The `firmware/` directory contains a modular PlatformIO project configured for three modes:

```bash
cd /home/jamie/Documents/Github/Stream_Deck/firmware
```

#### Environment 1: ESP32-S3 Capacitive Touchscreen (Recommended)
Configured for ESP32-S3 panels (such as CrowPanel 5.0" / 7.0" 800×480 with GT911 touch):
```bash
pio run -e esp32s3_ble_touch -t upload
```
* **What happens:** The device boots directly into the 16-button LVGL touch interface, starts scanning for `DeckD-<hostname>`, connects wirelessly via BLE, and downloads the current active profile layout.

#### Environment 2: Generic ESP32 with Physical Pushbuttons (BLE)
Configured for any ESP32 with physical switches wired to GPIOs:
```bash
pio run -e esp32_ble_buttons -t upload
```
* **What happens:** The device connects via BLE and fires button actions when physical switches are triggered.

#### Environment 3: USB Serial CDC (Wired / Headless)
Configured for standard USB cable connection without Bluetooth:
```bash
pio run -e esp32_serial -t upload
```
* **What happens:** Communicates directly over USB serial at 115200 baud. You can monitor and test commands interactively:
  ```bash
  pio device monitor -b 115200
  # Type: press 0, press 1, next, prev, scan
  ```

#### Testing the Firmware Parser Locally
You can test and verify the embedded protocol parsing logic on your PC with your native compiler:
```bash
python3 test/test_deck_parser.py
```

---

## Configuring Profiles (`profiles.toml`)

Profiles define what buttons appear and what actions they trigger. You can edit `host/profiles.toml` manually or use the graphical editor (`deckgui.py`).

### Profile Format

```toml
[profiles.default]
title = "Default Desktop"
group1 = "Quick Launch"
group2 = "Media & System"

# Row 1 (Buttons 0 - 7)
b0 = { label = "Terminal", icon = "terminal", color = "warn", action = "keys:Ctrl+Alt+T" }
b1 = { label = "Files",    icon = "folder",   color = "accent", action = "cmd:nautilus ~" }
b2 = { label = "Browser",  icon = "globe",    color = "accent", action = "cmd:xdg-open https://google.com" }
b3 = { label = "VS Code",  icon = "code",     color = "accent", action = "cmd:code" }
b4 = { label = "Copy",     icon = "copy",     color = "idle",   action = "keys:Ctrl+C" }
b5 = { label = "Paste",    icon = "paste",    color = "idle",   action = "keys:Ctrl+V" }
b6 = { label = "Screenshot", icon = "camera", color = "good",   action = "keys:Print" }
b7 = { label = "Lock",     icon = "lock",     color = "bad",    action = "keys:Super+L" }

# Row 2 (Buttons 8 - 15)
b8  = { label = "Play/Pause", icon = "play",   color = "good",   action = "keys:PlayPause" }
b9  = { label = "Mute",       icon = "mute",   color = "bad",    action = "keys:Mute" }
b10 = { label = "Prev Track", icon = "prev",   color = "idle",   action = "keys:PreviousSong" }
b11 = { label = "Next Track", icon = "next",   color = "idle",   action = "keys:NextSong" }
b12 = { label = "Vol +",      icon = "volup",  color = "good",   action = "keys:VolumeUp" }
b13 = { label = "Vol -",      icon = "voldown", color = "good",  action = "keys:VolumeDown" }
b14 = { label = "Sys Monitor", icon = "refresh", color = "warn", action = "cmd:gnome-system-monitor" }
b15 = { label = "Suspend",    icon = "power",  color = "bad",    action = "cmd:systemctl suspend" }

# Application-Specific Profile (Activated when matching window is focused)
[profiles.code]
match_app = "code"
title = "VS Code"
group1 = "Editor Navigation"
group2 = "Git & Terminal"

b0 = { label = "Command Palette", icon = "terminal", color = "accent", action = "keys:Ctrl+Shift+P" }
b1 = { label = "Go to File",      icon = "folder",   color = "accent", action = "keys:Ctrl+P" }
b2 = { label = "Format Doc",      icon = "code",     color = "good",   action = "keys:Ctrl+Shift+I" }
b3 = { label = "Toggle Terminal", icon = "terminal", color = "warn",   action = "keys:Ctrl+grave" }
b4 = { label = "Git Commit",      icon = "ok",       color = "good",   action = "keys:Ctrl+Enter" }
```

### Action Syntax
* `keys:<combination>`: Emulates keyboard shortcuts via Linux `/dev/uinput`. Examples:
  * `keys:Ctrl+C`
  * `keys:Ctrl+Shift+P`
  * `keys:Super+L`
  * `keys:PlayPause`, `keys:VolumeUp`, `keys:Mute`
* `cmd:<shell_command>`: Executes a shell command or script in the background. Examples:
  * `cmd:nautilus ~/Downloads`
  * `cmd:systemctl suspend`
  * `cmd:spotify`
* `page:next` / `page:prev`: Navigates through multi-page layouts.

### Built-in Semantic Colors
* `accent` (Cyan / Light Blue)
* `good` / `green` (Green)
* `warn` / `yellow` (Amber / Yellow)
* `bad` / `red` (Red)
* `idle` / `muted` (Slate / Dark Gray)
* Custom hex colors: `#RRGGBB` (e.g. `#89B4FA`)

---

## Operating the System

### The Host System Tray Applet (`decktray`)
The tray applet sits in your top bar and gives instant status:
* **Icon Status:** Green icon when an ESP32 is actively connected; gray icon when disconnected.
* **Status line:** Displays current connection (`BLE · workstation` or `USB · workstation`) and active profile.
* **Window Focus line:** Displays the current window class and title detected by GNOME Shell.
* **Follow focus:** Checked by default so layouts follow window focus automatically.
* **Pin Profile:** Clicking any profile name in the menu pins that profile override across all devices.
* **USB / BLE Toggles:** Temporarily pause or resume USB serial or BLE polling without stopping the systemd service.

### The Macro & Profile Editor GUI (`deckgui`)
Run `./.venv/bin/python deckgui.py` to open the visual suite:
1. **Interactive Grid:** View and click your 16 buttons with real colors and icons as they appear on the device.
2. **Page Navigator:** Easily switch between button groups and pages.
3. **Shortcut Recorder:** Click a button to open the action editor, press your keyboard hotkey to record it, pick an icon from the catalog, and pick a semantic color.
4. **Direct TOML Editor:** Edit the configuration file with syntax highlighting and instant structural validation.

### On-Device Controls (Touchscreen)
* **Active App Banner:** The header shows the current active application and window title.
* **Host Dropdown:** Displays connected host identity; tap **Scan** to search for nearby computers.
* **Profile Dropdown:** Select **Follow focus** or pin any profile directly from the screen.
* **Page Buttons (`<` / `>`):** Switch between button pages for the current profile.
* **Feedback Line:** Confirms actions dispatched to the host or displays errors if an action times out.

---

## Troubleshooting & FAQ

#### 1. Keystrokes are not firing on the host
* Check if `/dev/uinput` permissions are set. Run:
  ```bash
  ls -la /dev/uinput
  ```
  It should have `crw-rw---- 1 root input`. Verify your user is in the `input` group:
  ```bash
  groups
  ```
* If not, follow Step 1.2 in the installation guide and log out/in.

#### 2. The daemon fails to start with a BLE error
* Ensure your PC has Bluetooth enabled: `bluetoothctl show`.
* Check if another process is holding the Bluetooth adapter:
  ```bash
  journalctl --user -u deckd.service -e
  ```

#### 3. Profiles do not switch automatically when changing windows
* Verify that the GNOME Shell extension is active:
  ```bash
  gnome-extensions info deckfocus@mmesp32
  ```
* If it is disabled, run `gnome-extensions enable deckfocus@mmesp32`.
* Check if D-Bus focus events are emitting:
  ```bash
  busctl --user monitor org.mmesp32.DeckFocus
  ```

#### 4. The ESP32 screen says "Offline / Scanning..."
* Ensure the host daemon is running (`systemctl --user status deckd.service`).
* Verify that your host's Bluetooth adapter is powered on.
* If using a custom ESP32 board, check `firmware/include/deck_config.h` to confirm the BLE service UUID matches `6E400001-B5A3-F393-E0A9-E50E24DCCA9E`.

---

## Additional Documentation
* **[Protocol Specification (`docs/PROTOCOL.md`)](file:///home/jamie/Documents/Github/Stream_Deck/docs/PROTOCOL.md):** Detailed reference for the line-based `DECK` protocol.
* **[Hardware Porting Guide (`docs/HARDWARE_GUIDE.md`)](file:///home/jamie/Documents/Github/Stream_Deck/docs/HARDWARE_GUIDE.md):** Pin definitions, display controllers, and physical button wiring.
