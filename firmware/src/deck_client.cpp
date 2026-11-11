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

#include <ESPmDNS.h>
#include <Preferences.h>
#include <WiFi.h>
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

static DeckLauncherState s_launcherState = {
    .items = {},
    .count = 0,
    .autoReturn = true,
    .ready = false,
    .dirty = true
};
static DeckLauncherState s_launcherStaging = {};
static uint8_t s_launcherExpectedCount = 0;

static bool s_client_active = false;
static bool s_client_initialized = false;
static DeckTransport s_transport = DECK_TRANSPORT_BLE ? DECK_MODE_BLE : DECK_MODE_USB;

static WiFiClient* s_tcpClient = nullptr;
static WiFiClient* s_pending_tcp = nullptr;
static portMUX_TYPE s_wifi_mux = portMUX_INITIALIZER_UNLOCKED;
static bool s_dial_in_progress = false;
static uint32_t s_dial_generation = 0;
static uint32_t s_next_dial_ms = 0;
static uint32_t s_dial_backoff_ms = 1000;
static bool s_mdns_started = false;
static char s_cached_host[40] = {};
static uint16_t s_cached_port = 0;
static char s_pinned_host[64] = {};
static char s_pending_host[40] = {};
static uint16_t s_pending_port = 0;
static bool s_use_cached_host = true;

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

static const char* transport_ready_feedback(DeckTransport t) {
    if (t == DECK_MODE_USB) return "USB Serial active; waiting for deckd";
    if (t == DECK_MODE_WIFI) return "Wi-Fi mode active; searching for deckd";
    return "Bluetooth mode active; scanning for deckd";
}

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
    memset(s_launcherState.items, 0, sizeof(s_launcherState.items));
    s_launcherState.count = 0;
    s_launcherState.autoReturn = true;
    s_launcherState.ready = false;
    s_launcherState.dirty = true;
}

static bool send_line(const String& line, bool allow_handshake = false) {
    if (s_transport == DECK_MODE_USB) {
        if (!s_client_active) return false;
        if (!allow_handshake && s_deckState.linkState != DECK_LINK_CONNECTED) return false;
        Serial.println(line);
        return true;
    }
    if (s_transport == DECK_MODE_WIFI) {
        if (!s_client_active || !s_tcpClient || !s_tcpClient->connected()) return false;
        if (!allow_handshake && s_deckState.linkState != DECK_LINK_CONNECTED) return false;
        String out = line + "\n";
        return s_tcpClient->write((const uint8_t*)out.c_str(), out.length()) == out.length();
    }
#if DECK_TRANSPORT_BLE
    if (!s_pRxChar || s_deckState.linkState != DECK_LINK_CONNECTED) return false;
    String out = line + "\n";
    return s_pRxChar->writeValue((const uint8_t*)out.c_str(), out.length(), false);
#else
    (void)line;
    (void)allow_handshake;
    return false;
#endif
}

static void close_wifi_client() {
    if (s_tcpClient) {
        s_tcpClient->stop();
        delete s_tcpClient;
        s_tcpClient = nullptr;
    }
}

static void process_deck_line(const String& line);

static void drain_stream_lines(Stream& stream) {
    while (stream.available()) {
        char ch = (char)stream.read();
        if (ch == '\n' || ch == '\r') {
            if (s_rxBuffer.length() > 0) {
                s_rxBuffer.trim();
                process_deck_line(s_rxBuffer);
                s_rxBuffer = "";
            }
        } else if (s_rxBuffer.length() < 2048) {
            s_rxBuffer += ch;
        }
    }
}

