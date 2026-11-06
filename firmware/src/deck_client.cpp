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

#include "deck_client.h"

#if DECK_TRANSPORT_BLE
#include <NimBLEDevice.h>
#endif

static const char* TAG = "deck_client";

#if DECK_TRANSPORT_BLE
static const NimBLEUUID kServiceUUID(DECK_BLE_SERVICE_UUID);
static const NimBLEUUID kRxCharUUID(DECK_BLE_RX_CHAR_UUID);
static const NimBLEUUID kTxCharUUID(DECK_BLE_TX_CHAR_UUID);

static NimBLEClient* s_pClient = nullptr;
static NimBLERemoteCharacteristic* s_pRxChar = nullptr;
static NimBLERemoteCharacteristic* s_pTxChar = nullptr;
static NimBLEAdvertisedDevice* s_targetDevice = nullptr;
#endif

static const char* const kFallbackLabels[DECK_MAX_BUTTONS] = {
    "Term", "Files", "Edge", "Code", "Copy", "Paste", "Shot", "Lock",
    "Play", "Mute", "Prev", "Next", "Vol+", "Vol-", "Tasks", "Sleep"
};
static const char* const kFallbackIcons[DECK_MAX_BUTTONS] = {
    "terminal", "folder", "globe", "code", "copy", "paste", "camera", "lock",
    "play", "mute", "prev", "next", "volup", "voldown", "activity", "moon"
};
static const char* const kFallbackColors[DECK_MAX_BUTTONS] = {
    "warn", "accent", "accent", "accent", "idle", "idle", "good", "bad",
    "good", "bad", "idle", "idle", "good", "good", "warn", "bad"
};

static DeckClientState s_deckState = {
    .linkState = DECK_LINK_DISCONNECTED,
    .focusApp = "Default",
    .focusTitle = "Offline (Base Layout)",
    .page = 1,
    .pageCount = 1,
    .groupTitles = {"Quick Launch", "Media & Controls"},
    .buttons = {},
    .nextSeq = 1,
    .lastRxMs = 0,
    .dirty = true
};

static bool s_client_active = false;
static bool s_client_initialized = false;
static String s_rxBuffer = "";
static uint32_t s_lastScanMs = 0;
static uint32_t s_lastPingMs = 0;
static bool s_layout_ready = false;
static uint16_t s_pending_seq;
static uint32_t s_pending_ms;
static bool s_choose_host = false;
static bool s_scan_requested = false;
static char s_preferred_address[18] = {};
static portMUX_TYPE s_hosts_mux = portMUX_INITIALIZER_UNLOCKED;
static struct { char address[18]; char label[48]; } s_hosts[8];
static uint8_t s_host_count;
static uint16_t s_host_selected;

static void apply_fallback_labels() {
    s_layout_ready = false;
    s_deckState.profilesReady = false;
    s_deckState.profileOptions[0] = '\0';
    s_deckState.profileOverride[0] = '\0';
    s_deckState.hostName[0] = '\0';
    s_deckState.actionPending = false;
    strcpy(s_deckState.groupTitles[0], "Quick Launch");
    strcpy(s_deckState.groupTitles[1], "Media & Audio Controls");
    for (uint8_t i = 0; i < DECK_MAX_BUTTONS; i++) {
        strncpy(s_deckState.buttons[i].label, kFallbackLabels[i], DECK_MAX_LABEL_LEN - 1);
        s_deckState.buttons[i].label[DECK_MAX_LABEL_LEN - 1] = '\0';
        strncpy(s_deckState.buttons[i].icon, kFallbackIcons[i], sizeof(s_deckState.buttons[i].icon) - 1);
        s_deckState.buttons[i].icon[sizeof(s_deckState.buttons[i].icon) - 1] = '\0';
        strncpy(s_deckState.buttons[i].color, kFallbackColors[i], sizeof(s_deckState.buttons[i].color) - 1);
        s_deckState.buttons[i].color[sizeof(s_deckState.buttons[i].color) - 1] = '\0';
    }
    strncpy(s_deckState.focusApp, "Base App", sizeof(s_deckState.focusApp) - 1);
    strncpy(s_deckState.focusTitle, "Offline (Base Layout)", sizeof(s_deckState.focusTitle) - 1);
    s_deckState.page = 1;
    s_deckState.pageCount = 1;
    s_deckState.dirty = true;
}

