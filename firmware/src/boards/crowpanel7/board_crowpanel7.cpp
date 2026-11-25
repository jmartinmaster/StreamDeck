/*
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * This file is part of Stream Deck.
 *
 * Native ESP-IDF 5.x RGB LCD + PCA9557 + GT911 touch driver for
 * Elecrow CrowPanel 7.0" ESP32-S3 (800x480).
 */

#include "board_crowpanel7.h"

#include <Arduino.h>
#include <Wire.h>
#include <lvgl.h>
#include <algorithm>
#include <cmath>
#include <cstring>

#include "PCA9557.h"
#include "TAMC_GT911.h"

#include <esp_arduino_version.h>
#include <esp_heap_caps.h>
#include <esp_lcd_panel_ops.h>
#include <esp_lcd_panel_rgb.h>
#include <freertos/semphr.h>

static constexpr int PHYSICAL_WIDTH  = 800;
static constexpr int PHYSICAL_HEIGHT = 480;
static constexpr int BACKLIGHT_PIN   = 2;
static constexpr int I2C_SDA_PIN     = 19;
static constexpr int I2C_SCL_PIN     = 20;
static constexpr uint8_t GT911_ADDR_1 = 0x5D;
static constexpr uint8_t GT911_ADDR_2 = 0x14;
static constexpr int LV_BUF_LINES    = 20;
static constexpr int MAX_DIRTY_RECTS = 48;

static esp_lcd_panel_handle_t s_panel_handle = nullptr;
static PCA9557 s_pca9557;
static TAMC_GT911 s_ts(I2C_SDA_PIN, I2C_SCL_PIN, -1, -1, PHYSICAL_WIDTH, PHYSICAL_HEIGHT);

static lv_disp_draw_buf_t s_draw_buf;
static lv_disp_drv_t s_disp_drv;
static lv_indev_drv_t s_indev_drv;

static uint16_t* s_fbs[2] = {nullptr, nullptr};
static int s_back_idx = 1;  // driver scans out s_fbs[0] after init
static SemaphoreHandle_t s_frame_sem = nullptr;
static lv_area_t s_dirty[MAX_DIRTY_RECTS];
static int s_dirty_n = 0;
static lv_area_t s_dirty_overflow;
static bool s_has_overflow = false;
static bool s_dirty_full = false;
static bool s_swap_pending = false;
static volatile uint32_t s_lcd_frames = 0;

struct TouchData {
    bool isTouched;
    uint16_t x;
    uint16_t y;
};
static volatile TouchData s_latest_touch = {false, 0, 0};
static volatile bool s_press_latched = false;
static portMUX_TYPE s_touch_mux = portMUX_INITIALIZER_UNLOCKED;

