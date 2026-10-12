/* Narrow IDF/NimBLE doubles for running the actual ble.c state machine natively.
 * They model app-visible events/resources, never radio/controller behavior. */
#ifndef IH_BLE_RUNTIME_H
#define IH_BLE_RUNTIME_H
#include "runtime_stubs/runtime.h"
#define CONFIG_BT_NIMBLE_MAX_BONDS 3
#define BLE_HS_CONN_HANDLE_NONE UINT16_MAX
#define BLE_HS_ENOTCONN 1
#define BLE_HS_EBUSY 2
#define BLE_HS_ENOMEM 3
#define BLE_HS_EAGAIN 4
#define BLE_HS_EALREADY 5
#define ESP_ERR_NVS_NOT_FOUND 3
#define ESP_FAIL 4
#define BLE_ERR_REM_USER_CONN_TERM 0x13
#define BLE_ADDR_RANDOM 1
#define BLE_GAP_REPEAT_PAIRING_RETRY 1
#define BLE_GAP_REPEAT_PAIRING_IGNORE 0
#define BLE_HS_FOREVER INT32_MAX
#define BLE_HS_ADV_F_DISC_GEN 2
#define BLE_HS_ADV_F_BREDR_UNSUP 4
#define BLE_GAP_CONN_MODE_UND 1
#define BLE_GAP_DISC_MODE_GEN 1
#define BLE_GAP_ADV_ITVL_MS(ms) (ms)
#define ESP_HID_REPORT_TYPE_INPUT 1
#define ESP_HID_TRANSPORT_BLE 1
#define NVS_READONLY 0
#define NVS_READWRITE 1
#define BLE_SM_IO_CAP_NO_IO 3
#define BLE_SM_PAIR_KEY_DIST_ENC 1
#define BLE_SM_PAIR_KEY_DIST_ID 2
#define BIT0 1
#define pdFALSE 0
#define BLE_UUID16_INIT(v) {.value = (v)}
typedef struct { uint16_t value; } ble_uuid16_t;
typedef struct { uint8_t type, val[6]; } ble_addr_t;
struct ble_gap_conn_desc {
    ble_addr_t peer_id_addr, peer_ota_addr;
    struct { bool encrypted, bonded; } sec_state;
};
enum { BLE_GAP_EVENT_CONNECT, BLE_GAP_EVENT_DISCONNECT, BLE_GAP_EVENT_ENC_CHANGE,
       BLE_GAP_EVENT_PASSKEY_ACTION, BLE_GAP_EVENT_REPEAT_PAIRING, BLE_GAP_EVENT_SUBSCRIBE,
       BLE_GAP_EVENT_MTU, BLE_GAP_EVENT_CONN_UPDATE, BLE_GAP_EVENT_NOTIFY_TX, BLE_GAP_EVENT_ADV_COMPLETE };