static bool send_line(const String& line) {
#if DECK_TRANSPORT_BLE
    if (!s_pRxChar || s_deckState.linkState != DECK_LINK_CONNECTED) return false;
    String out = line + "\n";
    return s_pRxChar->writeValue((const uint8_t*)out.c_str(), out.length(), false);
#else
    Serial.println(line);
    return true;
#endif
}

static void process_deck_line(const String& line) {
    s_deckState.lastRxMs = millis();
    if (!line.startsWith("DECK")) return;

    if (line.startsWith("DECK HOST\t")) {
        line.substring(10).toCharArray(s_deckState.hostName, sizeof(s_deckState.hostName));
    } else if (line.startsWith("DECK ACK ")) {
        if (s_deckState.actionPending && line.substring(9).toInt() == s_pending_seq) {
            s_deckState.actionPending = false;
            strcpy(s_deckState.feedback, "Action accepted by computer");
        }
    } else if (line.startsWith("DECK BYE")) {
        s_layout_ready = false;
        strcpy(s_deckState.feedback, "Host busy or disconnected; choose another computer");
    } else if (line == "DECK PROFILES BEGIN") {
        s_deckState.profilesReady = false;
        strcpy(s_deckState.profileOptions, "Follow focus");
    } else if (line.startsWith("DECK PROFILE ")) {
        size_t used = strlen(s_deckState.profileOptions);
        String profile = line.substring(13);
        if (used && profile.length() < sizeof(s_deckState.profileOverride) &&
            used + 1 + profile.length() < sizeof(s_deckState.profileOptions)) {
            strcat(s_deckState.profileOptions, "\n");
            strcat(s_deckState.profileOptions, profile.c_str());
        } else {
            s_deckState.profileOptions[0] = '\0';
            Serial.println("[deck] profile catalog exceeds device capacity");
        }
    } else if (line == "DECK PROFILES END") {
        s_deckState.profilesReady = s_deckState.profileOptions[0] != '\0';
    } else if (line == "DECK PROFILES ERROR") {
        s_deckState.profilesReady = false;
        Serial.println("[deck] daemon cannot provide profile catalog");
    } else if (line.startsWith("DECK SELECTED\t")) {
        String profile = line.substring(14);
        profile.toCharArray(s_deckState.profileOverride, sizeof(s_deckState.profileOverride));
    } else if (line.startsWith("DECK ERR ")) {
        Serial.println("[deck] " + line);
        line.substring(9).toCharArray(s_deckState.feedback, sizeof(s_deckState.feedback));
        s_deckState.actionPending = false;
    } else if (line.startsWith("DECK FOCUS ")) {
        int tab = line.indexOf('\t', 11);
        if (tab >= 0) {
            String app = line.substring(11, tab);
            String title = line.substring(tab + 1);
            strncpy(s_deckState.focusApp, app.c_str(), sizeof(s_deckState.focusApp) - 1);
            s_deckState.focusApp[sizeof(s_deckState.focusApp) - 1] = '\0';
            strncpy(s_deckState.focusTitle, title.c_str(), sizeof(s_deckState.focusTitle) - 1);
            s_deckState.focusTitle[sizeof(s_deckState.focusTitle) - 1] = '\0';
            s_deckState.dirty = true;
        }
    } else if (line.startsWith("DECK GROUPS\t")) {
        int tab = line.indexOf('\t', 12);
        if (tab >= 0) {
            line.substring(12, tab).toCharArray(s_deckState.groupTitles[0], sizeof(s_deckState.groupTitles[0]));
            line.substring(tab + 1).toCharArray(s_deckState.groupTitles[1], sizeof(s_deckState.groupTitles[1]));
        } else {
            line.substring(12).toCharArray(s_deckState.groupTitles[0], sizeof(s_deckState.groupTitles[0]));
            s_deckState.groupTitles[1][0] = '\0';
        }
        s_deckState.dirty = true;
    } else if (line.startsWith("DECK ICONS\t")) {
        int prev = 11;
        for (uint8_t i = 0; i < DECK_MAX_BUTTONS; i++) {
            int next = line.indexOf('\t', prev);
            String val = (next >= 0) ? line.substring(prev, next) : ((prev < (int)line.length()) ? line.substring(prev) : "");
            prev = (next >= 0) ? next + 1 : line.length();
            strncpy(s_deckState.buttons[i].icon, val.c_str(), sizeof(s_deckState.buttons[i].icon) - 1);
            s_deckState.buttons[i].icon[sizeof(s_deckState.buttons[i].icon) - 1] = '\0';
        }
        s_deckState.dirty = true;
    } else if (line.startsWith("DECK COLORS\t")) {
        int prev = 12;
        for (uint8_t i = 0; i < DECK_MAX_BUTTONS; i++) {
            int next = line.indexOf('\t', prev);
            String val = (next >= 0) ? line.substring(prev, next) : ((prev < (int)line.length()) ? line.substring(prev) : "");
            prev = (next >= 0) ? next + 1 : line.length();
            strncpy(s_deckState.buttons[i].color, val.c_str(), sizeof(s_deckState.buttons[i].color) - 1);
            s_deckState.buttons[i].color[sizeof(s_deckState.buttons[i].color) - 1] = '\0';
        }
        s_deckState.dirty = true;
    } else if (line.startsWith("DECK LAYOUT ")) {
        int first_tab = line.indexOf('\t', 12);
        if (first_tab > 12) {
            String page_str = line.substring(12, first_tab);
            int slash = page_str.indexOf('/');
            if (slash > 0) {
                s_deckState.page = page_str.substring(0, slash).toInt();
                s_deckState.pageCount = page_str.substring(slash + 1).toInt();
            }

            int prev = first_tab + 1;
            for (uint8_t i = 0; i < DECK_MAX_BUTTONS; i++) {
                int next = line.indexOf('\t', prev);
                String label_text;
                if (next >= 0) {
                    label_text = line.substring(prev, next);
                    prev = next + 1;
                } else if (prev < (int)line.length()) {
                    label_text = line.substring(prev);
                    prev = line.length();
                } else {
                    label_text = "";
                }
                strncpy(s_deckState.buttons[i].label, label_text.c_str(), DECK_MAX_LABEL_LEN - 1);
                s_deckState.buttons[i].label[DECK_MAX_LABEL_LEN - 1] = '\0';
            }
            s_layout_ready = true;
            s_deckState.dirty = true;
        }
    } else if (line.startsWith("DECK PING ")) {
        String nonce = line.substring(10);
        send_line("DECK PONG " + nonce);
    }
}

