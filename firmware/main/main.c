#include "sdkconfig.h"
#include "protocol.h"
#include "ble.h"
#include "link.h"
#include "ota.h"
#include "ota_wire.h"
#include "esp_app_desc.h"
#include "esp_ota_ops.h"
#include <inttypes.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>
#include "nvs_flash.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_task_wdt.h"
#include "esp_timer.h"
#include "driver/usb_serial_jtag.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"

#define COMMAND_QUEUE_SIZE 16
#define TASK_POLL_MS 100
/* USB is deliberately lossy. Wi-Fi has its own queue and never waits for USB. */
typedef struct { char data[IH_OUTPUT_MAX + 2]; size_t length; } ih_output;
static QueueHandle_t commands, stops, output;
static TaskHandle_t executor;
static _Atomic uint32_t out_drops, cancel_epoch;
static uint32_t active_epoch;
static int64_t command_deadline;
static unsigned step = 8, interval_ms = 15;
static uint8_t buttons;
static uint32_t button_generation;

static void usb_enqueue(const char *data, size_t length)
{
    ih_output message = {0};
    message.length = length < sizeof(message.data) ? length : sizeof(message.data);
    memcpy(message.data, data, message.length);
    if (xQueueSend(output, &message, 0) != pdTRUE) atomic_fetch_add(&out_drops, 1);
}
static void publish(bool serial, uint32_t session, const char *line)
{
    /* Dispatch TCP first, outside the USB task/queue. Session access is atomic. */
    if (!ih_wifi_output(session, line)) atomic_fetch_add(&out_drops, 1);
    if (serial) {
        char wire[IH_OUTPUT_MAX + 2];
        /* IDF logs may arrive as prefix/body/newline fragments. A preceding
         * log fragment (or a dropped log newline) must not hide the @ marker. */
        int n = snprintf(wire, sizeof(wire), "\n%s\n", line);
        usb_enqueue(wire, n < (int)sizeof(wire) ? (size_t)n : sizeof(wire) - 1);
    }
}
void ih_emit(const char *fmt, ...)
{
    char line[IH_OUTPUT_MAX];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(line, sizeof(line), fmt, ap);
    va_end(ap);
    publish(true, ih_wifi_session(), line);
}
/* Keep GAP diagnostics, but don't let ESP_LOG's USB sink stall NimBLE either.
 * IDF can call this with fragments; preserve bytes rather than adding LF. */
