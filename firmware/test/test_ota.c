/* Synthetic IDF/PSA failures exercise the real OTA executor, without hardware. */
#include "runtime.h"
#include <stdio.h>
#include <string.h>
#include "../main/ota.c"
static const esp_partition_t running = {.size = IH_OTA_SLOT_SIZE, .label = "ota_0"};
static const esp_partition_t target = {.size = IH_OTA_SLOT_SIZE, .label = "ota_1"};
static const esp_partition_t *current = &running;
static unsigned char stored[256];
static size_t stored_size;
static esp_err_t nvs_error;
static wifi_ps_type_t ps;
static unsigned ps_restores;
static int64_t now;
static uint32_t session;
static unsigned aborts, ends, boots, restarts, marks;
static esp_err_t write_error, end_error, mark_error, boot_error, begin_error;
static esp_ota_img_states_t state;
static char project[32], version[32];
int64_t esp_timer_get_time(void) { return now; }
uint32_t ih_wifi_session(void) { return session; }
void esp_restart(void) { ++restarts; }
const esp_partition_t *esp_ota_get_running_partition(void) { return current; }
const esp_partition_t *esp_ota_get_next_update_partition(const esp_partition_t *p) { assert(!p); return current == &running ? &target : &running; }
esp_err_t esp_ota_get_state_partition(const esp_partition_t *p, esp_ota_img_states_t *out)
{ assert(p); *out = p == current ? state : ESP_OTA_IMG_ABORTED; return ESP_OK; }
esp_err_t esp_ota_begin(const esp_partition_t *p, size_t size, esp_ota_handle_t *handle)
{ assert(p == &target && size == OTA_WITH_SEQUENTIAL_WRITES); *handle = 1; return begin_error; }
esp_err_t esp_ota_write(esp_ota_handle_t handle, const void *data, size_t size)
{ assert(handle == 1 && data && size <= IH_OTA_BINARY_MAX); return write_error; }
esp_err_t esp_ota_abort(esp_ota_handle_t handle) { assert(handle == 1); ++aborts; return ESP_OK; }
esp_err_t esp_ota_end(esp_ota_handle_t handle) { assert(handle == 1); ++ends; return end_error; }
esp_err_t esp_ota_get_partition_description(const esp_partition_t *p, esp_app_desc_t *desc)
{ assert(p == &target); memcpy(desc->version, version, 32); memcpy(desc->project_name, project, 32); return ESP_OK; }
esp_err_t esp_ota_set_boot_partition(const esp_partition_t *p) { assert(p == &target); ++boots; return boot_error; }
esp_err_t esp_ota_mark_app_valid_cancel_rollback(void) { ++marks; return mark_error; }
esp_reset_reason_t esp_reset_reason(void) { return ESP_RST_SW; }
esp_err_t esp_wifi_get_ps(wifi_ps_type_t *out) { *out = ps; return ESP_OK; }
esp_err_t esp_wifi_set_ps(wifi_ps_type_t next) { ps = next; if (next != WIFI_PS_NONE) ++ps_restores; return ESP_OK; }
esp_err_t nvs_open(const char *name, int mode, nvs_handle_t *h)
{ assert(!strcmp(name, "ota")); (void)mode; *h = 1; return nvs_error; }
esp_err_t nvs_get_blob(nvs_handle_t h, const char *key, void *value, size_t *size)
{ assert(h == 1 && !strcmp(key, "last")); if (!stored_size) return ESP_ERR_NVS_NOT_FOUND;
  assert(*size >= stored_size); memcpy(value, stored, stored_size); *size = stored_size; return ESP_OK; }