#if DECK_TRANSPORT_BLE
static void on_notify_data(NimBLERemoteCharacteristic* pChar, uint8_t* pData, size_t length, bool isNotify) {
    (void)pChar;
    (void)isNotify;
    for (size_t i = 0; i < length; i++) {
        char ch = (char)pData[i];
        if (ch == '\n' || ch == '\r') {
            if (s_rxBuffer.length() > 0) {
                process_deck_line(s_rxBuffer);
                s_rxBuffer = "";
            }
        } else {
            s_rxBuffer += ch;
        }
    }
}

static bool remember_host(const char* address, const char* name) {
    char label[48];
    snprintf(label, sizeof(label), "%.26s [%s]", name[0] ? name : "DeckD", address + 9);
    for (char* p = label; *p; ++p) if (*p == '\n' || *p == '\r') *p = ' ';
    portENTER_CRITICAL(&s_hosts_mux);
    uint8_t index = 0;
    while (index < s_host_count && strcmp(s_hosts[index].address, address) != 0) index++;
    if (index < 8) {
        strcpy(s_hosts[index].address, address);
        if (name[0] || index == s_host_count) strcpy(s_hosts[index].label, label);
        if (index == s_host_count) s_host_count++;
    }
    bool connect = !s_choose_host && (!s_preferred_address[0] || strcmp(s_preferred_address, address) == 0);
    portEXIT_CRITICAL(&s_hosts_mux);
    return connect;
}