static void touch_task(void* pvParameters) {
    (void)pvParameters;
    while (true) {
        s_ts.read();
        portENTER_CRITICAL(&s_touch_mux);
        if (s_ts.isTouched && s_ts.touches > 0) {
            s_latest_touch.isTouched = true;
            s_latest_touch.x = s_ts.points[0].x;
            s_latest_touch.y = s_ts.points[0].y;
            s_press_latched = true;
        } else {
            s_latest_touch.isTouched = false;
        }
        portEXIT_CRITICAL(&s_touch_mux);
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}

static bool IRAM_ATTR on_frame_done(esp_lcd_panel_handle_t,
                                    const esp_lcd_rgb_panel_event_data_t*,
                                    void*) {
    s_lcd_frames++;
    BaseType_t woken = pdFALSE;
    xSemaphoreGiveFromISR(s_frame_sem, &woken);
    return woken == pdTRUE;
}

static void copy_rect(uint16_t* dst, const uint16_t* src, const lv_area_t& a) {
    const int w = a.x2 - a.x1 + 1;
    const int h = a.y2 - a.y1 + 1;
    if (w == PHYSICAL_WIDTH) {
        const size_t off = static_cast<size_t>(a.y1) * PHYSICAL_WIDTH;
        memcpy(dst + off, src + off, static_cast<size_t>(w) * h * sizeof(uint16_t));
        return;
    }
    const size_t bytes = static_cast<size_t>(w) * sizeof(uint16_t);
    size_t off = static_cast<size_t>(a.y1) * PHYSICAL_WIDTH + a.x1;
    for (int y = 0; y < h; y++, off += PHYSICAL_WIDTH) {
        memcpy(dst + off, src + off, bytes);
    }
}

static inline void merge_area(lv_area_t& dst, const lv_area_t& src) {
    if (src.x1 < dst.x1) dst.x1 = src.x1;
    if (src.y1 < dst.y1) dst.y1 = src.y1;
    if (src.x2 > dst.x2) dst.x2 = src.x2;
    if (src.y2 > dst.y2) dst.y2 = src.y2;
}

static void complete_swap_sync() {
    const uint16_t* shown = s_fbs[s_back_idx];
    uint16_t* stale = s_fbs[1 - s_back_idx];
    if (s_dirty_full) {
        memcpy(stale, shown, PHYSICAL_WIDTH * PHYSICAL_HEIGHT * sizeof(uint16_t));
    } else {
        for (int i = 0; i < s_dirty_n; i++) copy_rect(stale, shown, s_dirty[i]);
        if (s_has_overflow) copy_rect(stale, shown, s_dirty_overflow);
    }
    s_back_idx = 1 - s_back_idx;
    s_dirty_n = 0;
    s_has_overflow = false;
    s_dirty_full = false;
    s_swap_pending = false;
}

static void finish_swap() {
    xSemaphoreTake(s_frame_sem, pdMS_TO_TICKS(120));
    complete_swap_sync();
}

void board_crowpanel7_poll_swap() {
    if (!s_swap_pending) return;
    if (xSemaphoreTake(s_frame_sem, 0) == pdTRUE) {
        complete_swap_sync();
    }
}

static void disp_flush_cb(lv_disp_drv_t* disp, const lv_area_t* area, lv_color_t* color_p) {
    if (s_swap_pending) finish_swap();

    uint16_t* back = s_fbs[s_back_idx];
    const int w = area->x2 - area->x1 + 1;
    const int h = area->y2 - area->y1 + 1;
    const uint16_t* src = reinterpret_cast<const uint16_t*>(color_p);
    if (w == PHYSICAL_WIDTH) {
        memcpy(back + static_cast<size_t>(area->y1) * PHYSICAL_WIDTH, src,
               static_cast<size_t>(w) * h * sizeof(uint16_t));
    } else {
        uint16_t* dst = back + static_cast<size_t>(area->y1) * PHYSICAL_WIDTH + area->x1;
        const size_t row_bytes = static_cast<size_t>(w) * sizeof(uint16_t);
        for (int y = 0; y < h; y++, dst += PHYSICAL_WIDTH, src += w) {
            memcpy(dst, src, row_bytes);
        }
    }

    if (!s_dirty_full) {
        if (s_dirty_n > 0 &&
            s_dirty[s_dirty_n - 1].x1 == area->x1 &&
            s_dirty[s_dirty_n - 1].x2 == area->x2 &&
            s_dirty[s_dirty_n - 1].y2 + 1 == area->y1) {
            s_dirty[s_dirty_n - 1].y2 = area->y2;
        } else if (s_dirty_n < MAX_DIRTY_RECTS) {
            s_dirty[s_dirty_n++] = *area;
        } else if (!s_has_overflow) {
            s_dirty_overflow = *area;
            s_has_overflow = true;
        } else {
            merge_area(s_dirty_overflow, *area);
        }
    }

    if (lv_disp_flush_is_last(disp)) {
        xSemaphoreTake(s_frame_sem, 0);
        uint32_t seq = s_lcd_frames;
        esp_lcd_panel_draw_bitmap(s_panel_handle, 0, 0, PHYSICAL_WIDTH, PHYSICAL_HEIGHT, back);
        if (s_lcd_frames != seq) xSemaphoreTake(s_frame_sem, 0);
        s_swap_pending = true;
    }
    lv_disp_flush_ready(disp);
}

static void touch_read_cb(lv_indev_drv_t* indev_driver, lv_indev_data_t* data) {
    (void)indev_driver;

    portENTER_CRITICAL(&s_touch_mux);
    bool touched = s_latest_touch.isTouched || s_press_latched;
    s_press_latched = false;
    uint16_t raw_x = s_latest_touch.x;
    uint16_t raw_y = s_latest_touch.y;
    portEXIT_CRITICAL(&s_touch_mux);

    if (touched) {
        data->state = LV_INDEV_STATE_PR;
        data->point.x = std::max<lv_coord_t>(0, std::min<lv_coord_t>(PHYSICAL_WIDTH - 1, raw_x));
        data->point.y = std::max<lv_coord_t>(0, std::min<lv_coord_t>(PHYSICAL_HEIGHT - 1, raw_y));
    } else {
        data->state = LV_INDEV_STATE_REL;
    }
}

struct PanelInit {
    const esp_lcd_rgb_panel_config_t* config;
    SemaphoreHandle_t done;
};

static void panel_init_task(void* arg) {
    PanelInit* p = static_cast<PanelInit*>(arg);
    ESP_ERROR_CHECK(esp_lcd_new_rgb_panel(p->config, &s_panel_handle));
    ESP_ERROR_CHECK(esp_lcd_panel_reset(s_panel_handle));
    ESP_ERROR_CHECK(esp_lcd_panel_init(s_panel_handle));

    void *fb1 = nullptr, *fb2 = nullptr;
    ESP_ERROR_CHECK(esp_lcd_rgb_panel_get_frame_buffer(s_panel_handle, 2, &fb1, &fb2));
    s_fbs[0] = static_cast<uint16_t*>(fb1);
    s_fbs[1] = static_cast<uint16_t*>(fb2);

    esp_lcd_rgb_panel_event_callbacks_t cbs = {};
    cbs.on_bounce_frame_finish = on_frame_done;
    ESP_ERROR_CHECK(esp_lcd_rgb_panel_register_event_callbacks(s_panel_handle, &cbs, nullptr));
    xSemaphoreGive(p->done);
    vTaskDelete(nullptr);
}

void board_crowpanel7_set_backlight(uint8_t percent) {
    percent = std::min<uint8_t>(100, percent);
    uint32_t duty = (percent == 0) ? 0 : static_cast<uint32_t>(std::pow(percent / 100.0, 2.0) * 255.0);
#if defined(ESP_ARDUINO_VERSION) && ESP_ARDUINO_VERSION >= ESP_ARDUINO_VERSION_VAL(3, 0, 0)
    ledcWrite(BACKLIGHT_PIN, duty);
#else
    ledcWrite(0, duty);
#endif
}

static void reset_gt911() {
    pinMode(38, OUTPUT);
    digitalWrite(38, LOW);
    pinMode(17, OUTPUT);
    digitalWrite(17, LOW);
    pinMode(18, OUTPUT);
    digitalWrite(18, LOW);
    pinMode(42, OUTPUT);
    digitalWrite(42, LOW);
    delay(20);

    Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
    Wire.setClock(400000);

    s_pca9557.reset();
    s_pca9557.setMode(IO_OUTPUT);
    s_pca9557.setState(IO0, IO_LOW);
    s_pca9557.setState(IO1, IO_LOW);
    delay(20);
    s_pca9557.setState(IO0, IO_HIGH);
    delay(100);
    s_pca9557.setMode(IO1, IO_INPUT);
    delay(50);

    Wire.beginTransmission(GT911_ADDR_1);
    uint8_t gt_addr = (Wire.endTransmission(true) == 0) ? GT911_ADDR_1 : GT911_ADDR_2;

    s_ts.begin(gt_addr);
    s_ts.setRotation(ROTATION_INVERTED);
    delay(20);
}

void board_crowpanel7_init(uint8_t initial_backlight) {
#if defined(ESP_ARDUINO_VERSION) && ESP_ARDUINO_VERSION >= ESP_ARDUINO_VERSION_VAL(3, 0, 0)
    ledcAttach(BACKLIGHT_PIN, 12000, 8);
#else
    ledcSetup(0, 12000, 8);
    ledcAttachPin(BACKLIGHT_PIN, 0);
#endif

    reset_gt911();

    esp_lcd_rgb_panel_config_t panel_config = {
        .clk_src = LCD_CLK_SRC_DEFAULT,
        .timings = {
            .pclk_hz = 13333333,
            .h_res = PHYSICAL_WIDTH,
            .v_res = PHYSICAL_HEIGHT,
            .hsync_pulse_width = 48,
            .hsync_back_porch = 40,
            .hsync_front_porch = 40,
            .vsync_pulse_width = 31,
            .vsync_back_porch = 13,
            .vsync_front_porch = 1,
            .flags = {
                .pclk_active_neg = 1,
            },
        },
        .data_width = 16,
        .bits_per_pixel = 16,
        .num_fbs = 2,
        .bounce_buffer_size_px = PHYSICAL_WIDTH * 16,
        .dma_burst_size = 64,
        .hsync_gpio_num = 39,
        .vsync_gpio_num = 40,
        .de_gpio_num = 41,
        .pclk_gpio_num = 0,
        .disp_gpio_num = -1,
        .data_gpio_nums = {
            15, 7, 6, 5, 4, 9, 46, 3, 8, 16, 1, 14, 21, 47, 48, 45
        },
        .flags = {
            .fb_in_psram = 1,
        },
    };

    s_frame_sem = xSemaphoreCreateBinary();
    PanelInit init = {&panel_config, xSemaphoreCreateBinary()};
    xTaskCreatePinnedToCore(panel_init_task, "panel_init", 4096, &init, 5, nullptr, 0);
    xSemaphoreTake(init.done, portMAX_DELAY);
    vSemaphoreDelete(init.done);

    lv_init();

    lv_color_t* lv_buf = static_cast<lv_color_t*>(heap_caps_malloc(
        PHYSICAL_WIDTH * LV_BUF_LINES * sizeof(lv_color_t), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    if (!lv_buf) {
        lv_buf = static_cast<lv_color_t*>(heap_caps_malloc(
            PHYSICAL_WIDTH * LV_BUF_LINES * sizeof(lv_color_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    }
    if (!s_fbs[0] || !s_fbs[1] || !lv_buf) {
        Serial.println("[board] Display buffer allocation failed!");
        abort();
    }
    lv_disp_draw_buf_init(&s_draw_buf, lv_buf, nullptr, PHYSICAL_WIDTH * LV_BUF_LINES);

    lv_disp_drv_init(&s_disp_drv);
    s_disp_drv.hor_res = PHYSICAL_WIDTH;
    s_disp_drv.ver_res = PHYSICAL_HEIGHT;
    s_disp_drv.flush_cb = disp_flush_cb;
    s_disp_drv.draw_buf = &s_draw_buf;
    s_disp_drv.user_data = s_panel_handle;
    lv_disp_drv_register(&s_disp_drv);

    lv_indev_drv_init(&s_indev_drv);
    s_indev_drv.type = LV_INDEV_TYPE_POINTER;
    s_indev_drv.read_cb = touch_read_cb;
    lv_indev_drv_register(&s_indev_drv);

    xTaskCreatePinnedToCore(touch_task, "TouchTask", 4096, nullptr, 2, nullptr, 0);

    board_crowpanel7_set_backlight(initial_backlight);
    Serial.println("[board] CrowPanel 7.0\" RGB + GT911 ready (800x480)");
}
