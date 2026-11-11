/*
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * This file is part of Stream Deck.
 *
 * Stream Deck is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * Stream Deck is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with Stream Deck.  If not, see <https://www.gnu.org/licenses/>.
 */

#include <Arduino.h>
#include "deck_config.h"
#include "deck_client.h"

#ifdef USE_LVGL_UI
#include <lvgl.h>
#include "deck_ui_lvgl.h"
#endif

// Example GPIO button pins for hardware buttons (optional)
#ifndef BUTTON_PIN_0
#define BUTTON_PIN_0 -1
#endif

void setup() {
    Serial.begin(115200);
    delay(500);
    Serial.println("\n[main] ========================================");
    Serial.println("[main] ESP32 Stream Deck Firmware Initializing");
    Serial.printf("[main] Transport: %s\n", DECK_TRANSPORT_BLE ? "Bluetooth LE (NimBLE)" : "Serial (CDC)");
    Serial.printf("[main] Device ID: %s\n", DECK_DEVICE_ID);
    Serial.println("[main] ========================================");

    deck_client_init();
    deck_client_start();

#ifdef USE_LVGL_UI
    Serial.println("[main] Initializing LVGL Deck UI...");
    // Initialize your display and touch driver here before calling deck_ui_create
    deck_ui_create(lv_scr_act());
#else
    Serial.println("[main] Running in Headless / Hardware Buttons mode.");
    Serial.println("[main] Commands: 'press <0-15>', 'next', 'prev', 'scan'");
#endif

    if (BUTTON_PIN_0 >= 0) {
        pinMode(BUTTON_PIN_0, INPUT_PULLUP);
    }
}

void loop() {
    // 1. Process deck protocol state machine and BLE/Serial events
    deck_client_loop();

#ifdef USE_LVGL_UI
    // 2. Handle LVGL timer and sync UI
    lv_timer_handler();
    static uint32_t last_ui_update = 0;
    if (millis() - last_ui_update > 200) {
        last_ui_update = millis();
        deck_ui_update();
    }
#else
    // 3. Serial CLI test helper for headless / non-display devices
    if (Serial.available()) {
        String cmd = Serial.readStringUntil('\n');
        cmd.trim();
        if (cmd.startsWith("press ")) {
            uint8_t btn = cmd.substring(6).toInt();
            Serial.printf("[main] Simulating press on button %u\n", btn);
            deck_client_send_press(btn);
        } else if (cmd.startsWith("launcher ")) {
            uint8_t idx = cmd.substring(9).toInt();
            Serial.printf("[main] Simulating launcher press on slot %u\n", idx);
            deck_client_send_launcher_press(idx);
        } else if (cmd == "next") {
            deck_client_send_page_next();
        } else if (cmd == "prev") {
            deck_client_send_page_prev();
        } else if (cmd == "scan") {
            deck_client_scan_hosts();
        } else if (cmd == "status") {
            const DeckClientState& st = deck_client_get_state();
            const DeckLauncherState& ln = deck_client_get_launcher();
            Serial.printf("[status] Link: %d, Focus: %s - %s, Page: %u/%u, Launcher: %u\n",
                          st.linkState, st.focusApp, st.focusTitle, st.page, st.pageCount, ln.count);
        }
    }

    // 4. Sample hardware button pin if configured
    if (BUTTON_PIN_0 >= 0) {
        static bool last_btn_state = HIGH;
        bool btn_state = digitalRead(BUTTON_PIN_0);
        if (last_btn_state == HIGH && btn_state == LOW) {
            Serial.println("[main] Physical button 0 pressed");
            deck_client_send_press(0);
            delay(50); // Simple debounce
        }
        last_btn_state = btn_state;
    }
#endif

    delay(5);
}
