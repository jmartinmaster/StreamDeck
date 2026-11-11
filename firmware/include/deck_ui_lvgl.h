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

#ifdef USE_LVGL_UI

#include <lvgl.h>

// Creates the full Stream Deck screen inside the given parent container/screen.
lv_obj_t* deck_ui_create(lv_obj_t* parent);

// Call periodically (e.g. in your UI loop / timer) to sync UI elements with deck_client state.
void deck_ui_update();

#endif // USE_LVGL_UI
