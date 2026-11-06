# Hardware Porting & Setup Guide

This boilerplate is designed to run on **any ESP32, ESP32-S3, or compatible microcontroller** as long as you have the PlatformIO / Arduino build environment.

---

## Hardware Flavors

### 1. ESP32-S3 Touchscreen Panels (Recommended)
Supported hardware:
- CrowPanel 5.0" / 7.0" ESP32-S3 (800x480 RGB display with GT911 capacitive touch)
- Sunton ESP32-S3 5.0" / 7.0" panels
- CYD (Cheap Yellow Display) ESP32-2432S028 / ESP32-S3 variants

**How to build:**
```bash
cd firmware
pio run -e esp32s3_ble_touch -t upload
```
*Notes:*
- The UI renders 16 interactive macro buttons (2 groups of 8 columns) using LVGL 8.3.
- Connects automatically over Bluetooth LE to your Linux workstation running `deckd`.
- Shows active application icon/title, profile selection dropdown, host selection dropdown, and page navigation buttons.

---

### 2. Physical Button Matrix / Pushbuttons over BLE
If you want to build a physical macropad using tactile switches or mechanical keyboard switches:
- Microcontroller: Any standard ESP32 (e.g. ESP32 DevKit V1, NodeMCU-32S, ESP32-C3/S3)
- Wiring: Connect pushbuttons between GPIO pins and GND (using internal pullups `INPUT_PULLUP`).

**How to build:**
```bash
cd firmware
pio run -e esp32_ble_buttons -t upload
```
*Customizing Pins:*
Edit `firmware/src/main.cpp` or pass `-DBUTTON_PIN_0=<gpio>` in `platformio.ini`. When the button is pressed, it calls:
```cpp
deck_client_send_press(button_index);
```

---

### 3. USB Serial (No Bluetooth, Universal Compatibility)
Works on any board connected to your PC via USB cable (ESP32, RP2040, Arduino, Teensy).
- Does not require Bluetooth hardware or antenna.
- The `deckd` daemon will automatically detect the USB serial port at 115200 baud.

**How to build:**
```bash
cd firmware
pio run -e esp32_serial -t upload
```
You can test button presses directly in the serial monitor:
```bash
pio device monitor -b 115200
# Type:
press 0
press 1
next
prev
```
