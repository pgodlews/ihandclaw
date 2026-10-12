/* Read: API semantics are cited in README.md against the installed IDF tree. */
#include "ota.h"
#include "ota_wire.h"
#include <inttypes.h>
#include <stdio.h>
#include <string.h>
#include "esp_ota_ops.h"
#include "esp_app_desc.h"
#include "esp_timer.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "nvs.h"
#include "psa/crypto.h"

#define UPLOAD_IDLE_US (30000000LL)
#define CONFIRM_US (120000000LL)
#define REBOOT_US (1000000LL)
static struct {
    bool active, pending, binary, ps_changed;
    wifi_ps_type_t previous_ps;
    uint32_t session, expected, received;
    uint8_t expected_hash[32];
    esp_ota_handle_t handle;
    const esp_partition_t *partition;
    psa_hash_operation_t hash;
    int64_t last_data, confirm_at, reboot_at;
} ota;

/* One atomic NVS blob per checkpoint, never per DATA. No credentials or addresses. */
static struct {
    uint32_t magic;
    char event[24], reason[24], from[17], to[17];
    int32_t begin, end, boot, trial, confirm, reset;
} journal;
static esp_err_t journal_error;
static void record(const char *event, const char *reason)
{
    snprintf(journal.event, sizeof(journal.event), "%s", event);
    if (reason) snprintf(journal.reason, sizeof(journal.reason), "%s", reason);
    nvs_handle_t h;
    journal_error = nvs_open("ota", NVS_READWRITE, &h);
    if (journal_error == ESP_OK) {
        journal_error = nvs_set_blob(h, "last", &journal, sizeof(journal));
        if (journal_error == ESP_OK) journal_error = nvs_commit(h);
        nvs_close(h);
    }
}
void ih_ota_status(char *detail, size_t size)
{
    snprintf(detail, size,
        " ota_binary=1 ota_pending=%u ota_last=%s,%s>%s,b:%" PRId32 ",e:%" PRId32
        ",s:%" PRId32 ",t:%" PRId32 ",c:%" PRId32 ",r:%s,x:%" PRId32 " ota_nv=%d",
        ota.pending, journal.event, journal.from, journal.to, journal.begin, journal.end,
        journal.boot, journal.trial, journal.confirm, journal.reason, journal.reset, (int)journal_error);
}
static void restore_ps(void)
{
    if (ota.ps_changed) {
        esp_err_t err = esp_wifi_set_ps(ota.previous_ps);
        if (err != ESP_OK) record("ps_restore_failed", "wifi_ps");
        ota.ps_changed = false;
    }
}
static void abort_upload(void)
{
    if (ota.active) {
        (void)esp_ota_abort(ota.handle);
        (void)psa_hash_abort(&ota.hash);
    }
    ota.active = false;
    ota.session = 0;
    restore_ps();
}
void ih_ota_init(void)
{
    esp_ota_img_states_t state = ESP_OTA_IMG_UNDEFINED;
    const esp_partition_t *running = esp_ota_get_running_partition();
    ota.pending = running && esp_ota_get_state_partition(running, &state) == ESP_OK &&
                  state == ESP_OTA_IMG_PENDING_VERIFY;
    memset(&journal, 0, sizeof(journal));
    nvs_handle_t h;
    if (nvs_open("ota", NVS_READONLY, &h) == ESP_OK) {
        size_t n = sizeof(journal);
        if (nvs_get_blob(h, "last", &journal, &n) != ESP_OK || n != sizeof(journal) ||
            journal.magic != 0x49484f31) memset(&journal, 0, sizeof(journal));
        nvs_close(h);
    }
    journal.event[sizeof(journal.event)-1] = 0;
    journal.reason[sizeof(journal.reason)-1] = 0;
    journal.from[sizeof(journal.from)-1] = 0;
    journal.to[sizeof(journal.to)-1] = 0;
    if (!journal.magic) {
        journal.magic = 0x49484f31;
        strcpy(journal.event, "none"); strcpy(journal.reason, "none");
        strcpy(journal.from, running ? running->label : "-"); strcpy(journal.to, "-");
        journal.begin = journal.end = journal.boot = journal.confirm = -1;
    }
    journal.reset = (int32_t)esp_reset_reason();
    if (ota.pending) {
        if (strcmp(journal.to, running->label)) {
            /* A legacy uploader could not record BEGIN/END; identify this trial anyway. */
            const esp_partition_t *previous = esp_ota_get_next_update_partition(NULL);
            snprintf(journal.from, sizeof(journal.from), "%s", previous ? previous->label : "-");
            snprintf(journal.to, sizeof(journal.to), "%s", running->label);
            journal.begin = journal.end = journal.boot = journal.confirm = -1;
        }
        journal.trial = 1;
        record("trial", "await_status");
    } else if (running && !strcmp(running->label, journal.to) && state == ESP_OTA_IMG_VALID &&
               (!strcmp(journal.event, "trial") || !strcmp(journal.event, "confirm_failed"))) {
        /* Recover a power cut after otadata became VALID but before journal commit. */
        journal.confirm = ESP_OK;
        record("confirmed", "otadata_valid");
    } else if (running && strcmp(running->label, journal.to) &&
               (!strcmp(journal.event, "set_boot") || !strcmp(journal.event, "trial") ||
                !strcmp(journal.event, "trial_timeout") || !strcmp(journal.event, "confirm_failed"))) {
        const esp_partition_t *other = esp_ota_get_next_update_partition(NULL);
        esp_ota_img_states_t other_state;
        bool aborted = other && esp_ota_get_state_partition(other, &other_state) == ESP_OK &&
                       other_state == ESP_OTA_IMG_ABORTED;
        record(aborted ? "rollback" : "boot_fallback",
               !strcmp(journal.event, "trial_timeout") ? "timeout" :
               aborted ? "unconfirmed_reset" : "boot_not_selected");
    }
    ota.confirm_at = esp_timer_get_time() + CONFIRM_US;
}
bool ih_ota_pending(void) { return ota.pending; }
void ih_ota_poll(void)
{
    int64_t now = esp_timer_get_time();
    if (ota.active && (ota.session != ih_wifi_session() || now - ota.last_data >= UPLOAD_IDLE_US))
    {
        record("aborted", ota.session != ih_wifi_session() ? "session_lost" : "upload_idle");
        abort_upload();
    }
    /* No mark-invalid call is needed: an unconfirmed reboot makes the
     * rollback-enabled bootloader mark PENDING_VERIFY as ABORTED. */
    if (ota.pending && now >= ota.confirm_at) {
        record("trial_timeout", "timeout");
        esp_restart();
    } else if (ota.reboot_at && now >= ota.reboot_at) esp_restart();
}
ih_error ih_ota_confirm(uint32_t session, bool wifi_up)
{
    if (!session || session != ih_wifi_session() || !wifi_up) return IH_NOCONN;
    if (ota.pending) {
        if (esp_timer_get_time() >= ota.confirm_at) return IH_TIMEOUT;
        journal.confirm = esp_ota_mark_app_valid_cancel_rollback();
        if (journal.confirm != ESP_OK) { record("confirm_failed", "mark_valid"); return IH_INTERNAL; }
        ota.pending = false;
        record("confirmed", "none");
    }
    return IH_OK;
}
static bool valid_version(const char *version, size_t size)
{
    const char *end = memchr(version, 0, size);
    if (!end || end == version) return false;
    for (const char *p = version; p < end; ++p)
        if (!((*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') ||
              (*p >= '0' && *p <= '9') || *p == '.' || *p == '_' || *p == '-')) return false;
    return true;
}
ih_error ih_ota_execute(const ih_request *r, char *detail, size_t size)
{
    const ih_command *c = &r->command;
    ih_ota_poll();
    if (!r->tcp || !r->session || r->session != ih_wifi_session()) return IH_ARG;
    if (ota.pending || ota.reboot_at) return IH_BUSY;
    if (c->ota_op == IH_OTA_ABORT) {
        if (ota.active) record("aborted", "requested");
        abort_upload();
        return IH_OK;
    }
    if (c->ota_op == IH_OTA_BEGIN) {
        if (ota.active) return IH_BUSY;
        ota.partition = esp_ota_get_next_update_partition(NULL);
        if (!ota.partition || ota.partition == esp_ota_get_running_partition()) return IH_INTERNAL;
        if (!c->ota_size || c->ota_size > ota.partition->size) return IH_SIZE;
        memset(&journal, 0, sizeof(journal)); journal.magic = 0x49484f31;
        snprintf(journal.from, sizeof(journal.from), "%s", esp_ota_get_running_partition()->label);
        snprintf(journal.to, sizeof(journal.to), "%s", ota.partition->label);
        journal.begin = journal.end = journal.boot = journal.confirm = -1;
        ota.hash = psa_hash_operation_init();
        if (psa_crypto_init() != PSA_SUCCESS ||
            psa_hash_setup(&ota.hash, PSA_ALG_SHA_256) != PSA_SUCCESS) {
            (void)psa_hash_abort(&ota.hash);
            record("begin_failed", "sha_setup");
            return IH_INTERNAL;
        }
        /* Sequential erasure bounds each binary write to 8 KiB. */
        journal.begin = esp_ota_begin(ota.partition, OTA_WITH_SEQUENTIAL_WRITES, &ota.handle);
        record(journal.begin == ESP_OK ? "begin" : "begin_failed", "none");
        if (journal.begin != ESP_OK) {
            (void)psa_hash_abort(&ota.hash);
            return IH_INTERNAL;
        }
        ota.active = true; ota.session = r->session; ota.binary = c->ota_binary;
        if (esp_wifi_get_ps(&ota.previous_ps) == ESP_OK &&
            esp_wifi_set_ps(WIFI_PS_NONE) == ESP_OK) ota.ps_changed = true;
        ota.expected = c->ota_size; ota.received = 0;
        memcpy(ota.expected_hash, c->ota_hash, 32);
        ota.last_data = esp_timer_get_time();
        snprintf(detail, size, " chunk=%u window=%u mode=%s size=%" PRIu32 " slot=%s",
                 ota.binary ? IH_OTA_BINARY_MAX : IH_OTA_CHUNK_MAX, ota.binary ? IH_OTA_WINDOW : 1,
                 ota.binary ? "bin" : "base64", ota.expected, ota.partition->label);
        return IH_OK;
    }
    if (!ota.active || ota.session != r->session) return IH_ARG;
    if (c->ota_op == IH_OTA_DATA) {
        size_t length = r->frame ? r->frame->length : c->ota_length;
        const uint8_t *data = r->frame ? r->frame->data : c->ota_data;
        if (ota.binary != (r->frame != NULL) || !length ||
            length > (r->frame ? IH_OTA_BINARY_MAX : IH_OTA_CHUNK_MAX) ||
            length > ota.expected - ota.received || (r->frame && r->frame->offset != ota.received)) {
            record("data_failed", "size_offset"); abort_upload(); return IH_SIZE;
        }
        esp_err_t err = esp_ota_write(ota.handle, data, length);
        if (err != ESP_OK) {
            record("data_failed", "write");
            abort_upload();
            return err == ESP_ERR_OTA_VALIDATE_FAILED ? IH_IMAGE : IH_INTERNAL;
        }
        if (psa_hash_update(&ota.hash, data, length) != PSA_SUCCESS) {
            record("data_failed", "sha_update"); abort_upload(); return IH_INTERNAL;
        }
        ota.received += length;
        ota.last_data = esp_timer_get_time();
        snprintf(detail, size, " received=%" PRIu32, ota.received);
        return IH_OK;
    }
    if (c->ota_op != IH_OTA_END) return IH_ARG;
    if (ota.received != ota.expected) { record("end_failed", "size"); abort_upload(); return IH_SIZE; }
    uint8_t actual[32]; size_t length = 0;
    if (psa_hash_finish(&ota.hash, actual, sizeof(actual), &length) != PSA_SUCCESS || length != 32) {
        record("end_failed", "sha_finish"); abort_upload(); return IH_INTERNAL;
    }
    if (memcmp(actual, ota.expected_hash, 32)) { record("end_failed", "hash"); abort_upload(); return IH_HASH; }
    /* end checks the complete image, chip/revision, checksum and image SHA.
     * It frees the handle on both success and failure; never abort it after end. */
    esp_err_t err = esp_ota_end(ota.handle);
    ota.active = false; ota.session = 0;
    restore_ps();
    journal.end = err;
    record(err == ESP_OK ? "end" : "end_failed", err == ESP_OK ? "none" : "image");
    if (err != ESP_OK) return IH_IMAGE;
    esp_app_desc_t desc;
    if (esp_ota_get_partition_description(ota.partition, &desc) != ESP_OK ||
        memcmp(desc.project_name, "iphone_hand\0", 12) || !valid_version(desc.version, sizeof(desc.version)))
    { record("end_failed", "descriptor"); return IH_IMAGE; }
    journal.boot = esp_ota_set_boot_partition(ota.partition);
    record(journal.boot == ESP_OK ? "set_boot" : "set_boot_failed", "none");
    if (journal.boot != ESP_OK) return IH_INTERNAL;
    snprintf(detail, size, " fw=%s reboot=1 slot=%s", desc.version, ota.partition->label);
    /* Leave the nonblocking TCP writer time to send END's acknowledgement.
     * If it is lost, the host can still verify the image after reboot. */
    ota.reboot_at = esp_timer_get_time() + REBOOT_US;
    return IH_OK;
}