struct ble_gap_event {
    int type;
    struct { int status; uint16_t conn_handle; } connect, enc_change, conn_update;
    struct { int reason; struct ble_gap_conn_desc conn; } disconnect;
    struct { struct { int action; } params; } passkey;
    struct { uint16_t conn_handle; } repeat_pairing;
    struct { uint16_t conn_handle, attr_handle; bool cur_notify; } subscribe;
    struct { unsigned value; } mtu;
    struct { int status; uint16_t conn_handle; } notify_tx;
};
struct ble_hs_adv_fields {
    int flags, appearance, appearance_is_present, num_uuids16, uuids16_is_complete;
    const ble_uuid16_t *uuids16; const uint8_t *name; uint8_t name_len; bool name_is_complete;
};
struct ble_gap_adv_params { int conn_mode, disc_mode, itvl_min, itvl_max; };
struct ble_npl_event { void (*fn)(struct ble_npl_event *); bool queued; };
struct ble_npl_callout { struct ble_npl_event event; bool active; int64_t due; };
void *nimble_port_get_dflt_eventq(void);
void ble_npl_event_init(struct ble_npl_event *, void (*)(struct ble_npl_event *), void *);
void ble_npl_eventq_put(void *, struct ble_npl_event *);
void ble_npl_callout_init(struct ble_npl_callout *, void *, void (*)(struct ble_npl_event *), void *);
void ble_npl_callout_reset(struct ble_npl_callout *, unsigned);
void ble_npl_callout_stop(struct ble_npl_callout *);
#define ble_npl_time_ms_to_ticks32(ms) (ms)
typedef void *SemaphoreHandle_t;
typedef unsigned *EventGroupHandle_t;
SemaphoreHandle_t xSemaphoreCreateBinary(void);
void xSemaphoreGive(SemaphoreHandle_t);
int xSemaphoreTake(SemaphoreHandle_t, unsigned);
EventGroupHandle_t xEventGroupCreate(void);
void xEventGroupSetBits(EventGroupHandle_t, unsigned);
void xEventGroupClearBits(EventGroupHandle_t, unsigned);
unsigned xEventGroupWaitBits(EventGroupHandle_t, unsigned, int, int, unsigned);
struct os_mbuf { uint8_t data[8]; size_t size; };
unsigned os_msys_num_free(void);
struct os_mbuf *ble_hs_mbuf_from_flat(const void *, size_t);
int ble_att_svr_read_local(uint16_t, struct os_mbuf **);
int ble_hs_mbuf_to_flat(struct os_mbuf *, void *, size_t, void *);
void os_mbuf_free_chain(struct os_mbuf *);
int ble_gatts_notify_custom(uint16_t, uint16_t, struct os_mbuf *);
int ble_store_util_bonded_peers(ble_addr_t *, int *, int);
int ble_store_util_delete_peer(const ble_addr_t *);
bool ble_gap_rpa_resolve(uint8_t *, uint8_t *, uint8_t *);
int ble_gap_conn_find(uint16_t, struct ble_gap_conn_desc *);
int ble_gap_terminate(uint16_t, int);
int ble_gap_security_initiate(uint16_t);
bool ble_gap_adv_active(void);
int ble_gap_adv_stop(void);
int ble_gap_adv_start(uint8_t, const void *, int, const struct ble_gap_adv_params *, int (*)(struct ble_gap_event *, void *), void *);
int ble_gap_adv_set_fields(const struct ble_hs_adv_fields *);
int ble_gap_adv_rsp_set_fields(const struct ble_hs_adv_fields *);
#define ESP_LOGI(tag, ...) ((void)(tag))
#define ESP_LOGE(tag, ...) ((void)(tag))
/* Startup-only API shapes. Tests initialize state explicitly. */
#define BLE_GATT_REGISTER_OP_CHR 1
#define BLE_GATT_REGISTER_OP_DSC 2
#define BLE_SVC_HID_CHR_UUID16_BOOT_KBD_INP 1
#define BLE_SVC_HID_CHR_UUID16_BOOT_MOUSE_INP 2
#define BLE_SVC_HID_CHR_UUID16_PROTOCOL_MODE 3
#define BLE_SVC_HID_CHR_UUID16_HID_CTRL_PT 4
#define BLE_SVC_HID_DSC_UUID16_RPT_REF 5
struct test_definition { const ble_uuid16_t *uuid; void *arg; };
struct ble_gatt_register_ctxt {
    int op;
    struct { struct test_definition *chr_def; uint16_t val_handle; } chr;
    struct { struct test_definition *dsc_def; uint16_t handle; } dsc;
};
#define ble_uuid_u16(uuid) ((uuid)->value)
struct ble_store_status_event { int event_code; };
struct test_hs_config {
    void (*sync_cb)(void); void (*reset_cb)(int);
    void (*gatts_register_cb)(struct ble_gatt_register_ctxt *, void *);
    int sm_io_cap, sm_bonding, sm_mitm, sm_sc, sm_our_key_dist, sm_their_key_dist;
    int (*store_status_cb)(struct ble_store_status_event *, void *);
};
static struct test_hs_config ble_hs_cfg;
#define ble_store_util_status_rr 0
#define ble_store_clear() 0
#define ble_hs_util_ensure_addr(x) 0
#define ble_hs_id_infer_auto(x, type) (*(type) = 0, 0)
#define ble_svc_gap_device_name_set(name) 0
#define ble_svc_gap_device_appearance_set(v) 0
#define ble_svc_dis_pnp_id_set(name) ((void)(name), 0)
#define ble_att_svr_write_local(handle, om) ((void)(handle), os_mbuf_free_chain(om), 0)
#define ble_svc_hid_register_char_write_cb(cb) ((void)(cb))
#define nimble_port_init() 0
#define nimble_port_run() ((void)0)
#define nimble_port_freertos_deinit() ((void)0)
#define nimble_port_freertos_init(fn) ((void)(fn))
typedef unsigned nvs_handle_t;
#define nvs_open(name, mode, handle) ((void)(name), (void)(mode), *(handle) = 0, ESP_ERR_NVS_NOT_FOUND)
#define nvs_get_str(handle, name, data, size) ((void)(handle), (void)(name), (void)(data), (void)(size), ESP_ERR_NVS_NOT_FOUND)
#define nvs_set_str(handle, name, data) ((void)(handle), (void)(name), (void)(data), 0)
#define nvs_commit(handle) ((void)(handle), 0)
#define nvs_close(handle) ((void)(handle))
typedef void esp_hidd_dev_t;
typedef struct { const uint8_t *data; size_t len; } esp_hid_raw_report_map_t;
typedef struct {
    unsigned vendor_id, product_id, version;
    const char *device_name, *manufacturer_name;
    esp_hid_raw_report_map_t *report_maps; unsigned report_maps_len;
} esp_hid_device_config_t;
#define esp_hidd_dev_init(config, transport, cb, handle) ((void)(config), (void)(transport), (void)(cb), *(handle) = NULL, 0)
#define esp_hidd_dev_battery_set(handle, value) ((void)(handle), (void)(value), 0)
#endif
