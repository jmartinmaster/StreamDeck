/**
 * @file lv_conf.h
 * LVGL v8.3 configuration for ESP32-S3 Stream Deck panels
 */

#ifndef LV_CONF_H
#define LV_CONF_H

#include <stdint.h>

#define LV_COLOR_DEPTH 16
#define LV_COLOR_16_SWAP 0

#define LV_USE_LOG 0

#ifdef BOARD_HAS_PSRAM
#define LV_MEM_CUSTOM 1
#define LV_MEM_CUSTOM_INCLUDE <esp32-hal-psram.h>
#define LV_MEM_CUSTOM_ALLOC   ps_malloc
#define LV_MEM_CUSTOM_FREE    free
#define LV_MEM_CUSTOM_REALLOC ps_realloc
#else
#define LV_MEM_CUSTOM 0
#define LV_MEM_SIZE (48U * 1024U)
#endif

#define LV_TICK_CUSTOM 1
#define LV_TICK_CUSTOM_INCLUDE <Arduino.h>
#define LV_TICK_CUSTOM_SYS_TIME_EXPR (millis())

#define LV_DISP_DEF_REFR_PERIOD 16
#define LV_INDEV_DEF_READ_PERIOD 10

#ifdef BOARD_HAS_PSRAM
#include <esp_attr.h>
#define LV_ATTRIBUTE_FAST_MEM IRAM_ATTR
#endif

/* Fonts */
#define LV_FONT_MONTSERRAT_12 1
#define LV_FONT_MONTSERRAT_14 1
#define LV_FONT_MONTSERRAT_16 1
#define LV_FONT_MONTSERRAT_18 1
#define LV_FONT_MONTSERRAT_20 1
#define LV_FONT_MONTSERRAT_24 1
#define LV_FONT_DEFAULT &lv_font_montserrat_14

/* Widgets */
#define LV_USE_ARC 0
#define LV_USE_BAR 0
#define LV_USE_BTN 1
#define LV_USE_BTNMATRIX 1
#define LV_USE_CANVAS 0
#define LV_USE_CHECKBOX 1
#define LV_USE_DROPDOWN 1
#define LV_USE_IMG 1
#define LV_USE_KEYBOARD 1
#define LV_USE_LABEL 1
#define LV_USE_LINE 0
#define LV_USE_ROLLER 0
#define LV_USE_SLIDER 0
#define LV_USE_SWITCH 0
#define LV_USE_TEXTAREA 1
#define LV_USE_TABLE 0
#define LV_USE_MSGBOX 0
#define LV_USE_TABVIEW 0
#define LV_USE_COLORWHEEL 0
#define LV_USE_ANIMIMG 0
#define LV_USE_CALENDAR 0
#define LV_USE_CHART 0
#define LV_USE_IMGBTN 0
#define LV_USE_LED 0
#define LV_USE_LIST 0
#define LV_USE_MENU 0
#define LV_USE_METER 0
#define LV_USE_SPAN 0
#define LV_USE_SPINBOX 0
#define LV_USE_SPINNER 0
#define LV_USE_TILEVIEW 0
#define LV_USE_WIN 0

/* Layouts */
#define LV_USE_FLEX 1
#define LV_USE_GRID 0

#endif /* LV_CONF_H */