class AdvertisedDeviceCallbacks : public NimBLEAdvertisedDeviceCallbacks {
    void onResult(NimBLEAdvertisedDevice* advertisedDevice) override {
        if (advertisedDevice->isAdvertisingService(kServiceUUID) ||
            advertisedDevice->getName().find("DeckD") == 0 ||
            advertisedDevice->getName() == "deckd") {
            const std::string address = advertisedDevice->getAddress().toString();
            if (!remember_host(address.c_str(), advertisedDevice->getName().c_str())) return;
            NimBLEDevice::getScan()->stop();
            if (s_targetDevice) delete s_targetDevice;
            s_targetDevice = new NimBLEAdvertisedDevice(*advertisedDevice);
            s_deckState.linkState = DECK_LINK_CONNECTING;
        }
    }
};

class ClientCallbacks : public NimBLEClientCallbacks {
    void onConnect(NimBLEClient* pClient) override {
        (void)pClient;
    }

    void onDisconnect(NimBLEClient* pClient) override {
        (void)pClient;
        s_deckState.linkState = DECK_LINK_DISCONNECTED;
        s_pRxChar = nullptr;
        s_pTxChar = nullptr;
        s_rxBuffer = "";
        apply_fallback_labels();
    }
};
#endif

void deck_client_init() {
    apply_fallback_labels();
}

void deck_client_start() {
#if DECK_TRANSPORT_BLE
    if (!s_client_initialized) {
        NimBLEDevice::init(DECK_DEVICE_ID);
        NimBLEDevice::setMTU(247);
        NimBLEScan* pScan = NimBLEDevice::getScan();
        pScan->setAdvertisedDeviceCallbacks(new AdvertisedDeviceCallbacks());
        pScan->setActiveScan(true);
        pScan->setInterval(100);
        pScan->setWindow(99);
        s_client_initialized = true;
    }
    s_client_active = true;
    if (s_deckState.linkState == DECK_LINK_DISCONNECTED) {
        s_lastScanMs = 0;
    }
#else
    s_client_active = true;
    s_deckState.linkState = DECK_LINK_CONNECTED;
    s_deckState.lastRxMs = millis();
    send_line(String("DECK HELLO v1 ") + DECK_DEVICE_ID);
#endif
}

void deck_client_stop() {
    s_client_active = false;
#if DECK_TRANSPORT_BLE
    if (s_client_initialized) NimBLEDevice::getScan()->stop();
    if (s_pClient && s_pClient->isConnected()) {
        s_pClient->disconnect();
    }
#endif
    s_deckState.linkState = DECK_LINK_DISCONNECTED;
    s_rxBuffer = "";
    apply_fallback_labels();
}

