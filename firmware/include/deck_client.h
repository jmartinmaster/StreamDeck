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

#include <Arduino.h>
#include "deck_config.h"

enum DeckLinkState {
    DECK_LINK_DISCONNECTED,
    DECK_LINK_SCANNING,
    DECK_LINK_CONNECTING,
    DECK_LINK_CONNECTED
};

struct DeckButtonInfo {
    char label[DECK_MAX_LABEL_LEN];
    char icon[16];
    char color[16];
};

struct DeckClientState {
    DeckLinkState linkState;
    char focusApp[32];
    char focusTitle[48];
    uint8_t page;
    uint8_t pageCount;
    char groupTitles[2][32];
    DeckButtonInfo buttons[DECK_MAX_BUTTONS];
    uint16_t nextSeq;
    uint32_t lastRxMs;
    bool dirty;
    char profileOptions[1024];
    char profileOverride[64];
    bool profilesReady;
    char hostName[64];
    char feedback[112];
    bool actionPending;
};

// Lifecycle
void deck_client_init();
void deck_client_start();
void deck_client_stop();
void deck_client_loop();

// State querying
const DeckClientState& deck_client_get_state();
bool deck_client_is_connected();
bool deck_client_controls_ready();

// Actions
bool deck_client_send_press(uint8_t buttonIndex);
bool deck_client_send_page_next();
bool deck_client_send_page_prev();
bool deck_client_select_profile(const char* profile);
void deck_client_scan_hosts();
void deck_client_get_hosts(char* options, size_t capacity, uint16_t* selected);
bool deck_client_select_host(uint16_t index);
