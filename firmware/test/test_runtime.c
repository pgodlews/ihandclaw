/* Hardware-free regression tests execute the actual main.c against deterministic
 * queues, clock, USB and BLE doubles. These are synthetic, not RF measurements. */
#include "runtime.h"
#include <setjmp.h>
#include <stdlib.h>
#include <string.h>
#include "../main/main.c"

struct test_queue { size_t size; unsigned capacity, count; unsigned char *data; };
static jmp_buf finish;
static int64_t now_us, stop_at;
static bool stop_injected, usb_host, usb_reader, fail_release;
static unsigned feeds, reports, aborts, usb_writes, notify_wakes;
static uint8_t held_mouse, held_keyboard, held_consumer;
static char replies[128][IH_OUTPUT_MAX];
static unsigned reply_count, awaiting_replies;
static ih_ble_status fake_status;
static ih_error status_error;

QueueHandle_t xQueueCreate(unsigned capacity, size_t size)
{
    struct test_queue *q = calloc(1, sizeof(*q)); assert(q);
    q->size = size; q->capacity = capacity; q->data = calloc(capacity, size); assert(q->data);
    return q;
}
BaseType_t xQueueSend(QueueHandle_t q, const void *item, TickType_t timeout)
{
    assert(timeout == 0); /* Every caller must remain nonblocking. */
    if (q->count == q->capacity) return 0;
    memcpy(q->data + q->count++ * q->size, item, q->size);
    return pdTRUE;
}
BaseType_t xQueueReceive(QueueHandle_t q, void *item, TickType_t timeout)
{
    if (!q->count) {
        if (timeout) { assert(timeout <= TASK_POLL_MS); now_us += timeout * 1000LL; longjmp(finish, 1); }
        return 0;
    }
    memcpy(item, q->data, q->size);
    memmove(q->data, q->data + q->size, --q->count * q->size);
    return pdTRUE;
}
BaseType_t xTaskCreate(void (*fn)(void *), const char *name, unsigned stack,
                      void *arg, unsigned priority, TaskHandle_t *handle)
{
    (void)fn; (void)name; (void)stack; (void)arg; (void)priority;
    if (handle) *handle = (void *)1;
    return pdPASS;
}
void xTaskNotifyGive(TaskHandle_t handle) { assert(handle); ++notify_wakes; }
static void inject_stop(void)
{
    if (stop_at && now_us >= stop_at && !stop_injected) {
        stop_injected = true;
        ih_request request = {.command = ih_parse("99 STOP", 7), .session = 1, .tcp = true};
        ++awaiting_replies;
        ih_submit(&request);
    }
}
uint32_t ulTaskNotifyTake(BaseType_t clear, TickType_t wait)
{
    assert(clear == pdTRUE && wait <= TASK_POLL_MS);
    if (!awaiting_replies && !commands->count && !stops->count) longjmp(finish, 1);
    now_us += wait * 1000LL;
    inject_stop();
    return 0;
}
int64_t esp_timer_get_time(void) { return now_us; }
esp_reset_reason_t esp_reset_reason(void) { return ESP_RST_TASK_WDT; }
esp_err_t esp_task_wdt_reconfigure(const esp_task_wdt_config_t *config) { (void)config; return ESP_OK; }
esp_err_t esp_task_wdt_init(const esp_task_wdt_config_t *config) { (void)config; return ESP_OK; }
esp_err_t esp_task_wdt_add(TaskHandle_t handle) { (void)handle; return ESP_OK; }
esp_err_t esp_task_wdt_reset(void) { ++feeds; return ESP_OK; }
esp_err_t nvs_flash_init(void) { return ESP_OK; }
void esp_log_set_vprintf(int (*fn)(const char *, va_list)) { (void)fn; }
esp_err_t usb_serial_jtag_driver_install(const usb_serial_jtag_driver_config_t *config) { (void)config; return ESP_OK; }
bool usb_serial_jtag_is_connected(void) { return usb_host; }
int usb_serial_jtag_write_bytes(const void *data, size_t n, uint32_t timeout)
{
    (void)data; assert(timeout == 0); ++usb_writes;
    return usb_reader ? (int)n : 0;
}
int usb_serial_jtag_read_bytes(void *data, size_t n, uint32_t timeout) { (void)data; (void)n; (void)timeout; return 0; }
static const esp_app_desc_t app_desc = {.version = "test", .project_name = "iphone_hand", .app_elf_sha256 = {0x12, 0x34, [31] = 0xff}};
static const esp_partition_t app_slot = {.label = "ota_0"};
const esp_app_desc_t *esp_app_get_description(void) { return &app_desc; }
int esp_app_get_elf_sha256(char *dst, size_t size) { return snprintf(dst, size, "test-elf"); }
const esp_partition_t *esp_ota_get_running_partition(void) { return &app_slot; }
void ih_ota_status(char *detail, size_t size) { snprintf(detail, size, " ota_pending=0 ota_last=none"); }
void ih_ota_init(void) {}
void ih_ota_poll(void) {}
bool ih_ota_pending(void) { return false; }
ih_error ih_ota_confirm(uint32_t session, bool up) { (void)session; (void)up; return IH_OK; }
ih_error ih_ota_execute(const ih_request *r, char *detail, size_t size)
{ (void)r; (void)detail; (void)size; return IH_OK; }
void ih_wifi_init(void) {}
uint32_t ih_wifi_session(void) { return 1; }
void ih_wifi_status(bool *up, char *ip, size_t size) { *up = false; snprintf(ip, size, "-"); }
ih_error ih_wifi_provision(const ih_command *command) { (void)command; return IH_OK; }
bool ih_wifi_output(uint32_t session, const char *line)
{
    if (session) {
        assert(reply_count < 128); strcpy(replies[reply_count++], line);
        if (line[0] == '@' && line[1] != '0') { assert(awaiting_replies); --awaiting_replies; }
    }
    return true;
}
void ih_ble_init(void) {}
ih_error ih_ble_get_status(ih_ble_status *status) { *status = fake_status; return status_error; }
void ih_ble_wake_send(void) {}
ih_error ih_ble_pair(void) { fake_status.pairing = true; return IH_OK; }
ih_error ih_ble_restart(const char *name, bool unpair) { (void)name; (void)unpair; return IH_OK; }
void ih_ble_abort(uint32_t gen) { assert(gen == fake_status.generation); ++aborts; fake_status.conn = false; }
ih_error ih_ble_send(uint8_t id, const uint8_t *data, size_t n, uint32_t gen,
                     unsigned timeout_ms, ih_error (*guard)(void))
{
    (void)n; assert(gen == fake_status.generation && timeout_ms <= IH_NOTIFY_TIMEOUT_MS);
    if (!guard && fail_release) { now_us += timeout_ms * 1000LL; return IH_TIMEOUT; }
    inject_stop();
    if (guard && guard() != IH_OK) return guard();
    ++reports;
    if (id == 1) held_keyboard = data[0] | data[2];
    if (id == 2) held_mouse = data[0];
    if (id == 3) held_consumer = data[0] | data[1];
    return IH_OK;
}
static void reset(void)
{
    QueueHandle_t queues[] = {commands, stops, output};
    for (unsigned i = 0; i < 3; ++i) if (queues[i]) { free(queues[i]->data); free(queues[i]); }
    commands = xQueueCreate(COMMAND_QUEUE_SIZE, sizeof(ih_request));
    stops = xQueueCreate(4, sizeof(ih_request)); output = xQueueCreate(64, sizeof(ih_output));
    executor = (void *)1;
    atomic_store(&cancel_epoch, 0); atomic_store(&out_drops, 0);
    now_us = stop_at = 0; stop_injected = false;
    fail_release = usb_host = usb_reader = false;
    feeds = reports = aborts = usb_writes = notify_wakes = reply_count = awaiting_replies = 0;
    buttons = held_mouse = held_keyboard = held_consumer = 0;
    step = 8; interval_ms = 15; button_generation = 1;
    status_error = IH_OK;
    fake_status = (ih_ble_status){.conn = true, .ready = true, .generation = 1};
    strcpy(fake_status.peer, "-");
}
static void submit(const char *line)
{
    ih_request r = {.command = ih_parse(line, strlen(line)), .session = 1, .tcp = true}; ++awaiting_replies; ih_submit(&r);
}
static void run_executor(void)
{
    if (!setjmp(finish)) executor_task(NULL);
}
static void run_output(void)
{
    if (!setjmp(finish)) output_task(NULL);
}
static void stop_mid_command(const char *line)
{
    reset(); submit(line); submit("2 MOVE 80 0");
    stop_at = 50000;
    /* Idle after STOP and queued cancellations ends this synthetic task. */
    if (!setjmp(finish)) {
        /* notify double needs an idle end after injected STOP. */
        executor_task(NULL);
    }
    assert(stop_injected && !held_mouse && !held_keyboard && !held_consumer);
    assert(!strcmp(replies[0], "@1 ERR CANCELLED"));
    assert(!strcmp(replies[1], "@99 OK"));
    assert(!strcmp(replies[2], "@2 ERR CANCELLED"));
    assert(now_us < 200000 && feeds > 0);
}
int main(void)
{
    reset();
    for (unsigned i = 0; i < 100; ++i) ih_emit("@0 EVT test");
    assert(output->count == 64 && atomic_load(&out_drops) == 36 && reply_count == 100);
    run_output(); /* charger: no host */
    assert(!usb_writes && atomic_load(&out_drops) == 100);
    reset(); usb_enqueue("I gap: ", 7); ih_emit("@0 EVT test");
    assert(output->count == 2 && output->data[output->size] == '\n');
    reset(); usb_host = true; /* enumerated host, no CDC reader */
    ih_emit("@0 EVT test"); run_output(); assert(usb_writes == 1 && atomic_load(&out_drops) == 1);
    submit("1 PING"); run_executor(); assert(!strcmp(replies[1], "@1 OK fw=test chip=" CONFIG_IDF_TARGET " elf=12340000000000000000000000000000000000000000000000000000000000ff slot=ota_0 ota_pending=0 ota_last=none"));
    reset(); submit("1 BTN 1"); submit("2 KREPORT 8 4"); run_executor();
    assert(held_mouse && held_keyboard);
    submit("3 STOP"); run_executor(); assert(!held_mouse && !held_keyboard && !held_consumer);
    reset(); submit("1 BTN 7"); submit("2 MOVE 2001 0"); submit("3 SCROLL -2147483648"); submit("4 STOP 1");
    run_executor(); assert(reports == 1 && held_mouse == 7 && atomic_load(&cancel_epoch) == 0);
    assert(!strcmp(replies[1], "@2 ERR ARG") && !strcmp(replies[2], "@3 ERR ARG") && !strcmp(replies[3], "@4 ERR ARG"));
    reset(); submit("1 STOP"); fake_status.conn = false; run_executor(); assert(!strcmp(replies[0], "@1 OK"));
    reset(); submit("1 MOVE 2000 0"); step = 1; interval_ms = 100; run_executor();
    assert(!strcmp(replies[0], "@1 ERR TIMEOUT") && now_us == 10000000 && reports <= 104);
    reset(); submit("1 KEY 8 4 65535"); run_executor(); assert(!held_keyboard && now_us == 10000000);
    reset(); fail_release = true; submit("1 STOP"); run_executor();
    assert(aborts == 1 && now_us == 2000000 && !strcmp(replies[0], "@1 ERR TIMEOUT"));
    reset(); submit("1 STATUS"); run_executor();
    assert(strstr(replies[0], "out_drops=0 uptime_ms=0 reset=task_wdt") && strstr(replies[0], "pairing=0"));
    reset(); status_error = IH_INTERNAL; submit("1 STATUS"); run_executor();
    assert(strstr(replies[0], "@1 ERR INTERNAL ota_pending=0 ota_last=none"));
    reset(); for (unsigned i = 0; i < COMMAND_QUEUE_SIZE; ++i) submit("1 MOVE 1 0");
    submit("2 MOVE 1 0"); assert(!strcmp(replies[0], "@2 ERR BUSY"));
    submit("3 STOP"); assert(stops->count == 1 && atomic_load(&cancel_epoch) == 1);
    run_executor(); assert(!strcmp(replies[1], "@3 OK") && held_mouse == 0);
    stop_mid_command("1 MOVE 1000 0");
    stop_mid_command("1 SCROLL 2000 0");
    stop_mid_command("1 CLICK 1 1000");
    stop_mid_command("1 KEY 8 4 1000");
    stop_mid_command("1 CONSUMER 233 1000");
    puts("PASS: runtime output isolation, overload/STOP, deadlines, release and STATUS");
    reset(); free(commands->data); free(commands); free(stops->data); free(stops); free(output->data); free(output);
    return 0;
}
