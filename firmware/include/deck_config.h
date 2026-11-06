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

#pragma once

#include <stdint.h>

// =============================================================================
// Stream Deck Configuration & Build Options
// =============================================================================

// Transport selection:
// 1 = Bluetooth Low Energy (NimBLE Client -> deckd NUS GATT Server)
// 0 = USB Serial / Hardware Serial CDC
#ifndef DECK_TRANSPORT_BLE
#define DECK_TRANSPORT_BLE 1
#endif

// Max number of buttons per page (Standard is 16: 2 groups of 8)
#ifndef DECK_MAX_BUTTONS
#define DECK_MAX_BUTTONS 16
#endif

// Maximum characters per button label
#ifndef DECK_MAX_LABEL_LEN
#define DECK_MAX_LABEL_LEN 20
#endif

// Device identifier sent in DECK HELLO v1 <id>
#ifndef DECK_DEVICE_ID
#define DECK_DEVICE_ID "esp32-streamdeck"
#endif

// BLE Nordic UART Service (NUS) UUIDs
#define DECK_BLE_SERVICE_UUID "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
#define DECK_BLE_RX_CHAR_UUID "6E400002-B5A3-F393-E0A9-E50E24DCCA9E" // Device -> Host write
#define DECK_BLE_TX_CHAR_UUID "6E400003-B5A3-F393-E0A9-E50E24DCCA9E" // Host -> Device notify
