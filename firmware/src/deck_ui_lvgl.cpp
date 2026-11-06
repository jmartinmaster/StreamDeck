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

#include "deck_ui_lvgl.h"
#include "deck_client.h"

// Theme colors (Catppuccin Mocha style)
static const uint32_t C_GOOD   = 0xA6E3A1; // Green
static const uint32_t C_BAD    = 0xF38BA8; // Red
static const uint32_t C_WARN   = 0xF9E2AF; // Yellow
static const uint32_t C_MUTED  = 0xA6ADC8; // Slate / Subtext
static const uint32_t C_ACCENT = 0x89B4FA; // Blue / Accent
static const uint32_t C_IDLE   = 0x313244; // Surface0 / Dark slate

static lv_obj_t* deck_screen_root = nullptr;
static lv_obj_t* deck_title_lbl = nullptr;
static lv_obj_t* deck_status_dot = nullptr;
static lv_obj_t* deck_status_lbl = nullptr;
static lv_obj_t* deck_page_lbl = nullptr;
static lv_obj_t* deck_profile_dropdown = nullptr;
static lv_obj_t* deck_host_dropdown = nullptr;
static lv_obj_t* deck_feedback_lbl = nullptr;
static lv_obj_t* deck_group_titles[2] = {nullptr, nullptr};
static lv_obj_t* deck_btn_objs[DECK_MAX_BUTTONS] = {};
static lv_obj_t* deck_btn_labels[DECK_MAX_BUTTONS] = {};
static lv_obj_t* deck_btn_icons[DECK_MAX_BUTTONS] = {};

static const char* ui_icon(const char* name) {
    if (!name || !name[0]) return "";
    static const struct { const char* name; const char* symbol; } icons[] = {
        {"terminal", LV_SYMBOL_EDIT},
        {"folder", LV_SYMBOL_DIRECTORY},
        {"globe", LV_SYMBOL_WIFI},
        {"code", LV_SYMBOL_FILE},
        {"copy", LV_SYMBOL_COPY},
        {"paste", LV_SYMBOL_PASTE},
        {"camera", LV_SYMBOL_IMAGE},
        {"lock", LV_SYMBOL_SETTINGS},
        {"play", LV_SYMBOL_PLAY},
        {"pause", LV_SYMBOL_PAUSE},
        {"stop", LV_SYMBOL_STOP},
        {"mute", LV_SYMBOL_MUTE},
        {"prev", LV_SYMBOL_PREV},
        {"next", LV_SYMBOL_NEXT},
        {"volup", LV_SYMBOL_VOLUME_MAX},
        {"voldown", LV_SYMBOL_VOLUME_MID},
        {"refresh", LV_SYMBOL_REFRESH},
        {"power", LV_SYMBOL_POWER},
        {"home", LV_SYMBOL_HOME},
        {"close", LV_SYMBOL_CLOSE},
        {"ok", LV_SYMBOL_OK},
        {"warning", LV_SYMBOL_WARNING}
    };
    for (const auto& entry : icons) {
        if (strcmp(name, entry.name) == 0) return entry.symbol;
    }
    return LV_SYMBOL_BULLET;
}

static uint32_t ui_color(const char* name, uint32_t fallback) {
    if (!name || !name[0]) return fallback;
    if (name[0] == '#' && strlen(name) == 7) {
        return strtoul(name + 1, nullptr, 16);
    }
    if (strcmp(name, "good") == 0 || strcmp(name, "green") == 0) return C_GOOD;
    if (strcmp(name, "bad") == 0 || strcmp(name, "red") == 0) return C_BAD;
    if (strcmp(name, "warn") == 0 || strcmp(name, "yellow") == 0) return C_WARN;
    if (strcmp(name, "accent") == 0 || strcmp(name, "cyan") == 0) return C_ACCENT;
    if (strcmp(name, "idle") == 0 || strcmp(name, "muted") == 0) return C_MUTED;
    return fallback;
}

static void deck_host_cb(lv_event_t* e) {
    if (deck_host_dropdown) {
        deck_client_select_host(lv_dropdown_get_selected(deck_host_dropdown));
    }
}

static void deck_scan_cb(lv_event_t* e) {
    deck_client_scan_hosts();
}

static void deck_profile_cb(lv_event_t* e) {
    if (!deck_profile_dropdown) return;
    char profile[64] = {};
    if (lv_dropdown_get_selected(deck_profile_dropdown) != 0) {
        lv_dropdown_get_selected_str(deck_profile_dropdown, profile, sizeof(profile));
    }
    deck_client_select_profile(profile);
}