static void process_deck_line(const String& line) {
    if (!line.startsWith("DECK")) return;
    s_deckState.lastRxMs = millis();

    if (s_transport == DECK_MODE_USB && s_deckState.linkState != DECK_LINK_CONNECTED &&
        !line.startsWith("DECK BYE")) {
        s_deckState.linkState = DECK_LINK_CONNECTED;
        strcpy(s_deckState.feedback, "Connected over USB");
        s_deckState.dirty = true;
    }

    if (line.startsWith("DECK HELLO")) {
        if (s_transport == DECK_MODE_USB) {
            s_deckState.linkState = DECK_LINK_CONNECTED;
            strcpy(s_deckState.feedback, "Connected over USB");
            s_deckState.dirty = true;
            send_line(String("DECK HELLO v1 ") + DECK_DEVICE_ID, true);
        } else if (s_transport == DECK_MODE_WIFI) {
            s_deckState.linkState = DECK_LINK_CONNECTED;
            strcpy(s_deckState.feedback, "Connected over Wi-Fi");
            s_deckState.dirty = true;
        }
    } else if (line.startsWith("DECK HOST\t")) {
        line.substring(10).toCharArray(s_deckState.hostName, sizeof(s_deckState.hostName));
    } else if (line.startsWith("DECK ACK ")) {
        if (s_deckState.actionPending && line.substring(9).toInt() == s_pending_seq) {
            s_deckState.actionPending = false;
            strcpy(s_deckState.feedback, "Action accepted by computer");
        }
    } else if (line.startsWith("DECK BYE")) {
        s_layout_ready = false;
        if (s_transport == DECK_MODE_USB) {
            apply_fallback_labels();
            s_deckState.linkState = s_client_active ? DECK_LINK_SCANNING : DECK_LINK_DISCONNECTED;
            strcpy(s_deckState.feedback, "Host disconnected; waiting for deckd on USB");
        } else if (s_transport == DECK_MODE_WIFI) {
            close_wifi_client();
            apply_fallback_labels();
            s_deckState.linkState = s_client_active ? DECK_LINK_SCANNING : DECK_LINK_DISCONNECTED;
            s_next_dial_ms = millis() + 2000;
            strcpy(s_deckState.feedback, "Host disconnected; reconnecting over Wi-Fi...");
        } else {
            strcpy(s_deckState.feedback, "Host busy or disconnected; choose another computer");
        }
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
    } else if (line.startsWith("DECK LAUNCHER BEGIN\t")) {
        int tab2 = line.indexOf('\t', 20);
        memset(&s_launcherStaging, 0, sizeof(s_launcherStaging));
        s_launcherStaging.autoReturn = true;
        s_launcherExpectedCount = 0;
        if (tab2 > 20) {
            s_launcherStaging.autoReturn = (line.substring(20, tab2).toInt() != 0);
            int cnt = line.substring(tab2 + 1).toInt();
            if (cnt < 0) cnt = 0;
            if (cnt > DECK_MAX_LAUNCHER_APPS) cnt = DECK_MAX_LAUNCHER_APPS;
            s_launcherExpectedCount = static_cast<uint8_t>(cnt);
        }
    } else if (line.startsWith("DECK LAUNCHER ITEM\t")) {
        int t1 = line.indexOf('\t', 19);
        int t2 = (t1 >= 0) ? line.indexOf('\t', t1 + 1) : -1;
        int t3 = (t2 >= 0) ? line.indexOf('\t', t2 + 1) : -1;
        if (t1 > 19 && t2 > t1) {
            int idx = line.substring(19, t1).toInt();
            if (idx >= 0 && idx < DECK_MAX_LAUNCHER_APPS) {
                String lbl = line.substring(t1 + 1, t2);
                String icon = (t3 >= 0) ? line.substring(t2 + 1, t3) : line.substring(t2 + 1);
                String color = (t3 >= 0) ? line.substring(t3 + 1) : "accent";
                strncpy(s_launcherStaging.items[idx].label, lbl.c_str(), DECK_MAX_LABEL_LEN - 1);
                s_launcherStaging.items[idx].label[DECK_MAX_LABEL_LEN - 1] = '\0';
                strncpy(s_launcherStaging.items[idx].icon, icon.c_str(), sizeof(s_launcherStaging.items[idx].icon) - 1);
                s_launcherStaging.items[idx].icon[sizeof(s_launcherStaging.items[idx].icon) - 1] = '\0';
                strncpy(s_launcherStaging.items[idx].color, color.c_str(), sizeof(s_launcherStaging.items[idx].color) - 1);
                s_launcherStaging.items[idx].color[sizeof(s_launcherStaging.items[idx].color) - 1] = '\0';
                if (static_cast<uint8_t>(idx + 1) > s_launcherStaging.count) {
                    s_launcherStaging.count = static_cast<uint8_t>(idx + 1);
                }
            }
        }
    } else if (line == "DECK LAUNCHER END") {
        if (s_launcherExpectedCount < s_launcherStaging.count) {
            s_launcherStaging.count = s_launcherExpectedCount;
        }
        memcpy(&s_launcherState, &s_launcherStaging, sizeof(s_launcherState));
        s_launcherState.ready = true;
        s_launcherState.dirty = true;
        s_deckState.dirty = true;
    } else if (line == "DECK PROFILES ERROR") {
        s_deckState.profilesReady = false;
        Serial.println("[deck] daemon cannot provide profile catalog");
    } else if (line == "DECK SELECTED") {
        s_deckState.profileOverride[0] = '\0';
        s_deckState.dirty = true;
    } else if (line.startsWith("DECK SELECTED\t")) {
        String profile = line.substring(14);
        profile.toCharArray(s_deckState.profileOverride, sizeof(s_deckState.profileOverride));
        s_deckState.dirty = true;
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
        bool need_hello = (s_transport == DECK_MODE_USB && !s_layout_ready);
        send_line("DECK PONG " + nonce, true);
        if (need_hello) send_line(String("DECK HELLO v1 ") + DECK_DEVICE_ID, true);
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
        } else if (s_rxBuffer.length() < 2048) {
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

static void wifi_dial_task(void* arg) {
    uint32_t gen = static_cast<uint32_t>(reinterpret_cast<uintptr_t>(arg));
    char target_ip[40] = {};
    uint16_t target_port = 0;
    bool used_cache = false;

    char pinned_copy[64] = {};
    portENTER_CRITICAL(&s_wifi_mux);
    if (gen == s_dial_generation) {
        strncpy(pinned_copy, s_pinned_host, sizeof(pinned_copy) - 1);
        if (pinned_copy[0] == '\0' && s_use_cached_host && s_cached_host[0] && s_cached_port != 0) {
            strncpy(target_ip, s_cached_host, sizeof(target_ip) - 1);
            target_port = s_cached_port;
            used_cache = true;
            s_use_cached_host = false;
        }
    }
    portEXIT_CRITICAL(&s_wifi_mux);

    if (pinned_copy[0] != '\0') {
        const char* colon = strrchr(pinned_copy, ':');
        if (colon && atoi(colon + 1) > 0) {
            size_t host_len = static_cast<size_t>(colon - pinned_copy);
            if (host_len < sizeof(target_ip)) {
                memcpy(target_ip, pinned_copy, host_len);
                target_ip[host_len] = '\0';
            }
            target_port = static_cast<uint16_t>(atoi(colon + 1));
        } else {
            strncpy(target_ip, pinned_copy, sizeof(target_ip) - 1);
            target_port = 4650;
        }
    } else if (target_ip[0] == '\0' && WiFi.status() == WL_CONNECTED) {
        if (!s_mdns_started) {
            s_mdns_started = MDNS.begin(DECK_DEVICE_ID);
        }
        if (s_mdns_started) {
            int n = MDNS.queryService("mmdeckd", "tcp");
            if (n > 0) {
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
                IPAddress ip = MDNS.address(0);
#else
                IPAddress ip = MDNS.IP(0);
#endif
                if (ip != IPAddress(0, 0, 0, 0)) {
                    snprintf(target_ip, sizeof(target_ip), "%s", ip.toString().c_str());
                    target_port = MDNS.port(0) ? MDNS.port(0) : 4650;
                }
            }
        }
    }

    WiFiClient* client = nullptr;
    if (target_ip[0] != '\0' && target_port != 0 && WiFi.status() == WL_CONNECTED) {
        WiFiClient* probe = new WiFiClient();
        if (probe->connect(target_ip, target_port, 2500)) {
            client = probe;
        } else {
            probe->stop();
            delete probe;
        }
    }

    portENTER_CRITICAL(&s_wifi_mux);
    if (gen == s_dial_generation && client) {
        s_pending_tcp = client;
        strncpy(s_pending_host, target_ip, sizeof(s_pending_host) - 1);
        s_pending_port = target_port;
        client = nullptr;
    } else if (gen == s_dial_generation) {
        if (!used_cache) {
            s_dial_backoff_ms = (s_dial_backoff_ms < 16000) ? (s_dial_backoff_ms * 2) : 16000;
            uint32_t jitter = millis() % 1000;
            s_next_dial_ms = millis() + s_dial_backoff_ms + jitter;
        } else {
            s_next_dial_ms = millis() + 250;
        }
        if (target_ip[0] == '\0') {
            strcpy(s_deckState.feedback, "No Wi-Fi deckd found (_mmdeckd._tcp); retrying...");
        } else {
            snprintf(s_deckState.feedback, sizeof(s_deckState.feedback),
                     "deckd at %s:%u unreachable; retrying...", target_ip, target_port);
        }
        if (s_deckState.linkState == DECK_LINK_CONNECTING) {
            s_deckState.linkState = DECK_LINK_SCANNING;
        }
    }
    s_dial_in_progress = false;
    portEXIT_CRITICAL(&s_wifi_mux);

    if (client) {
        client->stop();
        delete client;
    }
    vTaskDelete(nullptr);
}

void deck_client_init() {
    Preferences prefs;
    prefs.begin(DECK_NVS_NS, true);
    uint8_t default_mode = DECK_TRANSPORT_BLE ? DECK_MODE_BLE : DECK_MODE_USB;
    uint8_t saved = prefs.isKey("deck_mode")
                        ? prefs.getUChar("deck_mode", default_mode)
                        : default_mode;
    String cached_host = prefs.isKey("deck_host") ? prefs.getString("deck_host", "") : "";
    uint16_t cached_port = prefs.isKey("deck_port") ? prefs.getUShort("deck_port", 0) : 0;
    String pinned_host = prefs.isKey("deck_pin") ? prefs.getString("deck_pin", "") : "";
    prefs.end();

    if (saved == DECK_MODE_WIFI) s_transport = DECK_MODE_WIFI;
    else if (saved == DECK_MODE_BLE && DECK_TRANSPORT_BLE) s_transport = DECK_MODE_BLE;
    else s_transport = DECK_MODE_USB;

    if (!cached_host.isEmpty() && cached_port != 0) {
        strncpy(s_cached_host, cached_host.c_str(), sizeof(s_cached_host) - 1);
        s_cached_port = cached_port;
        s_use_cached_host = true;
    }
    if (!pinned_host.isEmpty()) {
        strncpy(s_pinned_host, pinned_host.c_str(), sizeof(s_pinned_host) - 1);
        s_pinned_host[sizeof(s_pinned_host) - 1] = '\0';
    }

    apply_fallback_labels();
    strcpy(s_deckState.feedback, transport_ready_feedback(s_transport));
}

DeckTransport deck_client_get_transport() {
    return s_transport;
}

bool deck_client_ble_initialized() {
    return s_client_initialized;
}

const char* deck_client_get_wifi_host() {
    return s_pinned_host;
}

void deck_client_set_wifi_host(const char* host) {
    String clean = host ? String(host) : "";
    clean.trim();
    Preferences prefs;
    prefs.begin(DECK_NVS_NS, false);
    if (clean.isEmpty()) prefs.remove("deck_pin");
    else prefs.putString("deck_pin", clean);
    prefs.end();

    portENTER_CRITICAL(&s_wifi_mux);
    if (clean.isEmpty()) {
        s_pinned_host[0] = '\0';
    } else {
        strncpy(s_pinned_host, clean.c_str(), sizeof(s_pinned_host) - 1);
        s_pinned_host[sizeof(s_pinned_host) - 1] = '\0';
    }
    portEXIT_CRITICAL(&s_wifi_mux);

    if (s_client_active && s_transport == DECK_MODE_WIFI) {
        deck_client_stop();
        deck_client_start();
    }
    s_deckState.dirty = true;
}

bool deck_client_set_transport(DeckTransport transport) {
    if (transport > DECK_MODE_BLE) return false;
    if (transport == DECK_MODE_BLE && !DECK_TRANSPORT_BLE) return false;
    Preferences prefs;
    prefs.begin(DECK_NVS_NS, false);
    prefs.putUChar("deck_mode", static_cast<uint8_t>(transport));
    prefs.end();
    if (transport == s_transport) return true;

    bool was_active = s_client_active;
    if (was_active) deck_client_stop();
    s_transport = transport;
    if (was_active) deck_client_start();

    if (s_transport == DECK_MODE_BLE) {
        strcpy(s_deckState.feedback, "Bluetooth mode saved");
    } else if (s_client_initialized) {
        snprintf(s_deckState.feedback, sizeof(s_deckState.feedback),
                 "%s saved (BLE stopped; reboot to free BT RAM)",
                 s_transport == DECK_MODE_WIFI ? "Wi-Fi mode" : "USB Only");
    } else {
        strcpy(s_deckState.feedback, transport_ready_feedback(s_transport));
    }
    s_deckState.dirty = true;
    return true;
}

void deck_client_handle_serial_line(const String& line) {
    if (!s_client_active || s_transport != DECK_MODE_USB) return;
    process_deck_line(line);
}

void deck_client_start() {
    s_client_active = true;
    s_rxBuffer = "";
    if (s_transport == DECK_MODE_USB) {
        s_deckState.linkState = DECK_LINK_SCANNING;
        s_deckState.lastRxMs = millis();
        s_lastPingMs = millis();
        strcpy(s_deckState.feedback, "USB Only active; waiting for deckd on serial");
        send_line(String("DECK HELLO v1 ") + DECK_DEVICE_ID, true);
        return;
    }
    if (s_transport == DECK_MODE_WIFI) {
        portENTER_CRITICAL(&s_wifi_mux);
        s_dial_generation++;
        s_dial_backoff_ms = 1000;
        s_next_dial_ms = 0;
        s_use_cached_host = (s_cached_host[0] != '\0' && s_cached_port != 0);
        portEXIT_CRITICAL(&s_wifi_mux);
        s_deckState.linkState = DECK_LINK_SCANNING;
        s_deckState.lastRxMs = millis();
        s_lastPingMs = millis();
        strcpy(s_deckState.feedback, "Searching for deckd over Wi-Fi (_mmdeckd._tcp)...");
        return;
    }
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
    if (s_deckState.linkState == DECK_LINK_DISCONNECTED) {
        s_lastScanMs = 0;
    }
#endif
}

void deck_client_stop() {
    if ((s_transport == DECK_MODE_USB || s_transport == DECK_MODE_WIFI) &&
        s_client_active && s_deckState.linkState == DECK_LINK_CONNECTED) {
        send_line("DECK BYE", true);
    }
    s_client_active = false;

    WiFiClient* stale_pending = nullptr;
    portENTER_CRITICAL(&s_wifi_mux);
    s_dial_generation++;
    stale_pending = s_pending_tcp;
    s_pending_tcp = nullptr;
    portEXIT_CRITICAL(&s_wifi_mux);
    if (stale_pending) {
        stale_pending->stop();
        delete stale_pending;
    }
    close_wifi_client();

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

static bool rx_timed_out(uint32_t timeout_ms) {
    uint32_t rx_ms = s_deckState.lastRxMs;
    uint32_t cur_ms = millis();
    return static_cast<int32_t>(cur_ms - rx_ms) > static_cast<int32_t>(timeout_ms);
}

void deck_client_loop() {
    if (!s_client_active) return;

    uint32_t now = millis();
    if (s_deckState.actionPending && now - s_pending_ms > 5000) {
        s_deckState.actionPending = false;
        strcpy(s_deckState.feedback, "No reply; action may have run. Not retried.");
    }

    if (s_transport == DECK_MODE_USB) {
        drain_stream_lines(Serial);
        if (s_deckState.linkState == DECK_LINK_CONNECTED) {
            if (rx_timed_out(15000)) {
                apply_fallback_labels();
                s_deckState.linkState = DECK_LINK_SCANNING;
                strcpy(s_deckState.feedback, "USB host timed out; waiting for deckd...");
            } else if (now - s_lastPingMs > 1500) {
                s_lastPingMs = now;
                send_line("DECK PING " + String(now, HEX));
            }
        }
        return;
    }

    if (s_transport == DECK_MODE_WIFI) {
        WiFiClient* new_tcp = nullptr;
        char new_host[40] = {};
        uint16_t new_port = 0;

        portENTER_CRITICAL(&s_wifi_mux);
        if (s_pending_tcp) {
            new_tcp = s_pending_tcp;
            s_pending_tcp = nullptr;
            strncpy(new_host, s_pending_host, sizeof(new_host) - 1);
            new_port = s_pending_port;
        }
        portEXIT_CRITICAL(&s_wifi_mux);

        if (new_tcp) {
            close_wifi_client();
            s_tcpClient = new_tcp;
            s_tcpClient->setNoDelay(true);
            s_rxBuffer = "";
            s_layout_ready = false;
            s_deckState.linkState = DECK_LINK_CONNECTED;
            s_deckState.lastRxMs = millis();
            s_lastPingMs = now;
            s_dial_backoff_ms = 1000;
            strcpy(s_deckState.feedback, "Connected over Wi-Fi");
            s_deckState.dirty = true;
            send_line(String("DECK HELLO v1 ") + DECK_DEVICE_ID, true);

            if (new_host[0] && new_port != 0 &&
                (strcmp(new_host, s_cached_host) != 0 || new_port != s_cached_port)) {
                strncpy(s_cached_host, new_host, sizeof(s_cached_host) - 1);
                s_cached_port = new_port;
                Preferences prefs;
                prefs.begin(DECK_NVS_NS, false);
                prefs.putString("deck_host", s_cached_host);
                prefs.putUShort("deck_port", s_cached_port);
                prefs.end();
            }
        }

        if (s_tcpClient) {
            drain_stream_lines(*s_tcpClient);
            if (!s_tcpClient) return;

            if (!s_tcpClient->connected()) {
                close_wifi_client();
                apply_fallback_labels();
                s_deckState.linkState = DECK_LINK_SCANNING;
                s_next_dial_ms = now + 1000;
                strcpy(s_deckState.feedback, "Wi-Fi deckd disconnected; reconnecting...");
                return;
            }
            if (rx_timed_out(15000)) {
                close_wifi_client();
                apply_fallback_labels();
                s_deckState.linkState = DECK_LINK_SCANNING;
                s_next_dial_ms = now + 1000;
                strcpy(s_deckState.feedback, "Wi-Fi host timed out; reconnecting...");
                return;
            }
            if (now - s_lastPingMs > 4000) {
                s_lastPingMs = now;
                send_line("DECK PING " + String(now, HEX));
            }
            return;
        }

        if (WiFi.status() != WL_CONNECTED) {
            s_deckState.linkState = DECK_LINK_SCANNING;
            return;
        }

        bool spawn_dial = false;
        uint32_t gen = 0;
        portENTER_CRITICAL(&s_wifi_mux);
        if (!s_dial_in_progress && (int32_t)(now - s_next_dial_ms) >= 0) {
            s_dial_in_progress = true;
            gen = s_dial_generation;
            spawn_dial = true;
        }
        portEXIT_CRITICAL(&s_wifi_mux);

        if (spawn_dial) {
            s_deckState.linkState = DECK_LINK_CONNECTING;
            if (xTaskCreatePinnedToCore(
                    wifi_dial_task, "deck_dial", 6144,
                    reinterpret_cast<void*>(static_cast<uintptr_t>(gen)),
                    1, nullptr, 0) != pdPASS) {
                portENTER_CRITICAL(&s_wifi_mux);
                s_dial_in_progress = false;
                s_next_dial_ms = now + 2000;
                portEXIT_CRITICAL(&s_wifi_mux);
            }
        }
        return;
    }

#if DECK_TRANSPORT_BLE
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
        if (rx_timed_out(15000)) {
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

const DeckLauncherState& deck_client_get_launcher() {
    return s_launcherState;
}

bool deck_client_is_connected() {
    return s_deckState.linkState == DECK_LINK_CONNECTED;
}

bool deck_client_controls_ready() {
    return deck_client_is_connected() && s_layout_ready && !s_deckState.actionPending;
}

bool deck_client_launcher_ready() {
    return deck_client_is_connected() && s_launcherState.ready && !s_deckState.actionPending;
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

bool deck_client_send_launcher_press(uint8_t appIndex) {
    if (!deck_client_launcher_ready() ||
        appIndex >= s_launcherState.count || !s_launcherState.items[appIndex].label[0]) {
        Serial.println("[deck] launcher press rejected: no synchronized launcher item");
        return false;
    }
    uint16_t seq = s_deckState.nextSeq++;
    s_pending_seq = seq;
    s_pending_ms = millis();
    s_deckState.actionPending = true;
    snprintf(s_deckState.feedback, sizeof(s_deckState.feedback),
             "Launching %s...", s_launcherState.items[appIndex].label);
    if (!send_line("DECK LAUNCHER PRESS " + String(appIndex) + " " + String(seq))) {
        s_deckState.actionPending = false;
        strcpy(s_deckState.feedback, "Launcher action send failed");
        return false;
    }
    return true;
}

void deck_client_scan_hosts() {
    if (s_transport != DECK_MODE_BLE) {
        strcpy(s_deckState.feedback, "Switch to Bluetooth to scan BLE hosts");
        return;
    }
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
    if (s_transport != DECK_MODE_BLE) {
        strcpy(s_deckState.feedback, "Bluetooth disabled in current mode");
        return false;
    }
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