void deck_client_loop() {
    if (!s_client_active) return;

    uint32_t now = millis();
    if (s_deckState.actionPending && now - s_pending_ms > 5000) {
        s_deckState.actionPending = false;
        strcpy(s_deckState.feedback, "No reply; action may have run. Not retried.");
    }

#if !DECK_TRANSPORT_BLE
    // Serial transport polling
    while (Serial.available()) {
        char ch = (char)Serial.read();
        if (ch == '\n' || ch == '\r') {
            if (s_rxBuffer.length() > 0) {
                process_deck_line(s_rxBuffer);
                s_rxBuffer = "";
            }
        } else {
            s_rxBuffer += ch;
        }
    }
#else
    if (s_scan_requested) {
        s_scan_requested = false;
        NimBLEDevice::getScan()->stop();
        if (s_pClient && s_pClient->isConnected()) s_pClient->disconnect();
        if (s_targetDevice) { delete s_targetDevice; s_targetDevice = nullptr; }
        apply_fallback_labels();
        s_deckState.linkState = DECK_LINK_DISCONNECTED;
        s_lastScanMs = now - 3001;
    }

    if (s_deckState.linkState == DECK_LINK_DISCONNECTED) {
        if (s_choose_host && s_lastScanMs == UINT32_MAX) return;
        if (now - s_lastScanMs > 3000) {
            s_lastScanMs = now;
            s_deckState.linkState = DECK_LINK_SCANNING;
            bool started = NimBLEDevice::getScan()->start(s_choose_host ? 5 : 2, [](NimBLEScanResults) {
                if (s_deckState.linkState == DECK_LINK_SCANNING)
                    s_deckState.linkState = DECK_LINK_DISCONNECTED;
                if (s_choose_host) {
                    s_lastScanMs = UINT32_MAX;
                    portENTER_CRITICAL(&s_hosts_mux);
                    uint8_t count = s_host_count;
                    portEXIT_CRITICAL(&s_hosts_mux);
                    if (count) {
                        snprintf(s_deckState.feedback, sizeof(s_deckState.feedback),
                                 "Found %u computer(s); choose a Host to connect", count);
                    } else {
                        strcpy(s_deckState.feedback, "No computers found; check host BLE and other panel connections");
                    }
                }
            }, false);
            if (!started) {
                Serial.println("[deck] BLE scan start failed");
                strcpy(s_deckState.feedback, "BLE scan failed; tap Scan to retry");
                s_deckState.linkState = DECK_LINK_DISCONNECTED;
            }
        }
    } else if (s_deckState.linkState == DECK_LINK_CONNECTING && s_targetDevice) {
        if (!s_pClient) {
            s_pClient = NimBLEDevice::createClient();
            s_pClient->setClientCallbacks(new ClientCallbacks(), false);
        }

        if (s_pClient->connect(s_targetDevice)) {
            NimBLERemoteService* pService = s_pClient->getService(kServiceUUID);
            if (pService) {
                s_pRxChar = pService->getCharacteristic(kRxCharUUID);
                s_pTxChar = pService->getCharacteristic(kTxCharUUID);

                s_rxBuffer = "";
                s_layout_ready = false;
                if (s_pRxChar && s_pTxChar && s_pTxChar->canNotify() &&
                    s_pTxChar->subscribe(true, on_notify_data)) {
                    s_deckState.linkState = DECK_LINK_CONNECTED;
                    strncpy(s_deckState.hostName, s_targetDevice->getName().c_str(), sizeof(s_deckState.hostName) - 1);
                    s_deckState.hostName[sizeof(s_deckState.hostName) - 1] = '\0';
                    s_deckState.lastRxMs = millis();
                    send_line(String("DECK HELLO v1 ") + DECK_DEVICE_ID);
                } else {
                    s_pClient->disconnect();
                }
            } else {
                s_pClient->disconnect();
            }
        } else {
            s_deckState.linkState = DECK_LINK_DISCONNECTED;
        }

        delete s_targetDevice;
        s_targetDevice = nullptr;
    } else if (s_deckState.linkState == DECK_LINK_CONNECTED) {
        if (now - s_deckState.lastRxMs > 15000) {
            strcpy(s_deckState.feedback, "Host stopped responding; reconnecting...");
            if (s_pClient) s_pClient->disconnect();
        } else if (now - s_lastPingMs > 5000) {
            s_lastPingMs = now;
            send_line("DECK PING " + String(now, HEX));
        }
    }
#endif
}

