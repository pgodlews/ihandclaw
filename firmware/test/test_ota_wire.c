#include "../main/ota_wire.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
static void put(uint8_t *p, uint32_t n)
{ p[0] = n >> 24; p[1] = n >> 16; p[2] = n >> 8; p[3] = n; }
static ih_ota_frame *feed(ih_ota_wire *w, const uint8_t *bytes, size_t n, size_t fragment)
{
    ih_ota_frame *frame = NULL;
    while (n) {
        void *dst; size_t room = ih_ota_wire_room(w, &dst);
        assert(room);
        size_t take = n < room ? n : room;
        if (take > fragment) take = fragment;
        memcpy(dst, bytes, take); bytes += take; n -= take;
        frame = ih_ota_wire_commit(w, take);
        if (w->failed) return NULL;
    }
    return frame;
}
int main(void)
{
    uint8_t packet[IH_OTA_BINARY_MAX + 12];
    for (unsigned i = 12; i < sizeof(packet); ++i) packet[i] = (uint8_t)i;
    ih_ota_wire w = {0}; ih_ota_frame *frames[IH_OTA_WINDOW];
    ih_ota_wire_reset(&w, IH_OTA_BINARY_MAX * (IH_OTA_WINDOW + 1));
    for (unsigned i = 0; i < IH_OTA_WINDOW; ++i) {
        put(packet, IH_OTA_BINARY_MAX); put(packet + 4, i * IH_OTA_BINARY_MAX); put(packet + 8, i == IH_OTA_WINDOW - 1 ? 123 : 0);
        frames[i] = feed(&w, packet, sizeof(packet), 7);
        assert(frames[i] && !memcmp(frames[i]->data, packet + 12, IH_OTA_BINARY_MAX));
    }
    put(packet + 4, IH_OTA_BINARY_MAX * IH_OTA_WINDOW); put(packet + 8, 124);
    assert(!feed(&w, packet, 12, 1));
    void *dst; assert(ih_ota_wire_room(&w, &dst) == 0); /* Pool full: backpressure. */
    ih_ota_wire_release(frames[0]);
    ih_ota_frame *last = feed(&w, packet + 12, IH_OTA_BINARY_MAX, 4096);
    assert(last && last->id == 124 && w.received == w.total);
    ih_ota_wire_release(last);
    for (unsigned i = 1; i < IH_OTA_WINDOW; ++i) ih_ota_wire_release(frames[i]);
    for (unsigned bad = 0; bad < 5; ++bad) {
        ih_ota_wire_reset(&w, 10);
        put(packet, bad == 0 ? 0 : bad == 1 ? IH_OTA_BINARY_MAX + 1 : bad == 2 ? 11 : 10);
        put(packet + 4, bad == 3 ? 1 : 0); put(packet + 8, bad == 4 ? 0 : 1);
        assert(!feed(&w, packet, 12, 3) && w.failed);
    }
    ih_ota_wire_reset(&w, 10); put(packet, 10); put(packet + 4, 0); put(packet + 8, 1);
    assert(!feed(&w, packet, 17, 1) && w.frame);
    ih_ota_wire_reset(&w, 0); /* Disconnect releases partially received buffer. */
    puts("OTA binary framing, short reads, pool backpressure and bounds tests passed");
}
