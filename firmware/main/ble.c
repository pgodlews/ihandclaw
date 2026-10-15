#include "ble.h"
#include "report_map.h"
#include <stdio.h>
#include <string.h>
#ifdef IH_BLE_NATIVE_TEST
#include "../test/ble_runtime.h"
#else
#include "esp_hidd.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "nvs.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "host/ble_hs.h"
#include "host/ble_gap.h"
#include "host/ble_esp_gap.h"
#include "host/ble_gatt.h"
#include "host/ble_store.h"
#include "host/util/util.h"
#include "services/gap/ble_svc_gap.h"
#include "services/dis/ble_svc_dis.h"
#include "services/hid/ble_svc_hid.h"
#include "store/config/ble_store_config.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/event_groups.h"
#endif

/* IDF's example declares this too: the bundled public header omits init.
 * Implementation: host/store/config/src/ble_store_config.c. */
void ble_store_config_init(void);
static esp_hidd_dev_t *hid;
static char device_name[IH_NAME_MAX + 1] = "iPhone Hand";
static uint16_t conn_handle = BLE_HS_CONN_HANDLE_NONE;
static uint16_t input_handles[4], boot_keyboard, boot_mouse, protocol_handle;
static bool protocol_report;
static bool subscribed[4], boot_k_sub, boot_m_sub, encrypted, ready, synced, suspended, restarting;
static uint32_t generation;
static char peer[18] = "-";
static uint8_t own_addr_type;
static SemaphoreHandle_t done;
static EventGroupHandle_t state_events;
#define DISCONNECTED BIT0
static void (*idf_register_cb)(struct ble_gatt_register_ctxt *, void *);
static struct ble_npl_event operation_event;
static struct ble_npl_event advertise_event;
static struct ble_npl_callout advertise_retry, send_retry, pairing_expiry;
static struct ble_npl_event send_wake_event;
static bool sending, attempting_send, peer_known, pairing_initialized;
static int64_t pair_deadline;
static void send_attempt(struct ble_npl_event *ev);
static int gap_event(struct ble_gap_event *event, void *arg);

typedef enum { OP_STATUS, OP_SEND, OP_DISCONNECT, OP_NAME, OP_UNPAIR, OP_RESUME, OP_ABORT, OP_PAIR } operation;
static struct {
    operation op;
    ih_error result;
    ih_ble_status status;
    uint8_t report_id, data[8];
    size_t length;
    uint32_t generation;
    const char *name;
    int64_t send_deadline;
    ih_error (*guard)(void);
} request; /* Single executor owns this until the host signals done. */

static ih_error rc_error(int rc)
{
    if (!rc) return IH_OK;
    if (rc == BLE_HS_ENOTCONN) return IH_NOCONN;
    if (rc == BLE_HS_EBUSY || rc == BLE_HS_ENOMEM || rc == BLE_HS_EAGAIN) return IH_BUSY;
    return IH_INTERNAL;
}
static uint8_t local_byte(uint16_t handle, uint8_t fallback)
{
    struct os_mbuf *om = NULL;
    uint8_t value = fallback;
    if (handle && !ble_att_svr_read_local(handle, &om)) {
        if (ble_hs_mbuf_to_flat(om, &value, 1, NULL)) value = fallback;
        os_mbuf_free_chain(om);
    }
    return value;
}
/* Cache on the host task: reading the characteristic itself allocates an
 * mbuf, so doing that during pool exhaustion could falsely select boot mode. */
