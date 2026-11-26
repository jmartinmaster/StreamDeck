# Hardware Porting & Setup Guide

This firmware is designed to run on **any ESP32 or ESP32-S3 microcontroller** using PlatformIO and the Arduino framework.

---

## Before Flashing

- If `deckd` is already running on your workstation, **release the USB serial port** first (via the tray's USB toggle or `deckd-user release-usb`) so `esptool` can open `/dev/ttyUSB0` or `/dev/ttyACM0`.
- All ESP32-S3 environments default to a **4MB-safe partition table** (`huge_app.csv`, 3MB app partition) so they boot cleanly on 4MB, 8MB, and 16MB ESP32-S3 boards without partition errors. If your board has 8MB or 16MB flash and OPI PSRAM, you can optionally raise `board_upload.flash_size` or add `-DBOARD_HAS_PSRAM` in `firmware/platformio.ini`.
- If you have multiple USB serial devices plugged in, specify the port explicitly with `--upload-port /dev/ttyUSB0` (or `/dev/ttyACM0`).

---

## PlatformIO Environments

### 1. Elecrow CrowPanel 7.0" ESP32-S3 (`esp32s3_crowpanel_7` — Default, Ready to Flash)
Includes a complete native ESP-IDF 5.x double-buffered 800×480 RGB LCD driver, PCA9557 I/O expander reset, GT911 capacitive touch task, PWM backlight control, and `lv_conf.h` (`firmware/src/boards/crowpanel7/`).

**How to build and flash:**
```bash
cd firmware
pio run -e esp32s3_crowpanel_7 -t upload
```
*What happens on boot:*
- Initializes the 800×480 RGB display, GT911 touch (`0x5D` / `0x14`), and backlight at 100%.
- Renders the 16-button LVGL Stream Deck UI (2 groups of 8 buttons), host/profile/transport dropdowns, page navigation, paginated App Launcher, and an on-screen **Wi-Fi Setup** view (nearby AP scanner, on-screen keyboard, optional host IP override, and NVS credential persistence).
- Scans for `DeckD-<hostname>` over BLE automatically, or lets you switch to USB Serial or Wi-Fi TCP (`_mmdeckd._tcp` mDNS + port 4650) from the on-screen transport dropdown.

---

### 2. Custom ESP32-S3 Touchscreen Panels (`esp32s3_ble_touch`)
Target hardware:
- CrowPanel 5.0" ESP32-S3
- Sunton ESP32-S3 5.0" / 7.0" panels
- CYD (Cheap Yellow Display) ESP32-S3 variants

**How to build and flash:**
```bash
cd firmware
pio run -e esp32s3_ble_touch -t upload
```

> **Adding Your Panel Driver:**
> `esp32s3_ble_touch` provides the LVGL 8.3 UI (`src/deck_ui_lvgl.cpp`), `include/lv_conf.h`, and protocol/BLE engine (`src/deck_client.cpp`) without a board-specific display driver.
>
> Use `firmware/src/boards/crowpanel7/` as a reference to add your panel's display + touch initialization (e.g., `esp_lcd_panel_rgb`, LovyanGFX, or `Arduino_GFX` + GT911/FT6336) and register `lv_disp_drv_t` / `lv_indev_drv_t` after `lv_init()`.
>
> If flashed before adding a display driver, `esp32s3_ble_touch` boots safely, logs a warning on Serial, and runs the BLE/protocol stack in headless mode rather than crashing.

---

### 3. Headless / Physical Buttons over BLE (`esp32s3_ble_buttons`, `esp32_ble_buttons`)
Ideal for testing on any bare ESP32-S3 or ESP32 devkit (such as an ESP32-S3 DevKitC-1) or building a physical macropad with tactile/mechanical switches:
- Wiring: Connect pushbuttons between GPIO pins and GND (uses internal `INPUT_PULLUP`). Default `BUTTON_PIN_0=0` uses the onboard BOOT button on most devkits.
- Because BLE carries the `DECK` protocol, **USB Serial (115200 baud) remains free for an interactive test CLI**.

**How to build and flash:**
```bash
cd firmware
# For ESP32-S3 boards:
pio run -e esp32s3_ble_buttons -t upload

# For classic ESP32 boards (DevKit V1, NodeMCU-32S, etc.):
pio run -e esp32_ble_buttons -t upload
```

**Interactive Serial CLI (BLE headless environments):**
```bash
pio device monitor -b 115200
# Type:
status
scan
press 0
launcher 0
next
prev
```

*Customizing Pins:*
Edit `firmware/src/main.cpp` or pass `-DBUTTON_PIN_0=<gpio>` in `platformio.ini`. When a button is pressed, call:
```cpp
deck_client_send_press(button_index);
```

---

### 4. Wired USB Serial (`esp32s3_serial`, `esp32_serial`)
Works on any ESP32-S3 or classic ESP32 connected to your PC via USB cable without Bluetooth.
- Does not require Bluetooth hardware or NimBLE.
- The USB UART (`Serial` at 115200 baud) is dedicated to the `DECK` newline protocol talking directly to `deckd` (do not keep `pio device monitor` open while `deckd` is connected).
- Pressing the configured physical button (`BUTTON_PIN_0=0`, the onboard BOOT button by default) triggers button `0` over USB Serial.

**How to build and flash:**
```bash
cd firmware
# For ESP32-S3 boards:
pio run -e esp32s3_serial -t upload

# For classic ESP32 boards:
pio run -e esp32_serial -t upload
```

---

## Board Flash / PSRAM Notes

- **4MB vs 8MB/16MB Flash:** All ESP32-S3 environments use `board_upload.flash_size = 4MB` and `board_build.partitions = huge_app.csv`, which works on 4MB, 8MB, and 16MB modules.
- **Native USB CDC vs UART Bridge:** Boards with a dedicated USB-UART bridge chip (CH340 / CP2102, usually `/dev/ttyUSB0`) work with default settings. Boards that only expose native ESP32-S3 USB-OTG (`/dev/ttyACM0`) may need `-DARDUINO_USB_CDC_ON_BOOT=1` added to `build_flags` in `platformio.ini`.
