#ifndef IPHONE_HAND_PROTOCOL_H
#define IPHONE_HAND_PROTOCOL_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define IH_LINE_MAX 256
#define IH_NAME_MAX 248
#define IH_DELTA_MAX 2000
#define IH_COMMAND_BUDGET_MS 10000
#define IH_NOTIFY_TIMEOUT_MS 2000
#define IH_PAIR_WINDOW_MS 120000
#define IH_OTA_CHUNK_MAX 168
#define IH_OTA_BINARY_MAX 4096  /* 8 KB pool total: 32 KB did not fit next to BLE + Wi-Fi (measured) */
#define IH_OTA_WINDOW 2
#define IH_OTA_SLOT_SIZE 0x1f0000
typedef enum { IH_OTA_BEGIN, IH_OTA_DATA, IH_OTA_END, IH_OTA_ABORT } ih_ota_op;
typedef enum {
    IH_PING, IH_STATUS, IH_MOVE, IH_CFG, IH_BTN, IH_CLICK,
    IH_SCROLL, IH_KEY, IH_KREPORT, IH_CONSUMER, IH_UNPAIR, IH_NAME, IH_WIFI, IH_TOKEN, IH_STOP, IH_PAIR, IH_OTA
} ih_kind;
typedef enum {
    IH_OK, IH_PARSE, IH_UNKNOWN, IH_ARG, IH_NOCONN, IH_BUSY, IH_INTERNAL, IH_CANCELLED, IH_TIMEOUT, IH_SIZE, IH_HASH, IH_IMAGE
} ih_error;
typedef struct {
    uint32_t id; /* 0 only when no valid request ID can be recovered. */
    ih_kind kind;
    ih_error error;
    int32_t args[7];
    bool has_step, has_interval;
    char name[IH_NAME_MAX + 1];
    char ssid[33], password[65], token[65];
    bool wifi_off;
    ih_ota_op ota_op;
    uint32_t ota_size;
    bool ota_binary;
    size_t ota_length;
    uint8_t ota_hash[32], ota_data[IH_OTA_CHUNK_MAX];
} ih_command;
typedef struct {
    char data[IH_LINE_MAX + 1];
    size_t length;
    bool overflow;
} ih_stream;

/* Length excludes LF; one terminal CR is accepted and counts toward the limit.
 * IDs/MOVE/SCROLL/CFG/BTN/CLICK are decimal; KEY/KREPORT/CONSUMER also accept 0x.
 * NAME is nonempty, valid UTF-8 without control characters; spaces are preserved.
 * hold_ms: 0..65535 (executor budget still applies). MOVE/SCROLL: +/-2000 per axis. */
ih_command ih_parse(const char *line, size_t length);
/* Returns exactly once per LF, including discarded overlong lines. */
bool ih_feed(ih_stream *stream, unsigned char byte, ih_command *out);
/* TCP provisioning and serial OTA are rejected even with invalid arguments. */
void ih_check_transport(ih_command *command, bool tcp);
const char *ih_error_name(ih_error error);
/* Consume independent axes without overflow, losing no counts. limit: 1..127. */
bool ih_next_step(int32_t *x, int32_t *y, unsigned limit, int16_t *dx, int16_t *dy);
#endif
