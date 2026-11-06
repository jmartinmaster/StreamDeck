#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This file is part of Stream Deck.
#
# Stream Deck is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Stream Deck is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Stream Deck.  If not, see <https://www.gnu.org/licenses/>.
"""Compile the production Deck parser against a small Arduino String stub to verify protocol parsing."""

from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]

def main():
    source = (ROOT / "src/deck_client.cpp").read_text()
    parser = source[source.index("static void process_deck_line("):
                    source.index("#if DECK_TRANSPORT_BLE\nstatic void on_notify_data")]
    remember = source[source.index("static bool remember_host("):
                      source.index("class AdvertisedDeviceCallbacks")]
    host_api = source[source.index("void deck_client_scan_hosts("):
                      source.index("bool deck_client_send_page_next(")]
    header = (ROOT / "include/deck_client.h").read_text()
    types = header[header.index("enum DeckLinkState"):header.index("// Lifecycle")]

    harness = r'''
#include <cassert>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <string>
#include <cstdio>

#define DECK_MAX_BUTTONS 16
#define DECK_MAX_LABEL_LEN 20

class String : public std::string {
public:
    using std::string::string;
    using std::string::operator=;
    String(const std::string& value) : std::string(value) {}
    bool startsWith(const char* prefix) const { return rfind(prefix, 0) == 0; }
    String substring(size_t start) const { return substr(start); }
    String substring(size_t start, size_t end) const { return substr(start, end - start); }
    int indexOf(char ch, size_t start = 0) const {
        size_t pos = find(ch, start); return pos == npos ? -1 : (int)pos;
    }
    long toInt() const { return strtol(c_str(), nullptr, 10); }
    void toCharArray(char* output, size_t size) const {
        if (size) { strncpy(output, c_str(), size - 1); output[size - 1] = 0; }
    }
    friend String operator+(const char* left, const String& right) {
        return String(std::string(left) + std::string(right));
    }
};

struct { void println(const String&) {} } Serial;
static uint32_t millis() { return 123; }
static String sent;
static bool send_line(const String& line) { sent = line; return true; }
'''

    state = r'''
static DeckClientState s_deckState = {};
static bool s_layout_ready;
static uint16_t s_pending_seq;
static bool s_choose_host, s_scan_requested;
static char s_preferred_address[18] = {};
static struct { char address[18]; char label[48]; } s_hosts[8];
static uint8_t s_host_count;
static uint16_t s_host_selected;
static int critical_depth, s_hosts_mux;
#define portENTER_CRITICAL(mux) do { (void)(mux); assert(!critical_depth); critical_depth++; } while(0)
#define portEXIT_CRITICAL(mux) do { (void)(mux); assert(critical_depth == 1); critical_depth--; } while(0)
'''

    checks = r'''
int main() {
    process_deck_line("DECK HOST\tlaptop");
    assert(strcmp(s_deckState.hostName, "laptop") == 0);
    s_deckState.actionPending = true; s_pending_seq = 5;
    process_deck_line("DECK ACK 4");
    assert(s_deckState.actionPending);
    process_deck_line("DECK ACK 5");
    assert(!s_deckState.actionPending && strstr(s_deckState.feedback, "accepted"));
    s_deckState.actionPending = true;
    process_deck_line("DECK ERR 6 unknown key");
    assert(!s_deckState.actionPending && strstr(s_deckState.feedback, "unknown key"));
    process_deck_line("DECK PROFILES BEGIN");
    process_deck_line("DECK PROFILE default");
    process_deck_line("DECK PROFILES END");
    assert(s_deckState.profilesReady);
    process_deck_line("DECK SELECTED\tdefault");
    assert(strcmp(s_deckState.profileOverride, "default") == 0);
    process_deck_line("DECK FOCUS code\tEditor");
    assert(strcmp(s_deckState.focusApp, "code") == 0);
    process_deck_line("DECK LAYOUT 1/1\tCopy\tPaste");
    assert(s_layout_ready && strcmp(s_deckState.buttons[0].label, "Copy") == 0);
    process_deck_line("DECK BYE busy");
    assert(!s_layout_ready && strstr(s_deckState.feedback, "busy"));
    process_deck_line("DECK PING 1234");
    assert(sent == "DECK PONG 1234");
    const char* laptop = "01:02:03:04:05:06";
    const char* desktop = "07:08:09:10:11:12";
    assert(remember_host(laptop, "DeckD-laptop"));
    assert(remember_host(desktop, "DeckD-desktop"));
    assert(s_host_count == 2);
    assert(deck_client_select_host(2));
    assert(!remember_host(laptop, "") && remember_host(desktop, ""));
    assert(s_host_count == 2 && s_scan_requested);
    char options[512];
    uint16_t selected;
    deck_client_get_hosts(options, sizeof(options), &selected);
    assert(selected == 2 && strstr(options, "DeckD-desktop"));
    assert(!deck_client_select_host(3));
    deck_client_scan_hosts();
    assert(s_choose_host && s_host_count == 0 && !s_preferred_address[0]);
    assert(!remember_host(laptop, "DeckD-laptop"));
    assert(deck_client_select_host(0));
    assert(remember_host(desktop, "DeckD-desktop"));
    for (int i = 0; i < 10; ++i) {
        char address[18];
        snprintf(address, sizeof(address), "11:22:33:44:55:%02x", i);
        remember_host(address, "DeckD-extra");
    }
    assert(s_host_count == 8);
    deck_client_get_hosts(options, sizeof(options), &selected);
    assert(strlen(options) < sizeof(options));
    puts("PASS Deck production parser and host picker: identity, ACK/errors, explicit host, deduplication and bounds");
    return 0;
}
'''
    with tempfile.TemporaryDirectory() as temp:
        src = Path(temp) / "parser.cpp"
        binary = Path(temp) / "parser"
        src.write_text(harness + types + state + parser + remember + host_api + checks)
        subprocess.run(["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                        str(src), "-o", str(binary)], check=True)
        subprocess.run([str(binary)], check=True)

if __name__ == "__main__":
    main()