static void deck_btn_click_cb(lv_event_t* e) {
    if (lv_event_get_code(e) != LV_EVENT_CLICKED) return;
    intptr_t index = (intptr_t)lv_event_get_user_data(e);
    if (index >= 0 && index < DECK_MAX_BUTTONS) {
        deck_client_send_press((uint8_t)index);
    }
}

static void deck_prev_cb(lv_event_t* e) {
    if (lv_event_get_code(e) != LV_EVENT_CLICKED) return;
    deck_client_send_page_prev();
    deck_ui_update();
}

static void deck_next_cb(lv_event_t* e) {
    if (lv_event_get_code(e) != LV_EVENT_CLICKED) return;
    deck_client_send_page_next();
    deck_ui_update();
}

void deck_ui_update() {
    if (!deck_title_lbl || !lv_obj_is_valid(deck_title_lbl)) return;
    const DeckClientState& state = deck_client_get_state();

    if (deck_host_dropdown) {
        char options[512];
        uint16_t selected;
        deck_client_get_hosts(options, sizeof(options), &selected);
        bool open = lv_dropdown_is_open(deck_host_dropdown);
        if (strcmp(lv_dropdown_get_options(deck_host_dropdown), options) != 0) {
            if (open) {
                selected = lv_dropdown_get_selected(deck_host_dropdown);
                lv_dropdown_close(deck_host_dropdown);
            }
            lv_dropdown_set_options(deck_host_dropdown, options);
            lv_dropdown_set_selected(deck_host_dropdown, selected);
            if (open) lv_dropdown_open(deck_host_dropdown);
        } else if (!open) {
            lv_dropdown_set_selected(deck_host_dropdown, selected);
        }
    }

    if (deck_feedback_lbl) {
        char feedback[220];
        snprintf(feedback, sizeof(feedback), "%s%s%s | %s",
                 state.hostName[0] ? state.hostName : "No host identity",
                 state.profileOverride[0] ? " | Pinned: " : " | Follow focus",
                 state.profileOverride, state.feedback);
        lv_label_set_text(deck_feedback_lbl, feedback);
    }

    if (deck_profile_dropdown && !lv_dropdown_is_open(deck_profile_dropdown)) {
        if (state.profilesReady && deck_client_is_connected()) {
            if (strcmp(lv_dropdown_get_options(deck_profile_dropdown), state.profileOptions) != 0) {
                lv_dropdown_set_options(deck_profile_dropdown, state.profileOptions);
            }
            uint16_t selected = 0, index = 0;
            const char* option = state.profileOptions;
            while (*option) {
                const char* end = strchr(option, '\n');
                size_t len = end ? (size_t)(end - option) : strlen(option);
                if (index && strlen(state.profileOverride) == len &&
                    strncmp(option, state.profileOverride, len) == 0) selected = index;
                if (!end) break;
                option = end + 1;
                index++;
            }
            lv_dropdown_set_selected(deck_profile_dropdown, selected);
            lv_obj_clear_state(deck_profile_dropdown, LV_STATE_DISABLED);
        } else {
            lv_obj_add_state(deck_profile_dropdown, LV_STATE_DISABLED);
        }
    }

    char title_buf[80];
    if (state.focusApp[0]) {
        snprintf(title_buf, sizeof(title_buf), "%s - %s", state.focusApp, state.focusTitle);
    } else {
        snprintf(title_buf, sizeof(title_buf), "Stream Deck");
    }
    lv_label_set_text(deck_title_lbl, title_buf);

    if (deck_status_dot && lv_obj_is_valid(deck_status_dot)) {
        uint32_t dot_col = C_MUTED;
        const char* st_msg = "Offline";
        if (state.linkState == DECK_LINK_CONNECTED) {
            dot_col = C_GOOD;
            st_msg = "Connected";
        } else if (state.linkState == DECK_LINK_CONNECTING) {
            dot_col = C_WARN;
            st_msg = "Connecting...";
        } else if (state.linkState == DECK_LINK_SCANNING) {
            dot_col = C_WARN;
            st_msg = "Scanning...";
        }
        lv_obj_set_style_bg_color(deck_status_dot, lv_color_hex(dot_col), 0);
        if (deck_status_lbl && lv_obj_is_valid(deck_status_lbl)) {
            lv_label_set_text(deck_status_lbl, st_msg);
        }
    }

    if (deck_page_lbl && lv_obj_is_valid(deck_page_lbl)) {
        char p_str[16];
        snprintf(p_str, sizeof(p_str), "%u/%u", state.page, state.pageCount);
        lv_label_set_text(deck_page_lbl, p_str);
    }

    for (uint8_t g = 0; g < 2; g++) {
        if (deck_group_titles[g] && lv_obj_is_valid(deck_group_titles[g])) {
            const char* title_text = state.groupTitles[g][0] ? state.groupTitles[g] :
                (g == 0 ? "Quick Launch & Controls" : "Actions & Media");
            lv_label_set_text(deck_group_titles[g], title_text);
        }
    }

    for (uint8_t i = 0; i < DECK_MAX_BUTTONS; i++) {
        if (deck_btn_labels[i] && lv_obj_is_valid(deck_btn_labels[i])) {
            lv_label_set_text(deck_btn_labels[i], state.buttons[i].label);
        }
        if (deck_btn_icons[i] && lv_obj_is_valid(deck_btn_icons[i])) {
            const char* sym = ui_icon(state.buttons[i].icon[0] ? state.buttons[i].icon : state.buttons[i].label);
            lv_label_set_text(deck_btn_icons[i], sym);
        }
        if (deck_btn_objs[i] && lv_obj_is_valid(deck_btn_objs[i])) {
            if (deck_client_controls_ready() && state.buttons[i].label[0]) {
                lv_obj_clear_state(deck_btn_objs[i], LV_STATE_DISABLED);
                uint32_t col = ui_color(state.buttons[i].color, C_ACCENT);
                lv_obj_set_style_border_color(deck_btn_objs[i], lv_color_hex(col), 0);
            } else {
                lv_obj_add_state(deck_btn_objs[i], LV_STATE_DISABLED);
            }
        }
    }
}