static bool report_mode(void) { return protocol_report; }
static void update_ready(void)
{
    /* HID Control Point "suspend" is not a reason to stop: input reports are what wake a
     * suspended host (iOS suspends when idle; gating on it gave NOCONN, 2026-10-10). */
    bool now = encrypted && report_mode() && subscribed[1] && subscribed[2] && subscribed[3];
    if (now && !ready) ih_emit("@0 EVT ready");
    ready = now;
}
static bool pairing_open(void)
{
    return esp_timer_get_time() < pair_deadline;
}
static bool bonded_peer(const ble_addr_t *address)
{
    ble_addr_t identity = *address;
    identity.type &= 1; /* Controller identity types 2/3 map to public/random. */
    if (identity.type == BLE_ADDR_RANDOM) {
        /* Public IDF API resolves an RPA against stored IRKs. This keeps the
         * bonded iPhone reconnecting even if CONNECT carries its on-air RPA. */
        uint8_t resolved[6], type;
        if (ble_gap_rpa_resolve(identity.val, resolved, &type)) {
            memcpy(identity.val, resolved, sizeof(resolved));
            identity.type = type & 1;
        }
    }
    ble_addr_t peers[CONFIG_BT_NIMBLE_MAX_BONDS];
    int count = 0;
    if (ble_store_util_bonded_peers(peers, &count, CONFIG_BT_NIMBLE_MAX_BONDS)) return false;
    for (int i = 0; i < count; ++i)
        if ((peers[i].type & 1) == identity.type && !memcmp(peers[i].val, identity.val, 6)) return true;
    return false;
}
static int store_status(struct ble_store_status_event *event, void *arg)
{
    (void)event;
    (void)arg;
    /* Never let an unsolicited pairing or CCCD overflow evict a trusted bond.
     * A full store requires explicit UNPAIR instead of round-robin deletion. */
    return BLE_HS_ENOMEM;
}
static void close_pairing(void)
{
    pair_deadline = 0;
    ble_npl_callout_stop(&pairing_expiry);
}
static void open_pairing(void)
{
    pair_deadline = esp_timer_get_time() + IH_PAIR_WINDOW_MS * 1000LL;
    ble_npl_callout_reset(&pairing_expiry, ble_npl_time_ms_to_ticks32(IH_PAIR_WINDOW_MS));
}
static void pairing_expired(struct ble_npl_event *ev)
{
    (void)ev;
    close_pairing();
    /* A pairing started near the deadline cannot finish outside the window. */
    if (conn_handle != BLE_HS_CONN_HANDLE_NONE && !peer_known) {
        struct ble_gap_conn_desc desc;
        if (!ble_gap_conn_find(conn_handle, &desc)) ble_store_util_delete_peer(&desc.peer_id_addr);
        ble_gap_terminate(conn_handle, BLE_ERR_REM_USER_CONN_TERM);
    }
    if (ble_gap_adv_active()) ble_gap_adv_stop();
    ble_npl_eventq_put(nimble_port_get_dflt_eventq(), &advertise_event);
    ih_emit("@0 EVT pairing open=0");
}
static void advertise(struct ble_npl_event *ev)
{
    (void)ev;
    if (!synced || restarting || conn_handle != BLE_HS_CONN_HANDLE_NONE || ble_gap_adv_active()) return;
    ble_addr_t peers[CONFIG_BT_NIMBLE_MAX_BONDS];
    int count = 0;
    int bonds_rc = ble_store_util_bonded_peers(peers, &count, CONFIG_BT_NIMBLE_MAX_BONDS);
    if (bonds_rc) {
        ble_npl_callout_reset(&advertise_retry, ble_npl_time_ms_to_ticks32(1000));
        return; /* Fail closed if the bond store cannot be read. */
    }
    if (!pairing_open() && !count) return;
    /* Closed window: undirected discovery preserves iOS reconnect/privacy;
     * CONNECT rejects unbonded identities before security initiation. */
    static const ble_uuid16_t uuids[] = {BLE_UUID16_INIT(0x1812), BLE_UUID16_INIT(0x180f)};
    struct ble_hs_adv_fields fields = {0}, scan = {0};
    fields.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP;
    fields.appearance = 0x03c0;
    fields.appearance_is_present = 1;
    fields.uuids16 = uuids;
    fields.num_uuids16 = 2;
    fields.uuids16_is_complete = 1;
    /* Legacy scan response has 29 name bytes; truncate only at a UTF-8 boundary.
     * The complete name (up to 248 bytes) remains in the GAP characteristic. */
    size_t n = strlen(device_name), short_n = n > 29 ? 29 : n;
    while (short_n && ((unsigned char)device_name[short_n] & 0xc0) == 0x80) --short_n;
    scan.name = (const uint8_t *)device_name;
    scan.name_len = (uint8_t)short_n;
    scan.name_is_complete = short_n == n;
    struct ble_gap_adv_params params = {0};
    params.conn_mode = BLE_GAP_CONN_MODE_UND;
    params.disc_mode = BLE_GAP_DISC_MODE_GEN;
    params.itvl_min = BLE_GAP_ADV_ITVL_MS(30);
    params.itvl_max = BLE_GAP_ADV_ITVL_MS(50);
    int rc = ble_gap_adv_set_fields(&fields);
    if (!rc) rc = ble_gap_adv_rsp_set_fields(&scan);
    if (!rc) rc = ble_gap_adv_start(own_addr_type, NULL, BLE_HS_FOREVER, &params, gap_event, NULL);
    if (rc) {
        ESP_LOGE("iphone_hand", "advertising failed rc=%d; retry in 1s", rc);
        ble_npl_callout_reset(&advertise_retry, ble_npl_time_ms_to_ticks32(1000));
    }
}
static void schedule_advertise(void)
{
    ble_npl_eventq_put(nimble_port_get_dflt_eventq(), &advertise_event);
}
static void register_cb(struct ble_gatt_register_ctxt *ctx, void *arg)
{
    idf_register_cb(ctx, arg); /* Preserve esp_hid's own handle registration. */
    if (ctx->op == BLE_GATT_REGISTER_OP_CHR) {
        switch (ble_uuid_u16(ctx->chr.chr_def->uuid)) {
        case BLE_SVC_HID_CHR_UUID16_BOOT_KBD_INP: boot_keyboard = ctx->chr.val_handle; break;
        case BLE_SVC_HID_CHR_UUID16_BOOT_MOUSE_INP: boot_mouse = ctx->chr.val_handle; break;
        case BLE_SVC_HID_CHR_UUID16_PROTOCOL_MODE: protocol_handle = ctx->chr.val_handle; break;
        default: break;
        }
    } else if (ctx->op == BLE_GATT_REGISTER_OP_DSC &&
               ble_uuid_u16(ctx->dsc.dsc_def->uuid) == BLE_SVC_HID_DSC_UUID16_RPT_REF) {
        struct os_mbuf *om = NULL;
        uint8_t ref[2] = {0};
        if (!ble_att_svr_read_local(ctx->dsc.handle, &om)) {
            int rc = ble_hs_mbuf_to_flat(om, ref, sizeof(ref), NULL);
            os_mbuf_free_chain(om);
            if (!rc && ref[1] == ESP_HID_REPORT_TYPE_INPUT && ref[0] >= 1 && ref[0] <= 3 && ctx->dsc.dsc_def->arg)
                input_handles[ref[0]] = *(uint16_t *)ctx->dsc.dsc_def->arg;
        }
    }
}
static void char_write(uint16_t handle, uint16_t uuid, uint8_t value)
{
    (void)handle;
    if (uuid == BLE_SVC_HID_CHR_UUID16_PROTOCOL_MODE) protocol_report = value == 1;
    if (uuid == BLE_SVC_HID_CHR_UUID16_HID_CTRL_PT) suspended = value == 0;
    update_ready();
}
/* Pairing diagnostics: one log line per GAP event with its status/reason. */
static void log_gap_event(const struct ble_gap_event *e)
{
    switch (e->type) {
    case BLE_GAP_EVENT_CONNECT: ESP_LOGI("gap", "connect status=%d", e->connect.status); break;
    case BLE_GAP_EVENT_DISCONNECT: ESP_LOGI("gap", "disconnect reason=0x%x", e->disconnect.reason); break;
    case BLE_GAP_EVENT_ENC_CHANGE: ESP_LOGI("gap", "enc_change status=%d", e->enc_change.status); break;
    case BLE_GAP_EVENT_PASSKEY_ACTION: ESP_LOGI("gap", "passkey_action action=%d", e->passkey.params.action); break;
    case BLE_GAP_EVENT_REPEAT_PAIRING: ESP_LOGI("gap", "repeat_pairing"); break;
    case BLE_GAP_EVENT_SUBSCRIBE: ESP_LOGI("gap", "subscribe handle=%d notify=%d", e->subscribe.attr_handle, e->subscribe.cur_notify); break;
    case BLE_GAP_EVENT_MTU: ESP_LOGI("gap", "mtu %d", e->mtu.value); break;
    case BLE_GAP_EVENT_CONN_UPDATE: ESP_LOGI("gap", "conn_update status=%d", e->conn_update.status); break;
    default: ESP_LOGI("gap", "event %d", e->type); break;
    }
}
static int gap_event(struct ble_gap_event *event, void *arg)
{
    (void)arg;
    struct ble_gap_conn_desc desc;
    log_gap_event(event);
    switch (event->type) {
    case BLE_GAP_EVENT_NOTIFY_TX:
        /* The bundled NOTIFY_TX is an attempt event, not a controller credit
         * event. Success from another service can prompt a probe; failed
         * attempts must not wake themselves into a retry spin. */
        if (sending && !attempting_send && event->notify_tx.conn_handle == conn_handle &&
            event->notify_tx.status == 0)
            ble_npl_callout_reset(&send_retry, ble_npl_time_ms_to_ticks32(1));
        break;
    case BLE_GAP_EVENT_CONNECT:
        if (event->connect.status) { schedule_advertise(); break; }
        conn_handle = event->connect.conn_handle;
        ++generation;
        encrypted = ready = suspended = false;
        memset(subscribed, 0, sizeof(subscribed));
        boot_k_sub = boot_m_sub = false;
        xEventGroupClearBits(state_events, DISCONNECTED);
        peer_known = false;
        if (!ble_gap_conn_find(conn_handle, &desc)) {
            peer_known = bonded_peer(&desc.peer_id_addr) || bonded_peer(&desc.peer_ota_addr);
            const uint8_t *a = desc.peer_id_addr.val;
            snprintf(peer, sizeof(peer), "%02x:%02x:%02x:%02x:%02x:%02x", a[5], a[4], a[3], a[2], a[1], a[0]);
        }
        if (restarting || (!peer_known && !pairing_open())) { ble_gap_terminate(conn_handle, BLE_ERR_REM_USER_CONN_TERM); break; }
        { int rc = ble_gap_security_initiate(conn_handle);
          ESP_LOGI("gap", "security_initiate rc=%d", rc);
          if (rc && rc != BLE_HS_EALREADY) ble_gap_terminate(conn_handle, BLE_ERR_REM_USER_CONN_TERM); }
        break;
    case BLE_GAP_EVENT_ENC_CHANGE:
        if (event->enc_change.conn_handle != conn_handle) break;
        if (!event->enc_change.status && !ble_gap_conn_find(conn_handle, &desc) && desc.sec_state.encrypted) {
            if (!peer_known && !pairing_open()) {
                ble_store_util_delete_peer(&desc.peer_id_addr);
                ble_gap_terminate(conn_handle, BLE_ERR_REM_USER_CONN_TERM);
                break;
            }
            if (!peer_known) {
                /* First successful bond closes the window; explicit PAIR is
                 * required to admit another phone. Never trust an ephemeral link. */
                if (!desc.sec_state.bonded) {
                    ble_gap_terminate(conn_handle, BLE_ERR_REM_USER_CONN_TERM);
                    break;
                }
                peer_known = true;
                close_pairing();
                ih_emit("@0 EVT pairing open=0");
            }
            if (!encrypted) ih_emit("@0 EVT conn peer=%s", peer);
            encrypted = true;
            update_ready();
        } else {
            encrypted = ready = false;
            ble_gap_terminate(conn_handle, BLE_ERR_REM_USER_CONN_TERM);
        }
        break;
    case BLE_GAP_EVENT_SUBSCRIBE:
        if (event->subscribe.conn_handle != conn_handle) break;
        for (unsigned i = 1; i <= 3; ++i)
            if (input_handles[i] == event->subscribe.attr_handle) subscribed[i] = event->subscribe.cur_notify;
        if (boot_keyboard == event->subscribe.attr_handle) boot_k_sub = event->subscribe.cur_notify;
        if (boot_mouse == event->subscribe.attr_handle) boot_m_sub = event->subscribe.cur_notify;
        update_ready();
        break;
    case BLE_GAP_EVENT_DISCONNECT:
        if (!peer_known) ble_store_util_delete_peer(&event->disconnect.conn.peer_id_addr);
        peer_known = false;
        if (sending) ble_npl_callout_reset(&send_retry, ble_npl_time_ms_to_ticks32(1));
        conn_handle = BLE_HS_CONN_HANDLE_NONE;
        ++generation;
        encrypted = ready = false;
        memset(subscribed, 0, sizeof(subscribed));
        boot_k_sub = boot_m_sub = false;
        strcpy(peer, "-");
        ih_emit("@0 EVT disc reason=%d", event->disconnect.reason);
        xEventGroupSetBits(state_events, DISCONNECTED);
        schedule_advertise();
        break;
    case BLE_GAP_EVENT_ADV_COMPLETE: schedule_advertise(); break;
    case BLE_GAP_EVENT_REPEAT_PAIRING:
        /* The phone forgot us ("Forget This Device") but we still hold its bond:
         * drop the stale bond and retry during an authorized pairing window.
         * Outside it, even a spoofed known address must not create a new bond. */
        if (!pairing_open()) {
            ble_gap_terminate(event->repeat_pairing.conn_handle, BLE_ERR_REM_USER_CONN_TERM);
            return BLE_GAP_REPEAT_PAIRING_IGNORE;
        }
        if (ble_gap_conn_find(event->repeat_pairing.conn_handle, &desc) ||
            ble_store_util_delete_peer(&desc.peer_id_addr)) {
            ble_gap_terminate(event->repeat_pairing.conn_handle, BLE_ERR_REM_USER_CONN_TERM);
            return BLE_GAP_REPEAT_PAIRING_IGNORE;
        }
        /* Its old bond no longer authorizes it: the replacement must complete
         * within this window, including when expiry occurs mid-pairing. */
        peer_known = encrypted = ready = false;
        ++generation;
        return BLE_GAP_REPEAT_PAIRING_RETRY;
    default: break;
    }
    return 0;
}
static void sync_cb(void)
{
    ESP_ERROR_CHECK(ble_hs_util_ensure_addr(0));
    ESP_ERROR_CHECK(ble_hs_id_infer_auto(0, &own_addr_type));
    ESP_ERROR_CHECK(ble_svc_gap_device_name_set(device_name));
    ESP_ERROR_CHECK(ble_svc_gap_device_appearance_set(0x03c0));
    /* Initialize readable report values to valid, released payloads. */
    const uint8_t zero[8] = {0};
    const size_t lengths[] = {0, 8, 7, 2};
    for (unsigned i = 1; i <= 3; ++i) {
        ESP_ERROR_CHECK(input_handles[i] ? ESP_OK : ESP_FAIL);
        struct os_mbuf *om = ble_hs_mbuf_from_flat(zero, lengths[i]);
        ESP_ERROR_CHECK(om ? ESP_OK : ESP_ERR_NO_MEM);
        ESP_ERROR_CHECK(ble_att_svr_write_local(input_handles[i], om));
    }
    protocol_report = local_byte(protocol_handle, 0) == 1;
    synced = true;
    if (!pairing_initialized) {
        pairing_initialized = true;
        ble_addr_t peers[CONFIG_BT_NIMBLE_MAX_BONDS];
        int count = 0;
        if (!ble_store_util_bonded_peers(peers, &count, CONFIG_BT_NIMBLE_MAX_BONDS) && !count)
            open_pairing();
    }
    schedule_advertise();
}
static void reset_cb(int reason)
{
    synced = encrypted = ready = false;
    if (conn_handle != BLE_HS_CONN_HANDLE_NONE) ih_emit("@0 EVT disc reason=%d", reason);
    conn_handle = BLE_HS_CONN_HANDLE_NONE;
    if (sending) ble_npl_callout_reset(&send_retry, ble_npl_time_ms_to_ticks32(1));
    ++generation;
    strcpy(peer, "-");
    xEventGroupSetBits(state_events, DISCONNECTED);
}
static ih_error try_send(void)
{
    if (!encrypted || generation != request.generation) return IH_NOCONN;
    uint8_t id = request.report_id;
    uint16_t handle = input_handles[id];
    size_t len = request.length;
    uint8_t boot[3];
    const uint8_t *data = request.data;
    if (report_mode()) {
        if (!subscribed[id]) return IH_NOCONN;
    } else if (id == 1 && boot_k_sub) handle = boot_keyboard;
    else if (id == 2 && boot_m_sub && !data[5] && !data[6]) {
        handle = boot_mouse;
        /* MOVE steps are <=127 per axis, so conversion is exact. */
        boot[0] = data[0]; boot[1] = data[1]; boot[2] = data[3]; data = boot; len = 3;
    } else return IH_NOCONN;
    if (!handle) return IH_INTERNAL;
    if (!os_msys_num_free()) return IH_BUSY;
    struct os_mbuf *om = ble_hs_mbuf_from_flat(data, len);
    if (!om) return IH_BUSY;
    /* NimBLE consumes om on both success and failure. No Report ID prefix. */
    return rc_error(ble_gatts_notify_custom(conn_handle, handle, om));
}
static void send_attempt(struct ble_npl_event *ev)
{
    (void)ev;
    if (!sending) return;
    ih_error e = request.guard ? request.guard() : IH_OK;
    if (e == IH_OK && esp_timer_get_time() >= request.send_deadline) e = IH_TIMEOUT;
    if (e == IH_OK) {
        attempting_send = true;
        e = try_send();
        attempting_send = false;
    }
    if (e == IH_BUSY) {
        /* MSYS allocation/ACL resources have no public free-buffer callback in
         * this IDF. Probe on the host queue, leaving it free to process HCI
         * completion events and return mbufs. This is a total timeout, not a
         * fixed attempt count. NOTIFY_TX success/disconnect/STOP also wake us. */
        int64_t left = request.send_deadline - esp_timer_get_time();
        unsigned ms = left > 20000 ? 20 : (left > 0 ? (unsigned)((left + 999) / 1000) : 1);
        ble_npl_callout_reset(&send_retry, ble_npl_time_ms_to_ticks32(ms));
        return;
    }
    ble_npl_callout_stop(&send_retry);
    sending = false;
    request.result = e;
    xSemaphoreGive(done);
}
static void host_operation(struct ble_npl_event *ev)
{
    (void)ev;
    request.result = IH_OK;
    switch (request.op) {
    case OP_STATUS: {
        ble_addr_t peers[CONFIG_BT_NIMBLE_MAX_BONDS];
        int count = 0;
        int rc = ble_store_util_bonded_peers(peers, &count, CONFIG_BT_NIMBLE_MAX_BONDS);
        update_ready();
        request.status = (ih_ble_status){.conn = encrypted, .ready = ready, .pairing = pairing_open(), .bonded = count, .generation = generation};
        strcpy(request.status.peer, peer);
        request.result = rc_error(rc);
        break;
    }
    case OP_SEND:
        sending = true;
        send_attempt(NULL);
        return; /* The retry callout owns request until done is signalled. */
    case OP_PAIR:
        open_pairing();
        if (ble_gap_adv_active()) ble_gap_adv_stop();
        schedule_advertise();
        ih_emit("@0 EVT pairing open=1");
        break;
    case OP_ABORT:
        if (generation == request.generation && conn_handle != BLE_HS_CONN_HANDLE_NONE) {
            int rc = ble_gap_terminate(conn_handle, BLE_ERR_REM_USER_CONN_TERM);
            request.result = rc_error(rc);
            if (!rc) { encrypted = ready = false; ++generation; }
        }
        break;
    case OP_DISCONNECT:
        restarting = true;
        if (ble_gap_adv_active()) ble_gap_adv_stop();
        encrypted = ready = false;
        ++generation;
        if (conn_handle != BLE_HS_CONN_HANDLE_NONE)
            request.result = rc_error(ble_gap_terminate(conn_handle, BLE_ERR_REM_USER_CONN_TERM));
        break;
    case OP_RESUME:
        restarting = false;
        if (conn_handle != BLE_HS_CONN_HANDLE_NONE) {
            struct ble_gap_conn_desc desc;
            if (!ble_gap_conn_find(conn_handle, &desc)) encrypted = desc.sec_state.encrypted;
            update_ready();
        }
        schedule_advertise();
        break;
    case OP_UNPAIR: {
        restarting = false;
        ble_addr_t peers[CONFIG_BT_NIMBLE_MAX_BONDS];
        int count = 0;
        int rc = ble_store_util_bonded_peers(peers, &count, CONFIG_BT_NIMBLE_MAX_BONDS);
        for (int i = 0; !rc && i < count; ++i) rc = ble_store_util_delete_peer(&peers[i]);
        /* Utility deletion also removes the controller/privacy peer records. */
        if (!rc) rc = ble_store_clear();
        if (!rc) open_pairing();
        request.result = rc_error(rc);
        if (ble_gap_adv_active()) ble_gap_adv_stop();
        schedule_advertise();
        break;
    }
    case OP_NAME: {
        restarting = false;
        nvs_handle_t nvs;
        esp_err_t rc = nvs_open("iphone_hand", NVS_READWRITE, &nvs);
        if (rc == ESP_OK) {
            rc = nvs_set_str(nvs, "name", request.name);
            if (rc == ESP_OK) rc = nvs_commit(nvs);
            nvs_close(nvs);
        }
        if (rc != ESP_OK) { request.result = IH_INTERNAL; schedule_advertise(); break; }
        strcpy(device_name, request.name);
        request.result = rc_error(ble_svc_gap_device_name_set(device_name));
        if (ble_gap_adv_active()) ble_gap_adv_stop();
        schedule_advertise();
        break;
    }
    }
    xSemaphoreGive(done);
}
static ih_error perform(operation op)
{
    request.op = op;
    ble_npl_eventq_put(nimble_port_get_dflt_eventq(), &operation_event);
    xSemaphoreTake(done, portMAX_DELAY);
    return request.result;
}
ih_error ih_ble_get_status(ih_ble_status *status)
{
    ih_error result = perform(OP_STATUS);
    *status = request.status;
    return result;
}
ih_error ih_ble_send(uint8_t id, const uint8_t *data, size_t length, uint32_t gen,
                     unsigned timeout_ms, ih_error (*guard)(void))
{
    if (id < 1 || id > 3 || length > sizeof(request.data)) return IH_INTERNAL;
    request.send_deadline = esp_timer_get_time() + (int64_t)timeout_ms * 1000;
    request.guard = guard;
    request.report_id = id;
    request.length = length;
    request.generation = gen;
    memcpy(request.data, data, length);
    return perform(OP_SEND);
}
void ih_ble_wake_send(void)
{
    /* Safe from ingress: only the NimBLE host accesses the pending request. */
    ble_npl_eventq_put(nimble_port_get_dflt_eventq(), &send_wake_event);
}
ih_error ih_ble_pair(void)
{
    ih_error result = ih_ble_restart(NULL, false);
    return result == IH_OK ? perform(OP_PAIR) : result;
}
void ih_ble_abort(uint32_t gen)
{
    request.generation = gen;
    (void)perform(OP_ABORT);
}
ih_error ih_ble_restart(const char *name, bool unpair)
{
    ih_error result = perform(OP_DISCONNECT);
    if (result != IH_OK && result != IH_NOCONN) { (void)perform(OP_RESUME); return result; }
    if (!(xEventGroupWaitBits(state_events, DISCONNECTED, pdFALSE, pdTRUE, pdMS_TO_TICKS(5000)) & DISCONNECTED)) {
        (void)perform(OP_RESUME);
        return IH_BUSY;
    }
    request.name = name;
    return perform(unpair ? OP_UNPAIR : (name ? OP_NAME : OP_RESUME));
}
static void host_task(void *arg)
{
    (void)arg;
    nimble_port_run();
    nimble_port_freertos_deinit();
}
void ih_ble_init(void)
{
    done = xSemaphoreCreateBinary();
    state_events = xEventGroupCreate();
    assert(done && state_events);
    xEventGroupSetBits(state_events, DISCONNECTED);
    nvs_handle_t nvs;
    esp_err_t rc = nvs_open("iphone_hand", NVS_READONLY, &nvs);
    if (rc == ESP_OK) {
        size_t n = sizeof(device_name);
        rc = nvs_get_str(nvs, "name", device_name, &n);
        nvs_close(nvs);
        if (rc != ESP_OK && rc != ESP_ERR_NVS_NOT_FOUND) ESP_ERROR_CHECK(rc);
    } else if (rc != ESP_ERR_NVS_NOT_FOUND) ESP_ERROR_CHECK(rc);
    ESP_ERROR_CHECK(nimble_port_init());
    ble_hs_cfg.sync_cb = sync_cb;
    ble_hs_cfg.reset_cb = reset_cb;
    ble_hs_cfg.sm_io_cap = BLE_SM_IO_CAP_NO_IO;
    ble_hs_cfg.sm_bonding = 1;
    ble_hs_cfg.sm_mitm = 0;
    ble_hs_cfg.sm_sc = 1;
    ble_hs_cfg.sm_our_key_dist = BLE_SM_PAIR_KEY_DIST_ENC | BLE_SM_PAIR_KEY_DIST_ID;
    ble_hs_cfg.sm_their_key_dist = BLE_SM_PAIR_KEY_DIST_ENC | BLE_SM_PAIR_KEY_DIST_ID;
    ble_hs_cfg.store_status_cb = store_status;
    ble_store_config_init();
    static esp_hid_raw_report_map_t maps[] = {{.data = ih_report_map, .len = sizeof(ih_report_map)}};
    esp_hid_device_config_t config = {
        /* Unassigned prototype USB VID/PID: do not claim a manufacturer's allocation. */
        .vendor_id = 0, .product_id = 0, .version = 0,
        .device_name = device_name, .manufacturer_name = "iphone-hand prototype",
        .report_maps = maps, .report_maps_len = 1
    };
    ESP_ERROR_CHECK(esp_hidd_dev_init(&config, ESP_HID_TRANSPORT_BLE, NULL, &hid));
    /* esp_hid hardcodes USB vendor-ID source; explicitly use a prototype PnP ID. */
    /* Bundled DIS uses strlen() even for PnP binary data. All fields after
     * source are zero, so that implementation returns exactly our intended ID.
     * Keep a terminator for the setter's string-typed API. */
    static const uint8_t pnp[] = {2, 0, 0, 0, 0, 0, 0, 0};
    ESP_ERROR_CHECK(ble_svc_dis_pnp_id_set((const char *)pnp));
    ESP_ERROR_CHECK(esp_hidd_dev_battery_set(hid, 100)); /* USB powered; no battery sensor. */
    idf_register_cb = ble_hs_cfg.gatts_register_cb;
    ble_hs_cfg.gatts_register_cb = register_cb;
    /* Own control/protocol tracking on the NimBLE task. We use NimBLE notify
     * directly, so esp_hid's optional asynchronous write events are unnecessary. */
    ble_svc_hid_register_char_write_cb(char_write);
    ble_npl_event_init(&operation_event, host_operation, NULL);
    ble_npl_event_init(&advertise_event, advertise, NULL);
    ble_npl_event_init(&send_wake_event, send_attempt, NULL);
    ble_npl_callout_init(&send_retry, nimble_port_get_dflt_eventq(), send_attempt, NULL);
    ble_npl_callout_init(&pairing_expiry, nimble_port_get_dflt_eventq(), pairing_expired, NULL);
    ble_npl_callout_init(&advertise_retry, nimble_port_get_dflt_eventq(), advertise, NULL);
    nimble_port_freertos_init(host_task);
}