const DeckClientState& deck_client_get_state() {
    return s_deckState;
}

bool deck_client_is_connected() {
    return s_deckState.linkState == DECK_LINK_CONNECTED;
}

bool deck_client_controls_ready() {
    return deck_client_is_connected() && s_layout_ready && !s_deckState.actionPending;
}

bool deck_client_send_press(uint8_t buttonIndex) {
    if (!deck_client_controls_ready() ||
        buttonIndex >= DECK_MAX_BUTTONS || !s_deckState.buttons[buttonIndex].label[0]) {
        Serial.println("[deck] press rejected: no synchronized button layout");
        return false;
    }
    uint16_t seq = s_deckState.nextSeq++;
    s_pending_seq = seq;
    s_pending_ms = millis();
    s_deckState.actionPending = true;
    strcpy(s_deckState.feedback, "Sending action...");
    if (!send_line("DECK PRESS " + String(buttonIndex) + " " + String(seq))) {
        s_deckState.actionPending = false;
        strcpy(s_deckState.feedback, "Action send failed");
        return false;
    }
    return true;
}

void deck_client_scan_hosts() {
    s_choose_host = true;
    s_preferred_address[0] = '\0';
    portENTER_CRITICAL(&s_hosts_mux);
    s_host_count = 0;
    s_host_selected = 0;
    portEXIT_CRITICAL(&s_hosts_mux);
    strcpy(s_deckState.feedback, "Scanning computers; select one from the Host list");
    s_scan_requested = true;
}

void deck_client_get_hosts(char* options, size_t capacity, uint16_t* selected) {
    if (!options || capacity < 512 || !selected) return;
    strcpy(options, "Automatic (first available)");
    portENTER_CRITICAL(&s_hosts_mux);
    for (uint8_t i = 0; i < s_host_count; ++i) {
        strcat(options, "\n");
        strcat(options, s_hosts[i].label);
    }
    *selected = s_host_selected;
    portEXIT_CRITICAL(&s_hosts_mux);
}

bool deck_client_select_host(uint16_t index) {
    portENTER_CRITICAL(&s_hosts_mux);
    bool valid = index <= s_host_count;
    if (valid) {
        s_host_selected = index;
        if (index) strcpy(s_preferred_address, s_hosts[index - 1].address);
        else s_preferred_address[0] = '\0';
    }
    portEXIT_CRITICAL(&s_hosts_mux);
    if (!valid) {
        strcpy(s_deckState.feedback, "Host list changed; scan again");
        return false;
    }
    s_choose_host = false;
    strcpy(s_deckState.feedback, "Connecting to selected computer...");
    s_scan_requested = true;
    return true;
}

bool deck_client_send_page_next() {
    if (!deck_client_is_connected() || !s_layout_ready) {
        Serial.println("[deck] page request rejected: no synchronized layout");
        return false;
    }
    s_layout_ready = false;
    send_line("DECK PAGE NEXT");
    return true;
}

bool deck_client_send_page_prev() {
    if (!deck_client_is_connected() || !s_layout_ready) {
        Serial.println("[deck] page request rejected: no synchronized layout");
        return false;
    }
    s_layout_ready = false;
    send_line("DECK PAGE PREV");
    return true;
}

bool deck_client_select_profile(const char* profile) {
    if (!deck_client_is_connected() || !s_deckState.profilesReady || !profile ||
        strlen(profile) >= sizeof(s_deckState.profileOverride)) {
        Serial.println("[deck] profile selection rejected: not ready");
        return false;
    }
    bool was_ready = s_layout_ready;
    s_layout_ready = false;
    if (!send_line(String("DECK SELECT\t") + profile)) {
        s_layout_ready = was_ready;
        strcpy(s_deckState.feedback, "Profile selection send failed");
        Serial.println("[deck] profile selection send failed");
        return false;
    }
    return true;
}
