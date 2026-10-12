#ifndef IPHONE_HAND_BLE_H
#define IPHONE_HAND_BLE_H
#include "protocol.h"
void ih_emit(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
void ih_ble_init(void);
typedef struct { bool conn, ready, pairing; int bonded; uint32_t generation; char peer[18]; } ih_ble_status;
ih_error ih_ble_get_status(ih_ble_status *status);
/* generation pins a whole command to one connection, preventing replay after reconnect. */
/* guard runs on the host task: it must never block. NULL for safety releases.
 * Resource retries yield the host until success, cancellation or total timeout. */
ih_error ih_ble_send(uint8_t id, const uint8_t *data, size_t length, uint32_t generation,
                     unsigned timeout_ms, ih_error (*guard)(void));
void ih_ble_wake_send(void);
ih_error ih_ble_pair(void);
void ih_ble_abort(uint32_t generation);
ih_error ih_ble_restart(const char *name, bool unpair);
#endif
