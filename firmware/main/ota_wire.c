#include "ota_wire.h"
#include <stdlib.h>
#include <string.h>
/* Allocated on the first binary OTA frame, not statically: a 32 KB static pool left too little
 * internal RAM for esp_wifi_init next to BLE (ESP_ERR_NO_MEM boot loop, measured 2026-10-10).
 * The board reboots after an update, so the pool is never freed. */
static ih_ota_frame *pool;
static uint32_t be32(const uint8_t *p)
{
    return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 8 | p[3];
}
void ih_ota_wire_release(ih_ota_frame *frame)
{
    if (frame) atomic_store(&frame->busy, false);
}
void ih_ota_wire_reset(ih_ota_wire *w, uint32_t total)
{
    ih_ota_wire_release(w->frame);
    memset(w, 0, sizeof(*w)); w->total = total;
}
size_t ih_ota_wire_room(ih_ota_wire *w, void **data)
{
    if (w->failed || w->received == w->total) return 0;
    if (w->used < sizeof(w->header)) {
        *data = w->header + w->used;
        return sizeof(w->header) - w->used;
    }
    if (!pool && !(pool = calloc(IH_OTA_WINDOW, sizeof(*pool)))) {
        w->failed = true;
        return 0;
    }
    if (!w->frame) {
        for (unsigned i = 0; i < IH_OTA_WINDOW; ++i) {
            bool free = false;
            if (atomic_compare_exchange_strong(&pool[i].busy, &free, true)) {
                w->frame = &pool[i];
                w->frame->length = be32(w->header);
                w->frame->offset = be32(w->header + 4);
                w->frame->id = be32(w->header + 8);
                break;
            }
        }
    }
    if (!w->frame) return 0;
    *data = w->frame->data + w->used - sizeof(w->header);
    return w->frame->length - (w->used - sizeof(w->header));
}
ih_ota_frame *ih_ota_wire_commit(ih_ota_wire *w, size_t length)
{
    w->used += length;
    if (w->used == sizeof(w->header)) {
        uint32_t n = be32(w->header), offset = be32(w->header + 4), id = be32(w->header + 8);
        if (!n || n > IH_OTA_BINARY_MAX || offset != w->received ||
            n > w->total - w->received || id > 999999 ||
            (!id && (w->unacked == IH_OTA_WINDOW - 1 || n == w->total - w->received)))
            w->failed = true;
    }
    if (!w->frame || w->used != sizeof(w->header) + w->frame->length) return NULL;
    ih_ota_frame *frame = w->frame;
    w->received += frame->length;
    w->unacked = frame->id ? 0 : w->unacked + 1;
    w->frame = NULL; w->used = 0;
    return frame;
}