static int log_output(const char *fmt, va_list ap)
{
    char data[IH_OUTPUT_MAX];
    int n = vsnprintf(data, sizeof(data), fmt, ap);
    if (n > 0) usb_enqueue(data, n < (int)sizeof(data) ? (size_t)n : sizeof(data) - 1);
    return n;
}
static void watchdog_feed(void) { ESP_ERROR_CHECK(esp_task_wdt_reset()); }
static void output_task(void *arg)
{
    (void)arg;
    ESP_ERROR_CHECK(esp_task_wdt_add(NULL));
    ih_output message;
    for (;;) {
        watchdog_feed();
        if (xQueueReceive(output, &message, pdMS_TO_TICKS(TASK_POLL_MS)) != pdTRUE) continue;
        /* is_connected detects USB SOF, not an open CDC reader. Even on an
         * enumerated bus with no reader, a zero-wait write cannot stall us.
         * Driver sends the whole ring-buffer item or zero; never partial lines.
         * Do not use printf/fflush or wait_tx_done here. See README citations. */
        if (!usb_serial_jtag_is_connected() ||
            usb_serial_jtag_write_bytes(message.data, message.length, 0) != (int)message.length)
            atomic_fetch_add(&out_drops, 1);
    }
}
static ih_error command_guard(void)
{
    if (active_epoch != atomic_load(&cancel_epoch)) return IH_CANCELLED;
    if (esp_timer_get_time() >= command_deadline) return IH_TIMEOUT;
    return IH_OK;
}
static ih_error delay_ms(unsigned ms)
{
    int64_t until = esp_timer_get_time() + (int64_t)ms * 1000;
    for (;;) {
        ih_error e = command_guard();
        if (e != IH_OK) return e;
        int64_t left = until - esp_timer_get_time();
        if (left <= 0) return IH_OK;
        unsigned wait = (unsigned)((left + 999) / 1000);
        if (wait > TASK_POLL_MS) wait = TASK_POLL_MS;
        watchdog_feed();
        /* STOP ingress wakes this wait immediately. */
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(wait));
    }
}
static ih_error send_report(uint8_t id, const uint8_t *data, size_t n, uint32_t gen)
{
    ih_error e = command_guard();
    if (e != IH_OK) return e;
    watchdog_feed();
    return ih_ble_send(id, data, n, gen, IH_NOTIFY_TIMEOUT_MS, command_guard);
}
static ih_error mouse(uint8_t mask, int16_t x, int16_t y, int8_t v, int8_t h, uint32_t gen)
{
    uint16_t ux = (uint16_t)x, uy = (uint16_t)y;
    uint8_t data[7] = {mask, ux & 255, ux >> 8, uy & 255, uy >> 8, (uint8_t)v, (uint8_t)h};
    return send_report(2, data, sizeof(data), gen);
}
static bool is_hid(ih_kind kind)
{
    return kind == IH_MOVE || kind == IH_SCROLL || kind == IH_BTN || kind == IH_CLICK ||
           kind == IH_KEY || kind == IH_KREPORT || kind == IH_CONSUMER;
}
static ih_error release_all(void)
{
    buttons = 0;
    ih_ble_status status;
    ih_error e = ih_ble_get_status(&status);
    if (e != IH_OK || !status.conn) return e;
    const uint8_t zero[8] = {0};
    const size_t sizes[] = {0, 8, 7, 2};
    /* Safety cleanup has a separate *total* two-second budget. It ignores STOP
     * and the expired action budget; failure disconnects to clear latched HID. */
    int64_t until = esp_timer_get_time() + IH_NOTIFY_TIMEOUT_MS * 1000LL;
    for (uint8_t id = 1; id <= 3; ++id) {
        int64_t left = until - esp_timer_get_time();
        e = left > 0 ? ih_ble_send(id, zero, sizes[id], status.generation,
                                  (unsigned)((left + 999) / 1000), NULL) : IH_TIMEOUT;
        if (e != IH_OK) { ih_ble_abort(status.generation); return e; }
    }
    return IH_OK;
}
static const char *reset_name(esp_reset_reason_t reason)
{
    switch (reason) {
    case ESP_RST_POWERON: return "poweron";
    case ESP_RST_SW: return "software";
    case ESP_RST_PANIC: return "panic";
    case ESP_RST_INT_WDT: return "int_wdt";
    case ESP_RST_TASK_WDT: return "task_wdt";
    case ESP_RST_WDT: return "wdt";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_DEEPSLEEP: return "deepsleep";
    case ESP_RST_USB: return "usb";
    default: return "other";
    }
}
static ih_error execute(const ih_command *c, char *detail, size_t detail_size)
{
    ih_ble_status status;
    ih_error e;
    switch (c->kind) {
    case IH_PING: {
        char elf[65];
        /* The IDF helper defaults to nine hex characters, regardless of dst size. */
        const uint8_t *hash = esp_app_get_description()->app_elf_sha256;
        for (unsigned i = 0; i < 32; ++i) snprintf(elf + 2*i, 3, "%02x", hash[i]);
        snprintf(detail, detail_size, " fw=%s chip=%s elf=%s slot=%s", esp_app_get_description()->version, CONFIG_IDF_TARGET, elf,
                 esp_ota_get_running_partition()->label);
        size_t n = strlen(detail);
        ih_ota_status(detail + n, detail_size - n);
        return IH_OK;
    }
    case IH_STOP: return release_all();
    case IH_PAIR: return ih_ble_pair();
    case IH_CFG:
        if (c->has_step) step = (unsigned)c->args[0];
        if (c->has_interval) interval_ms = (unsigned)c->args[1];
        snprintf(detail, detail_size, " step=%u interval=%u", step, interval_ms);
        return IH_OK;
    case IH_NAME: case IH_UNPAIR:
        buttons = 0;
        return ih_ble_restart(c->name, c->kind == IH_UNPAIR);
    case IH_WIFI: case IH_TOKEN:
        return ih_wifi_provision(c);
    case IH_OTA: return IH_INTERNAL; /* Handled with session metadata in executor. */
    case IH_STATUS: {
        bool up; char ip[16];
        ih_wifi_status(&up, ip, sizeof(ip));
        e = ih_ble_get_status(&status);
        if (e == IH_OK) snprintf(detail, detail_size,
            " conn=%u ready=%u bonded=%d peer=%s wifi=%u ip=%s pairing=%u out_drops=%" PRIu32
            " uptime_ms=%" PRIu64 " reset=%s reset_code=%d",
            status.conn, status.ready, status.bonded, status.peer, up, ip, status.pairing,
            atomic_load(&out_drops), (uint64_t)(esp_timer_get_time() / 1000),
            reset_name(esp_reset_reason()), (int)esp_reset_reason());
        return e;
    }
    default: break;
    }
    e = command_guard();
    if (e != IH_OK) return e;
    e = ih_ble_get_status(&status);
    if (e != IH_OK) return e;
    if (!status.conn) return IH_NOCONN;
    if (button_generation != status.generation) { buttons = 0; button_generation = status.generation; }
    uint32_t gen = status.generation;
    switch (c->kind) {
    case IH_MOVE: case IH_SCROLL: {
        /* Defense in depth for callers which bypass the parser. */
        if (c->args[0] < -IH_DELTA_MAX || c->args[0] > IH_DELTA_MAX ||
            c->args[1] < -IH_DELTA_MAX || c->args[1] > IH_DELTA_MAX) return IH_ARG;
        int32_t x = c->args[0], y = c->args[1];
        int16_t dx, dy;
        bool first = true;
        while (ih_next_step(&x, &y, c->kind == IH_MOVE ? step : 127, &dx, &dy)) {
            if (!first && (e = delay_ms(interval_ms)) != IH_OK) return e;
            first = false;
            e = c->kind == IH_MOVE ? mouse(buttons, dx, dy, 0, 0, gen) : mouse(buttons, 0, 0, (int8_t)dx, (int8_t)dy, gen);
            if (e != IH_OK) return e;
        }
        return first ? mouse(buttons, 0, 0, 0, 0, gen) : IH_OK;
    }
    case IH_BTN:
        e = mouse((uint8_t)c->args[0], 0, 0, 0, 0, gen);
        if (e == IH_OK) buttons = (uint8_t)c->args[0];
        return e;
    case IH_CLICK:
        e = mouse(buttons | (uint8_t)c->args[0], 0, 0, 0, 0, gen);
        if (e != IH_OK || (e = delay_ms((unsigned)c->args[1])) != IH_OK) return e;
        return mouse(buttons, 0, 0, 0, 0, gen);
    case IH_KEY: case IH_KREPORT: {
        uint8_t data[8] = {(uint8_t)c->args[0], 0};
        for (size_t i = 0; i < 6; ++i) data[i+2] = (uint8_t)c->args[i+1];
        if (c->kind == IH_KEY) memset(data+3, 0, 5);
        e = send_report(1, data, sizeof(data), gen);
        if (e != IH_OK || c->kind == IH_KREPORT) return e;
        if ((e = delay_ms((unsigned)c->args[2])) != IH_OK) return e;
        memset(data, 0, sizeof(data));
        return send_report(1, data, sizeof(data), gen);
    }
    case IH_CONSUMER: {
        uint8_t data[2] = {c->args[0] & 255, (uint32_t)c->args[0] >> 8};
        e = send_report(3, data, sizeof(data), gen);
        if (e != IH_OK || (e = delay_ms((unsigned)c->args[1])) != IH_OK) return e;
        memset(data, 0, sizeof(data));
        return send_report(3, data, sizeof(data), gen);
    }
    default: return IH_INTERNAL;
    }
}
static void reply(const ih_request *request, ih_error e, const char *detail)
{
    if (request->frame && !request->command.id) {
        if (e != IH_OK) ih_wifi_output(request->session, "@0 EVT ota_error");
        return;
    }
    char line[IH_OUTPUT_MAX];
    if (e == IH_OK) snprintf(line, sizeof(line), "@%" PRIu32 " OK%s", request->command.id, detail);
    else snprintf(line, sizeof(line), "@%" PRIu32 " ERR %s%s", request->command.id, ih_error_name(e),
                  request->command.kind == IH_STATUS ? detail : "");
    publish(!request->tcp, request->session, line);
}
static void executor_task(void *arg)
{
    (void)arg;
    ESP_ERROR_CHECK(esp_task_wdt_add(NULL));
    ih_request request;
    for (;;) {
        watchdog_feed();
        ih_ota_poll();
        if (xQueueReceive(stops, &request, 0) != pdTRUE && xQueueReceive(commands, &request, 0) != pdTRUE) {
            ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(TASK_POLL_MS));
            continue;
        }
        ih_command *c = &request.command;
        ih_check_transport(c, request.tcp);
        active_epoch = request.cancel_epoch;
        command_deadline = esp_timer_get_time() + IH_COMMAND_BUDGET_MS * 1000LL;
        char detail[IH_OUTPUT_MAX - 32] = "";
        ih_error e = c->error;
        bool executing_hid = e == IH_OK && is_hid(c->kind);
        if (e == IH_OK) e = c->kind == IH_OTA ? ih_ota_execute(&request, detail, sizeof(detail)) :
                                                   execute(c, detail, sizeof(detail));
        if (e == IH_OK && c->kind == IH_STATUS && request.tcp) {
            bool up; char ip[16];
            ih_wifi_status(&up, ip, sizeof(ip));
            ih_error confirm = ih_ota_confirm(request.session, up);
            size_t n = strlen(detail);
            snprintf(detail + n, sizeof(detail) - n, " ota_confirm=%s", ih_error_name(confirm));
            n = strlen(detail);
            ih_ota_status(detail + n, sizeof(detail) - n);
        }
        if (e == IH_OK && c->kind == IH_STATUS && !request.tcp) {
            size_t n = strlen(detail);
            ih_ota_status(detail + n, sizeof(detail) - n);
        }
        if (e != IH_OK && c->kind == IH_STATUS) ih_ota_status(detail, sizeof(detail));
        if (e == IH_OK && executing_hid) e = command_guard();
        if (e != IH_OK && e != IH_ARG && executing_hid) (void)release_all();
        reply(&request, e, detail);
        ih_ota_wire_release(request.frame);
        memset(&request, 0, sizeof(request));
    }
}
void ih_submit(ih_request *request)
{
    ih_check_transport(&request->command, request->tcp);
    QueueHandle_t queue = commands;
    if (request->command.error == IH_OK && request->command.kind == IH_STOP) {
        /* Fence current and previously queued HID actions even if STOP's own
         * priority queue is full. No BLE API executes in an ingress task. */
        atomic_fetch_add(&cancel_epoch, 1);
        ih_ble_wake_send();
        queue = stops;
    }
    request->cancel_epoch = atomic_load(&cancel_epoch);
    if (xQueueSend(queue, request, 0) != pdTRUE) {
        reply(request, request->command.error == IH_OK ? IH_BUSY : request->command.error, "");
        ih_ota_wire_release(request->frame);
    }
    xTaskNotifyGive(executor);
}
void app_main(void)
{
    ESP_ERROR_CHECK(nvs_flash_init());
    commands = xQueueCreate(COMMAND_QUEUE_SIZE, sizeof(ih_request));
    stops = xQueueCreate(4, sizeof(ih_request));
    output = xQueueCreate(64, sizeof(ih_output));
    configASSERT(commands && stops && output);
    esp_task_wdt_config_t wdt = {.timeout_ms = 15000, .idle_core_mask = 0, .trigger_panic = true};
    esp_err_t err = esp_task_wdt_reconfigure(&wdt);
    if (err == ESP_ERR_INVALID_STATE) err = esp_task_wdt_init(&wdt);
    ESP_ERROR_CHECK(err);
    usb_serial_jtag_driver_config_t usb = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    usb.rx_buffer_size = 4096;
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&usb));
    /* Leave stdout's VFS untouched: protocol and runtime diagnostics use our
     * zero-wait sink. Boot/ROM diagnostics retain the IDF console fallback. */
    esp_log_set_vprintf(log_output);
    ESP_ERROR_CHECK(xTaskCreate(output_task, "protocol_output", 3072, NULL, 5, NULL) == pdPASS ? ESP_OK : ESP_ERR_NO_MEM);
    ih_emit("@0 EVT boot fw=%s", esp_app_get_description()->version);
    ih_ota_init();
    ih_ble_init();
    /* Create executor before network ingress can call ih_submit. */
    ESP_ERROR_CHECK(xTaskCreate(executor_task, "commands", 6144, NULL, 4, &executor) == pdPASS ? ESP_OK : ESP_ERR_NO_MEM);
    ih_wifi_init();
    ih_stream stream = {0};
    unsigned char bytes[64];
    for (;;) {
        int n = usb_serial_jtag_read_bytes(bytes, sizeof(bytes), portMAX_DELAY);
        for (int i = 0; i < n; ++i) {
            ih_command c;
            if (!ih_feed(&stream, bytes[i], &c)) continue;
            ih_request request = {.command = c};
            ih_submit(&request);
            memset(&c, 0, sizeof(c));
            memset(&request, 0, sizeof(request));
        }
    }
}
