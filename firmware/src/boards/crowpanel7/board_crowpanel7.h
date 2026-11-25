/*
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * This file is part of Stream Deck.
 *
 * Native ESP-IDF 5.x RGB LCD + PCA9557 + GT911 touch driver for
 * Elecrow CrowPanel 7.0" ESP32-S3 (800x480).
 */

#pragma once

#include <stdint.h>

// Initializes backlight PWM, PCA9557/GT911 touch controller, double-buffered
// 800x480 RGB LCD panel, LVGL core, and LVGL display/touch input drivers.
void board_crowpanel7_init(uint8_t initial_backlight = 100);

// Sets panel backlight brightness (0..100%).
void board_crowpanel7_set_backlight(uint8_t percent);

// Completes any pending double-buffer swap once the RGB DMA finishes scan-out.
void board_crowpanel7_poll_swap();