esp_err_t nvs_set_blob(nvs_handle_t h, const char *key, const void *value, size_t size)
{ assert(h == 1 && !strcmp(key, "last") && size <= sizeof(stored)); memcpy(stored, value, size); stored_size = size; return ESP_OK; }
esp_err_t nvs_commit(nvs_handle_t h) { assert(h == 1); return ESP_OK; }
void nvs_close(nvs_handle_t h) { assert(h == 1); }
int psa_crypto_init(void) { return PSA_SUCCESS; }
int psa_hash_setup(psa_hash_operation_t *op, int algorithm)
{ assert(algorithm == PSA_ALG_SHA_256); op->active = true; op->sum = 0; return PSA_SUCCESS; }
int psa_hash_update(psa_hash_operation_t *op, const uint8_t *data, size_t size)
{ assert(op->active); for (size_t i = 0; i < size; ++i) op->sum ^= data[i]; return PSA_SUCCESS; }
int psa_hash_finish(psa_hash_operation_t *op, uint8_t *hash, size_t size, size_t *length)
{ assert(op->active && size == 32); memset(hash, op->sum, size); *length = size; op->active = false; return PSA_SUCCESS; }
int psa_hash_abort(psa_hash_operation_t *op) { op->active = false; return PSA_SUCCESS; }
static void reset(void)
{
    memset(&ota, 0, sizeof(ota)); stored_size = 0; nvs_error = 0; current = &running;
    ps = WIFI_PS_MIN_MODEM; ps_restores = 0; now = 0; session = 7;
    aborts = ends = boots = restarts = marks = 0;
    write_error = end_error = mark_error = boot_error = begin_error = 0;
    state = ESP_OTA_IMG_VALID; strcpy(project, "iphone_hand"); strcpy(version, "test"); ih_ota_init();
}
static ih_error execute(ih_command c, uint32_t origin, bool tcp)
{
    ih_request r = {.command = c, .session = origin, .tcp = tcp}; char detail[200] = "";
    return ih_ota_execute(&r, detail, sizeof(detail));
}
static ih_command begin_command(unsigned size)
{
    ih_command c = {.kind = IH_OTA, .ota_op = IH_OTA_BEGIN, .ota_size = size};
    memset(c.ota_hash, 0x42, 32); return c;
}
static void begin(void) { assert(execute(begin_command(1), 7, true) == IH_OK); }
static ih_error data(void)
{
    ih_command c = {.kind = IH_OTA, .ota_op = IH_OTA_DATA, .ota_length = 1, .ota_data = {0x42}};
    return execute(c, 7, true);
}
static ih_error end(void) { return execute((ih_command){.kind = IH_OTA, .ota_op = IH_OTA_END}, 7, true); }
int main(void)
{
    reset(); assert(execute(begin_command(1), 7, false) == IH_ARG);
    assert(execute(begin_command(1), 6, true) == IH_ARG); assert(!ota.active);
    assert(execute(begin_command(IH_OTA_SLOT_SIZE+1), 7, true) == IH_SIZE);
    begin(); assert(execute(begin_command(1), 7, true) == IH_BUSY);
    assert(data() == IH_OK && !boots); assert(end() == IH_OK && ends == 1 && boots == 1);
    assert(execute(begin_command(1), 7, true) == IH_BUSY);
    now = REBOOT_US - 1; ih_ota_poll(); assert(!restarts);
    ++now; ih_ota_poll(); assert(restarts == 1);
    reset(); begin(); assert(end() == IH_SIZE && aborts == 1 && !boots && !ends);
    reset(); begin(); assert(data() == IH_OK); assert(data() == IH_SIZE && aborts == 1 && !boots);
    reset(); begin(); ota.expected_hash[0] ^= 1; assert(data() == IH_OK);
    assert(end() == IH_HASH && aborts == 1 && !ends && !boots);
    assert(execute((ih_command){.kind = IH_OTA, .ota_op = IH_OTA_ABORT}, 7, true) == IH_OK);
    assert(!strcmp(journal.event, "end_failed") && !strcmp(journal.reason, "hash"));
    reset(); begin(); session = 0; ih_ota_poll(); assert(aborts == 1 && !boots);
    session = 8; assert(execute(begin_command(1), 7, true) == IH_ARG);
    assert(execute(begin_command(1), 8, true) == IH_OK);
    reset(); begin(); now = UPLOAD_IDLE_US; ih_ota_poll(); assert(aborts == 1 && !boots);
    reset(); begin(); write_error = ESP_ERR_OTA_VALIDATE_FAILED;
    assert(data() == IH_IMAGE && aborts == 1 && !boots);
    reset(); begin(); assert(data() == IH_OK); end_error = 42;
    assert(end() == IH_IMAGE && ends == 1 && !aborts && !boots);
    reset(); begin(); assert(data() == IH_OK); strcpy(project, "another");
    assert(end() == IH_IMAGE && !boots);
    reset(); begin(); assert(data() == IH_OK); memset(version, 'x', sizeof(version));
    assert(end() == IH_IMAGE && !boots);
    reset(); begin(); assert(data() == IH_OK); boot_error = 42;
    assert(end() == IH_INTERNAL && !ota.reboot_at);
    reset(); begin_error = 42; assert(execute(begin_command(1), 7, true) == IH_INTERNAL && !ota.active);
    reset(); begin(); assert(execute((ih_command){.kind = IH_OTA, .ota_op = IH_OTA_ABORT}, 7, true) == IH_OK);
    assert(aborts == 1 && !ota.active);
    reset(); state = ESP_OTA_IMG_PENDING_VERIFY; ih_ota_init(); assert(ih_ota_pending());
    assert(execute(begin_command(1), 7, true) == IH_BUSY);
    assert(!strcmp(journal.to, "ota_0") && !strcmp(journal.from, "ota_1"));
    assert(ih_ota_confirm(0, true) == IH_NOCONN && !marks);
    assert(ih_ota_confirm(7, false) == IH_NOCONN && !marks);
    assert(ih_ota_confirm(6, true) == IH_NOCONN && !marks);
    mark_error = 42; assert(ih_ota_confirm(7, true) == IH_INTERNAL && ih_ota_pending());
    mark_error = 0; assert(ih_ota_confirm(7, true) == IH_OK && !ih_ota_pending());
    now = CONFIRM_US; ih_ota_poll(); assert(!restarts);
    reset(); state = ESP_OTA_IMG_PENDING_VERIFY; ih_ota_init();
    now = CONFIRM_US; assert(ih_ota_confirm(7, true) == IH_TIMEOUT && !marks);
    ih_ota_poll(); assert(restarts == 1 && !marks);
    /* Persist checkpoints across trial and fallback boots, including identical version. */
    reset(); begin(); assert(ps == WIFI_PS_NONE); assert(data() == IH_OK); assert(end() == IH_OK);
    assert(ps == WIFI_PS_MIN_MODEM && ps_restores == 1);
    assert(!strcmp(journal.event, "set_boot") && journal.begin == 0 && journal.end == 0 && journal.boot == 0);
    memset(&ota, 0, sizeof(ota)); current = &target; state = ESP_OTA_IMG_PENDING_VERIFY; ih_ota_init();
    assert(!strcmp(journal.event, "trial") && journal.trial == 1);
    assert(ih_ota_confirm(7, true) == IH_OK); assert(!strcmp(journal.event, "confirmed"));
    memset(&ota, 0, sizeof(ota)); state = ESP_OTA_IMG_VALID; ih_ota_init();
    assert(!strcmp(journal.event, "confirmed") && journal.confirm == 0);
    reset(); begin(); assert(data() == IH_OK); assert(end() == IH_OK);
    memset(&ota, 0, sizeof(ota)); current = &target; state = ESP_OTA_IMG_PENDING_VERIFY; ih_ota_init();
    now = CONFIRM_US; ih_ota_poll(); assert(!strcmp(journal.event, "trial_timeout"));
    memset(&ota, 0, sizeof(ota)); current = &running; state = ESP_OTA_IMG_VALID; ih_ota_init();
    assert(!strcmp(journal.event, "rollback") && !strcmp(journal.reason, "timeout"));
    reset(); begin(); assert(data() == IH_OK); assert(end() == IH_OK);
    memset(&ota, 0, sizeof(ota)); current = &target; state = ESP_OTA_IMG_PENDING_VERIFY; ih_ota_init();
    memset(&ota, 0, sizeof(ota)); current = &running; state = ESP_OTA_IMG_VALID; ih_ota_init();
    assert(!strcmp(journal.event, "rollback") && !strcmp(journal.reason, "unconfirmed_reset"));
    reset(); current = &target; state = ESP_OTA_IMG_PENDING_VERIFY; ih_ota_init();
    /* VALID metadata wins if journal's confirm checkpoint was interrupted. */
    memset(&ota, 0, sizeof(ota)); state = ESP_OTA_IMG_VALID; ih_ota_init();
    assert(!strcmp(journal.event, "confirmed") && !strcmp(journal.reason, "otadata_valid"));
    assert(!marks && !ih_ota_pending());
    reset(); begin(); session = 0; ih_ota_poll(); assert(ps == WIFI_PS_MIN_MODEM);
    assert(!strcmp(journal.reason, "session_lost"));
    reset(); nvs_error = 42; begin(); char detail[512]; ih_ota_status(detail, sizeof(detail));
    assert(strstr(detail, "ota_nv=42"));
    reset(); ih_command binary_begin = begin_command(IH_OTA_BINARY_MAX); binary_begin.ota_binary = true;
    memset(binary_begin.ota_hash, 0, 32); assert(execute(binary_begin, 7, true) == IH_OK);
    ih_ota_frame frame = {.length = IH_OTA_BINARY_MAX};
    ih_request r = {.tcp = true, .session = 7, .frame = &frame,
                    .command = {.kind = IH_OTA, .ota_op = IH_OTA_DATA}};
    assert(ih_ota_execute(&r, detail, sizeof(detail)) == IH_OK);
    assert(ota.received == IH_OTA_BINARY_MAX && end() == IH_OK);
    puts("OTA executor/rollback checkpoint tests passed (IDF/PSA doubles)");
}
