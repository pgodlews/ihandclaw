#ifndef IH_OTA_WIRE_H
#define IH_OTA_WIRE_H
#include "link.h"
#include <stdatomic.h>
/* IH_OTA_WINDOW bounded buffers: ownership transfers ingress -> executor -> pool. */
struct ih_ota_frame {
    _Atomic bool busy;
    uint32_t length, offset, id;
    uint8_t data[IH_OTA_BINARY_MAX];
};
typedef struct {
    uint8_t header[12];
    size_t used;
    uint32_t total, received;
    unsigned unacked;
    ih_ota_frame *frame;
    bool failed;
} ih_ota_wire;
void ih_ota_wire_reset(ih_ota_wire *wire, uint32_t total);
/* Returns writable receive space, or zero for executor backpressure. */
size_t ih_ota_wire_room(ih_ota_wire *wire, void **data);
/* Commit a short receive. Returns a completed frame with transferred ownership. */
ih_ota_frame *ih_ota_wire_commit(ih_ota_wire *wire, size_t length);
void ih_ota_wire_release(ih_ota_frame *frame);
#endif
