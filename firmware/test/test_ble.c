#define IH_BLE_NATIVE_TEST
#include "../main/ble.c"
#include <stdlib.h>

static int64_t clock_us, available_at, cancel_at;
static int notify_rc, bond_rc, bond_count, terms, security_calls, notifications, probes, deleted;
static bool advertising, resolve_rpa;
static ble_addr_t bond;
static struct ble_gap_conn_desc connection;
static struct ble_npl_event *events[32];
static unsigned event_count;
static bool signalled;

void ih_emit(const char *fmt, ...) { (void)fmt; }
int64_t esp_timer_get_time(void) { return clock_us; }
void ble_store_config_init(void) {}
void *nimble_port_get_dflt_eventq(void) { return events; }
void ble_npl_event_init(struct ble_npl_event *ev, void (*fn)(struct ble_npl_event *), void *arg)
{ (void)arg; ev->fn = fn; ev->queued = false; }
void ble_npl_eventq_put(void *queue, struct ble_npl_event *ev)
{
    (void)queue;
    if (!ev->queued) { assert(event_count < 32); ev->queued = true; events[event_count++] = ev; }
}
void ble_npl_callout_init(struct ble_npl_callout *co, void *q, void (*fn)(struct ble_npl_event *), void *arg)
{ (void)q; co->active = false; ble_npl_event_init(&co->event, fn, arg); }
void ble_npl_callout_reset(struct ble_npl_callout *co, unsigned ms)
{ assert(ms); co->active = true; co->due = clock_us + ms * 1000LL; }
void ble_npl_callout_stop(struct ble_npl_callout *co) { co->active = false; }
static void tick(void)
{
    if (event_count) {
        struct ble_npl_event *ev = events[0];
        memmove(events, events + 1, --event_count * sizeof(events[0]));
        ev->queued = false; ev->fn(ev); return;
    }
    struct ble_npl_callout *list[] = {&send_retry, &pairing_expiry, &advertise_retry};
    struct ble_npl_callout *next = NULL;
    for (unsigned i = 0; i < 3; ++i) if (list[i]->active && (!next || list[i]->due < next->due)) next = list[i];
    assert(next); clock_us = next->due; next->active = false; next->event.fn(&next->event);
}
SemaphoreHandle_t xSemaphoreCreateBinary(void) { return &signalled; }
void xSemaphoreGive(SemaphoreHandle_t sem) { (void)sem; signalled = true; }
int xSemaphoreTake(SemaphoreHandle_t sem, unsigned timeout)
{
    (void)sem; assert(timeout == portMAX_DELAY);
    unsigned iterations = 0;
    while (!signalled) { assert(++iterations < 1000); tick(); }
    signalled = false; return pdTRUE;
}
EventGroupHandle_t xEventGroupCreate(void) { static unsigned bits; bits = DISCONNECTED; return &bits; }
void xEventGroupSetBits(EventGroupHandle_t e, unsigned bits) { *e |= bits; }
void xEventGroupClearBits(EventGroupHandle_t e, unsigned bits) { *e &= ~bits; }
unsigned xEventGroupWaitBits(EventGroupHandle_t e, unsigned bits, int clear, int all, unsigned ms)
{ (void)clear; (void)all; (void)ms; return *e & bits; }
unsigned os_msys_num_free(void) { ++probes; return clock_us >= available_at; }
struct os_mbuf *ble_hs_mbuf_from_flat(const void *data, size_t n)
{
    if (clock_us < available_at) return NULL;
    struct os_mbuf *om = calloc(1, sizeof(*om)); assert(om && n <= 8); om->size = n; memcpy(om->data, data, n); return om;
}
int ble_att_svr_read_local(uint16_t handle, struct os_mbuf **out)
{ (void)handle; uint8_t one = 1; *out = ble_hs_mbuf_from_flat(&one, 1); return *out ? 0 : BLE_HS_ENOMEM; }
int ble_hs_mbuf_to_flat(struct os_mbuf *om, void *data, size_t n, void *arg)
{ (void)arg; memcpy(data, om->data, n); return 0; }
void os_mbuf_free_chain(struct os_mbuf *om) { free(om); }
int ble_gatts_notify_custom(uint16_t conn, uint16_t handle, struct os_mbuf *om)
{
    (void)handle; ++notifications; os_mbuf_free_chain(om);
    struct ble_gap_event ev = {.type = BLE_GAP_EVENT_NOTIFY_TX};
    ev.notify_tx.conn_handle = conn; ev.notify_tx.status = notify_rc;
    gap_event(&ev, NULL); return notify_rc;
}
int ble_store_util_bonded_peers(ble_addr_t *peers, int *count, int max)
{ (void)max; *count = bond_count; if (bond_count) *peers = bond; return bond_rc; }
int ble_store_util_delete_peer(const ble_addr_t *addr) { (void)addr; ++deleted; return 0; }
bool ble_gap_rpa_resolve(uint8_t *addr, uint8_t *id, uint8_t *type)
{ (void)addr; if (!resolve_rpa) return false; memcpy(id, bond.val, 6); *type = bond.type; return true; }
int ble_gap_conn_find(uint16_t conn, struct ble_gap_conn_desc *desc) { (void)conn; *desc = connection; return 0; }
int ble_gap_terminate(uint16_t conn, int reason) { (void)conn; (void)reason; ++terms; return 0; }
int ble_gap_security_initiate(uint16_t conn) { (void)conn; ++security_calls; return 0; }
bool ble_gap_adv_active(void) { return advertising; }
int ble_gap_adv_stop(void) { advertising = false; return 0; }
int ble_gap_adv_start(uint8_t type, const void *peer_addr, int duration, const struct ble_gap_adv_params *params,
                      int (*cb)(struct ble_gap_event *, void *), void *arg)
{ (void)type; (void)peer_addr; (void)duration; (void)params; (void)cb; (void)arg; advertising = true; return 0; }
int ble_gap_adv_set_fields(const struct ble_hs_adv_fields *f) { (void)f; return 0; }
int ble_gap_adv_rsp_set_fields(const struct ble_hs_adv_fields *f) { (void)f; return 0; }
static ih_error guard(void) { return cancel_at && clock_us >= cancel_at ? IH_CANCELLED : IH_OK; }
static void reset(void)
{
    clock_us = available_at = cancel_at = 0;
    notify_rc = bond_rc = bond_count = terms = security_calls = notifications = probes = deleted = 0;
    advertising = resolve_rpa = false; signalled = false; event_count = 0;
    generation = 7; conn_handle = 1; encrypted = protocol_report = synced = true;
    restarting = ready = suspended = peer_known = false; pair_deadline = 0; pairing_initialized = false; sending = attempting_send = false;
    input_handles[1] = 1; input_handles[2] = 2; input_handles[3] = 3; protocol_handle = 4;
    memset(subscribed, 1, sizeof(subscribed)); request.guard = NULL;
    done = xSemaphoreCreateBinary(); state_events = xEventGroupCreate();
    ble_npl_event_init(&operation_event, host_operation, NULL);
    ble_npl_event_init(&advertise_event, advertise, NULL);
    ble_npl_event_init(&send_wake_event, send_attempt, NULL);
    ble_npl_callout_init(&send_retry, events, send_attempt, NULL);
    ble_npl_callout_init(&pairing_expiry, events, pairing_expired, NULL);
    ble_npl_callout_init(&advertise_retry, events, advertise, NULL);
    bond = (ble_addr_t){.type = 0, .val = {1, 2, 3, 4, 5, 6}};
    connection = (struct ble_gap_conn_desc){0};
}
static void begin_pending_send(void)
{
    available_at = 3000000;
    request.op = OP_SEND; request.report_id = 1; request.length = 8;
    request.generation = generation; request.guard = guard; request.send_deadline = 2000000;
    memset(request.data, 0, sizeof(request.data));
    host_operation(NULL);
    assert(sending && !signalled && send_retry.active);
}
static void connect_peer(bool known)
{
    connection.peer_id_addr = known ? bond : (ble_addr_t){.type = 0, .val = {9}};
    connection.peer_ota_addr = connection.peer_id_addr;
    struct ble_gap_event ev = {.type = BLE_GAP_EVENT_CONNECT}; ev.connect.conn_handle = 1; gap_event(&ev, NULL);
}
int main(void)
{
    uint8_t key[8] = {0};
    reset(); available_at = 400000; /* More than four old retry delays. */
    assert(ih_ble_send(1, key, 8, 7, 2000, guard) == IH_OK);
    assert(clock_us == 400000 && probes == 21 && notifications == 1 && !sending);
    reset(); available_at = 3000000;
    assert(ih_ble_send(1, key, 8, 7, 2000, guard) == IH_TIMEOUT);
    assert(clock_us == 2000000 && notifications == 0 && !send_retry.active);
    reset(); notify_rc = BLE_HS_ENOMEM;
    assert(ih_ble_send(1, key, 8, 7, 2000, guard) == IH_TIMEOUT);
    assert(clock_us == 2000000 && notifications == 100); /* Failed NOTIFY_TX does not spin. */
    reset(); available_at = 3000000; cancel_at = 55000;
    assert(ih_ble_send(1, key, 8, 7, 2000, guard) == IH_CANCELLED && clock_us == 60000);
    reset(); assert(ih_ble_send(1, key, 8, 6, 2000, guard) == IH_NOCONN && !notifications);
    reset(); begin_pending_send(); clock_us = 5000; cancel_at = 1;
    ih_ble_wake_send(); tick(); assert(!sending && signalled && request.result == IH_CANCELLED && clock_us == 5000);
    reset(); begin_pending_send(); available_at = 1000;
    struct ble_gap_event tx = {.type = BLE_GAP_EVENT_NOTIFY_TX}; tx.notify_tx.conn_handle = 1;
    gap_event(&tx, NULL); assert(send_retry.due == 1000); tick();
    assert(!sending && signalled && notifications == 1 && request.result == IH_OK);
    reset(); begin_pending_send(); clock_us = 5000;
    struct ble_gap_event disc = {.type = BLE_GAP_EVENT_DISCONNECT}; gap_event(&disc, NULL);
    tick(); tick(); assert(!sending && signalled && request.result == IH_NOCONN && !notifications);
    reset(); subscribed[1] = false;
    assert(ih_ble_send(1, key, 8, 7, 2000, guard) == IH_NOCONN && !notifications);
    reset(); suspended = true; assert(ih_ble_send(1, key, 8, 7, 2000, guard) == IH_OK);
    reset(); char_write(4, BLE_SVC_HID_CHR_UUID16_PROTOCOL_MODE, 0);
    assert(!report_mode()); char_write(4, BLE_SVC_HID_CHR_UUID16_PROTOCOL_MODE, 1); assert(report_mode());
    reset(); connect_peer(false); assert(terms == 1 && security_calls == 0 && !encrypted);
    reset(); bond_count = 1; connect_peer(true); assert(!terms && security_calls == 1 && peer_known);
    memset(subscribed, 1, sizeof(subscribed));
    connection.sec_state.encrypted = true;
    struct ble_gap_event enc = {.type = BLE_GAP_EVENT_ENC_CHANGE}; enc.enc_change.conn_handle = 1;
    gap_event(&enc, NULL); assert(encrypted && ready); /* Bonded reconnect, closed window. */
    reset(); bond_count = 1; resolve_rpa = true;
    ble_addr_t rpa = {.type = BLE_ADDR_RANDOM, .val = {9, 0, 0, 0, 0, 0x40}};
    assert(bonded_peer(&rpa)); resolve_rpa = false; assert(!bonded_peer(&rpa));
    reset(); conn_handle = BLE_HS_CONN_HANDLE_NONE; sync_cb(); assert(pairing_open());
    connect_peer(false); assert(!terms && security_calls == 1);
    clock_us = IH_PAIR_WINDOW_MS * 1000LL; pairing_expired(NULL); assert(!pairing_open() && terms == 1 && deleted == 1);
    reset(); open_pairing(); connect_peer(false); connection.sec_state.encrypted = connection.sec_state.bonded = true;
    gap_event(&enc, NULL); assert(encrypted && peer_known && !pairing_open());
    reset(); bond_count = 1; conn_handle = BLE_HS_CONN_HANDLE_NONE; sync_cb(); assert(!pairing_open() && !pairing_expiry.active);
    advertise(NULL); assert(advertising); /* Bonded reconnect advertising persists. */
    reset(); conn_handle = BLE_HS_CONN_HANDLE_NONE; advertise(NULL); assert(!advertising);
    reset(); bond_count = 1; bond_rc = BLE_HS_ENOMEM; conn_handle = BLE_HS_CONN_HANDLE_NONE;
    advertise(NULL); assert(!advertising && advertise_retry.active);
    reset(); struct ble_gap_event repeat = {.type = BLE_GAP_EVENT_REPEAT_PAIRING}; repeat.repeat_pairing.conn_handle = 1;
    assert(gap_event(&repeat, NULL) == BLE_GAP_REPEAT_PAIRING_IGNORE && terms == 1 && !deleted);
    open_pairing(); assert(gap_event(&repeat, NULL) == BLE_GAP_REPEAT_PAIRING_RETRY && deleted == 1);
    assert(!peer_known && !encrypted);
    clock_us = IH_PAIR_WINDOW_MS * 1000LL; pairing_expired(NULL); assert(terms == 2);
    assert(store_status(NULL, NULL) == BLE_HS_ENOMEM);
    puts("PASS: BLE mbuf/notify retries, timeout/cancel, pairing window and bonded/RPA admission");
    return 0;
}