lv_obj_t* deck_ui_create(lv_obj_t* parent) {
    deck_client_start();

    deck_screen_root = lv_obj_create(parent);
    lv_obj_set_size(deck_screen_root, LV_PCT(100), LV_PCT(100));
    lv_obj_set_style_bg_color(deck_screen_root, lv_color_hex(0x181825), 0);
    lv_obj_set_style_pad_all(deck_screen_root, 8, 0);
    lv_obj_set_style_pad_row(deck_screen_root, 6, 0);
    lv_obj_set_flex_flow(deck_screen_root, LV_FLEX_FLOW_COLUMN);

    // Header Row
    lv_obj_t* header = lv_obj_create(deck_screen_root);
    lv_obj_set_size(header, LV_PCT(100), LV_SIZE_CONTENT);
    lv_obj_set_style_bg_opa(header, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_opa(header, LV_OPA_TRANSP, 0);
    lv_obj_set_style_pad_all(header, 0, 0);
    lv_obj_set_flex_flow(header, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(header, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(header, 8, 0);

    // Status Dot
    deck_status_dot = lv_obj_create(header);
    lv_obj_set_size(deck_status_dot, 10, 10);
    lv_obj_set_style_radius(deck_status_dot, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_color(deck_status_dot, lv_color_hex(C_MUTED), 0);
    lv_obj_set_style_border_opa(deck_status_dot, LV_OPA_TRANSP, 0);

    // Title Label
    deck_title_lbl = lv_label_create(header);
    lv_label_set_text(deck_title_lbl, "Stream Deck");
    lv_obj_set_style_text_font(deck_title_lbl, &lv_font_montserrat_20, 0);
    lv_obj_set_style_text_color(deck_title_lbl, lv_color_hex(0xCDD6F4), 0);
    lv_obj_set_flex_grow(deck_title_lbl, 1);

    // Status Text
    deck_status_lbl = lv_label_create(header);
    lv_label_set_text(deck_status_lbl, "Searching...");
    lv_obj_set_style_text_color(deck_status_lbl, lv_color_hex(C_MUTED), 0);

    // Prev Button
    lv_obj_t* btn_prev = lv_btn_create(header);
    lv_obj_set_size(btn_prev, 40, 32);
    lv_obj_set_style_bg_color(btn_prev, lv_color_hex(C_IDLE), 0);
    lv_obj_t* l_prev = lv_label_create(btn_prev);
    lv_label_set_text(l_prev, LV_SYMBOL_LEFT);
    lv_obj_center(l_prev);
    lv_obj_add_event_cb(btn_prev, deck_prev_cb, LV_EVENT_CLICKED, nullptr);

    // Page Label
    deck_page_lbl = lv_label_create(header);
    lv_label_set_text(deck_page_lbl, "1/1");
    lv_obj_set_style_text_color(deck_page_lbl, lv_color_hex(C_ACCENT), 0);

    // Next Button
    lv_obj_t* btn_next = lv_btn_create(header);
    lv_obj_set_size(btn_next, 40, 32);
    lv_obj_set_style_bg_color(btn_next, lv_color_hex(C_IDLE), 0);
    lv_obj_t* l_next = lv_label_create(btn_next);
    lv_label_set_text(l_next, LV_SYMBOL_RIGHT);
    lv_obj_center(l_next);
    lv_obj_add_event_cb(btn_next, deck_next_cb, LV_EVENT_CLICKED, nullptr);

    // Controls Row (Host / Scan / Connect / Profile)
    lv_obj_t* ctrl_row = lv_obj_create(deck_screen_root);
    lv_obj_set_size(ctrl_row, LV_PCT(100), LV_SIZE_CONTENT);
    lv_obj_set_style_bg_opa(ctrl_row, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_opa(ctrl_row, LV_OPA_TRANSP, 0);
    lv_obj_set_style_pad_all(ctrl_row, 0, 0);
    lv_obj_set_flex_flow(ctrl_row, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(ctrl_row, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(ctrl_row, 8, 0);

    lv_obj_t* l_host = lv_label_create(ctrl_row);
    lv_label_set_text(l_host, "Host:");

    deck_host_dropdown = lv_dropdown_create(ctrl_row);
    lv_obj_set_width(deck_host_dropdown, 190);
    lv_dropdown_set_options(deck_host_dropdown, "Automatic (first available)");
    lv_obj_add_event_cb(deck_host_dropdown, deck_host_cb, LV_EVENT_VALUE_CHANGED, nullptr);

    lv_obj_t* scan = lv_btn_create(ctrl_row);
    lv_obj_set_size(scan, 65, 36);
    lv_obj_set_style_bg_color(scan, lv_color_hex(C_IDLE), 0);
    lv_obj_t* scan_label = lv_label_create(scan);
    lv_label_set_text(scan_label, "Scan");
    lv_obj_center(scan_label);
    lv_obj_add_event_cb(scan, deck_scan_cb, LV_EVENT_CLICKED, nullptr);

    lv_obj_t* connect = lv_btn_create(ctrl_row);
    lv_obj_set_size(connect, 80, 36);
    lv_obj_set_style_bg_color(connect, lv_color_hex(C_IDLE), 0);
    lv_obj_t* connect_label = lv_label_create(connect);
    lv_label_set_text(connect_label, "Connect");
    lv_obj_center(connect_label);
    lv_obj_add_event_cb(connect, deck_host_cb, LV_EVENT_CLICKED, nullptr);

    lv_obj_t* l_prof = lv_label_create(ctrl_row);
    lv_label_set_text(l_prof, "Profile:");

    deck_profile_dropdown = lv_dropdown_create(ctrl_row);
    lv_obj_set_width(deck_profile_dropdown, 160);
    lv_dropdown_set_options(deck_profile_dropdown, "Follow focus");
    lv_obj_add_state(deck_profile_dropdown, LV_STATE_DISABLED);
    lv_obj_add_event_cb(deck_profile_dropdown, deck_profile_cb, LV_EVENT_VALUE_CHANGED, nullptr);

    // Feedback status line
    deck_feedback_lbl = lv_label_create(deck_screen_root);
    lv_obj_set_width(deck_feedback_lbl, LV_PCT(100));
    lv_label_set_long_mode(deck_feedback_lbl, LV_LABEL_LONG_WRAP);
    lv_obj_set_style_text_color(deck_feedback_lbl, lv_color_hex(C_MUTED), 0);

    const DeckClientState& state = deck_client_get_state();

    // 2 Group Cards: 8 buttons per card
    for (uint8_t g = 0; g < 2; g++) {
        lv_obj_t* g_card = lv_obj_create(deck_screen_root);
        lv_obj_set_size(g_card, LV_PCT(100), LV_SIZE_CONTENT);
        lv_obj_set_style_bg_color(g_card, lv_color_hex(0x1E1E2E), 0);
        lv_obj_set_style_radius(g_card, 8, 0);
        lv_obj_set_style_pad_all(g_card, 8, 0);
        lv_obj_set_style_pad_row(g_card, 6, 0);
        lv_obj_set_flex_flow(g_card, LV_FLEX_FLOW_COLUMN);

        lv_obj_t* g_head = lv_obj_create(g_card);
        lv_obj_set_size(g_head, LV_PCT(100), LV_SIZE_CONTENT);
        lv_obj_set_style_bg_opa(g_head, LV_OPA_TRANSP, 0);
        lv_obj_set_style_border_opa(g_head, LV_OPA_TRANSP, 0);
        lv_obj_set_style_pad_all(g_head, 0, 0);
        lv_obj_set_flex_flow(g_head, LV_FLEX_FLOW_ROW);
        lv_obj_set_flex_align(g_head, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
        lv_obj_set_style_pad_column(g_head, 6, 0);

        lv_obj_t* dot = lv_obj_create(g_head);
        lv_obj_set_size(dot, 8, 8);
        lv_obj_set_style_radius(dot, LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_bg_color(dot, lv_color_hex(g == 0 ? C_ACCENT : C_GOOD), 0);
        lv_obj_set_style_border_opa(dot, LV_OPA_TRANSP, 0);

        const char* def_title = state.groupTitles[g][0] ? state.groupTitles[g] :
            (g == 0 ? "Quick Launch & Controls" : "Actions & Media");
        deck_group_titles[g] = lv_label_create(g_head);
        lv_label_set_text(deck_group_titles[g], def_title);
        lv_obj_set_style_text_color(deck_group_titles[g], lv_color_hex(g == 0 ? C_ACCENT : C_GOOD), 0);
        lv_obj_set_flex_grow(deck_group_titles[g], 1);

        lv_obj_t* b_row = lv_obj_create(g_card);
        lv_obj_set_size(b_row, LV_PCT(100), LV_SIZE_CONTENT);
        lv_obj_set_style_bg_opa(b_row, LV_OPA_TRANSP, 0);
        lv_obj_set_style_border_opa(b_row, LV_OPA_TRANSP, 0);
        lv_obj_set_style_pad_all(b_row, 0, 0);
        lv_obj_set_flex_flow(b_row, LV_FLEX_FLOW_ROW);
        lv_obj_set_flex_align(b_row, LV_FLEX_ALIGN_SPACE_BETWEEN, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
        lv_obj_set_style_pad_column(b_row, 6, 0);

        for (uint8_t col = 0; col < 8; col++) {
            uint8_t i = g * 8 + col;
            lv_obj_t* btn = lv_btn_create(b_row);
            deck_btn_objs[i] = btn;
            lv_obj_set_size(btn, 88, 66);
            lv_obj_set_style_radius(btn, 8, 0);
            lv_obj_set_style_bg_color(btn, lv_color_hex(0x232931), 0);
            lv_obj_set_style_border_width(btn, 2, 0);
            uint32_t col_hex = ui_color(state.buttons[i].color, C_ACCENT);
            lv_obj_set_style_border_color(btn, lv_color_hex(col_hex), 0);
            lv_obj_set_style_bg_color(btn, lv_color_hex(0x393E46), LV_STATE_PRESSED);
            lv_obj_set_style_pad_all(btn, 2, 0);
            lv_obj_set_style_pad_gap(btn, 2, 0);
            lv_obj_set_flex_flow(btn, LV_FLEX_FLOW_COLUMN);
            lv_obj_set_flex_align(btn, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);

            const char* icon_str = ui_icon(state.buttons[i].icon[0] ? state.buttons[i].icon : state.buttons[i].label);
            lv_obj_t* icon_lbl = lv_label_create(btn);
            lv_label_set_text(icon_lbl, icon_str);
            lv_obj_set_style_text_color(icon_lbl, lv_color_hex(C_ACCENT), 0);
            deck_btn_icons[i] = icon_lbl;

            lv_obj_t* lbl = lv_label_create(btn);
            lv_label_set_text(lbl, state.buttons[i].label);
            lv_obj_set_style_text_font(lbl, &lv_font_montserrat_12, 0);
            lv_label_set_long_mode(lbl, LV_LABEL_LONG_CLIP);
            lv_obj_set_width(lbl, 82);
            lv_obj_set_style_text_align(lbl, LV_TEXT_ALIGN_CENTER, 0);
            deck_btn_labels[i] = lbl;

            lv_obj_add_event_cb(btn, deck_btn_click_cb, LV_EVENT_CLICKED, (void*)(intptr_t)i);
        }
    }

    deck_ui_update();
    return deck_screen_root;
}
