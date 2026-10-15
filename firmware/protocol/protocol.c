#include "protocol.h"
#include <errno.h>
#include <limits.h>
#include <stdlib.h>
#include <string.h>

static bool ws(char c) { return c == ' ' || c == '\t'; }
static char *token(char **cursor)
{
    char *p = *cursor;
    while (ws(*p)) ++p;
    if (!*p) { *cursor = p; return NULL; }
    char *start = p;
    while (*p && !ws(*p)) ++p;
    if (*p) *p++ = 0;
    *cursor = p;
    return start;
}
static bool number(const char *s, int32_t low, int32_t high, bool hex, int32_t *out)
{
    if (!s || !*s) return false;
    const char *p = s;
    if (*p == '-' || *p == '+') ++p;
    int base = 10;
    if (hex && p[0] == '0' && (p[1] == 'x' || p[1] == 'X')) { base = 16; p += 2; }
    if (!*p) return false;
    for (; *p; ++p) {
        if (!((*p >= '0' && *p <= '9') ||
              (base == 16 && ((*p >= 'a' && *p <= 'f') || (*p >= 'A' && *p <= 'F'))))) return false;
    }
    errno = 0;
    char *end;
    long long value = strtoll(s, &end, base);
    if (errno || *end || value < low || value > high) return false;
    *out = (int32_t)value;
    return true;
}
static bool utf8_name(const unsigned char *s)
{
    while (*s) {
        uint32_t cp;
        unsigned count;
        if (*s < 0x80) { cp = *s++; count = 0; }
        else if (*s >= 0xc2 && *s <= 0xdf) { cp = *s++ & 0x1f; count = 1; }
        else if (*s >= 0xe0 && *s <= 0xef) { cp = *s++ & 0x0f; count = 2; }
        else if (*s >= 0xf0 && *s <= 0xf4) { cp = *s++ & 7; count = 3; }
        else return false;
        unsigned n = count;
        while (n--) {
            if ((*s & 0xc0) != 0x80) return false;
            cp = (cp << 6) | (*s++ & 0x3f);
        }
        if ((count == 1 && cp < 0x80) || (count == 2 && cp < 0x800) ||
            (count == 3 && cp < 0x10000) || cp > 0x10ffff ||
            (cp >= 0xd800 && cp <= 0xdfff) || cp < 0x20 || (cp >= 0x7f && cp <= 0x9f)) return false;
    }
    return true;
}
static int hex_digit(unsigned char ch)
{
    if (ch >= '0' && ch <= '9') return ch - '0';
    if (ch >= 'a' && ch <= 'f') return ch - 'a' + 10;
    if (ch >= 'A' && ch <= 'F') return ch - 'A' + 10;
    return -1;
}
static bool percent_decode(const char *src, char *dst, size_t capacity)
{
    size_t n = 0;
    while (*src) {
        unsigned char ch = (unsigned char)*src++;
        if (ch == '%') {
            if (!src[0] || !src[1]) return false;
            int hi = hex_digit((unsigned char)src[0]), lo = hex_digit((unsigned char)src[1]);
            if (hi < 0 || lo < 0) return false;
            ch = (unsigned char)((hi << 4) | lo);
            src += 2;
        }
        if (!ch || ch < 0x20 || ch == 0x7f || n + 1 >= capacity) return false;
        dst[n++] = (char)ch;
    }
    dst[n] = 0;
    return n != 0;
}
/* Strict canonical RFC 4648 base64: no whitespace, alternate alphabet or
 * nonzero padding bits. Decoding never exposes binary to the line stream. */
static int base64_digit(char ch)
{
    const char *alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    const char *found = ch ? strchr(alphabet, ch) : NULL;
    return found ? (int)(found - alphabet) : -1;
}
static bool base64_decode(const char *text, ih_command *c)
{
    size_t n = strlen(text);
    if (!n || n % 4 || n > IH_OTA_CHUNK_MAX / 3 * 4) return false;
    for (size_t i = 0; i < n; i += 4) {
        int a = base64_digit(text[i]), b = base64_digit(text[i+1]);
        int d = base64_digit(text[i+2]), e = base64_digit(text[i+3]);
        bool pad2 = text[i+2] == '=', pad3 = text[i+3] == '=';
        if (a < 0 || b < 0 || (!pad2 && d < 0) || (!pad3 && e < 0) ||
            (pad2 && !pad3) || ((pad2 || pad3) && i + 4 != n) ||
            (pad2 && (b & 15)) || (!pad2 && pad3 && (d & 3))) return false;
        c->ota_data[c->ota_length++] = (uint8_t)((a << 2) | (b >> 4));
        if (!pad2) c->ota_data[c->ota_length++] = (uint8_t)((b << 4) | (d >> 2));
        if (!pad3) c->ota_data[c->ota_length++] = (uint8_t)((d << 6) | e);
    }
    return true;
}
void ih_check_transport(ih_command *c, bool tcp)
{
    if (c->id && ((tcp && (c->kind == IH_WIFI || c->kind == IH_TOKEN)) ||
                  (!tcp && c->kind == IH_OTA))) c->error = IH_ARG;
}
ih_command ih_parse(const char *line, size_t length)
{
    ih_command c = {.error = IH_PARSE};
    char buf[IH_LINE_MAX + 1];
    size_t copy = length > IH_LINE_MAX ? IH_LINE_MAX : length;
    memcpy(buf, line, copy);
    buf[copy] = 0;
    char *p = buf;
    char *id = token(&p);
    int32_t value;
    bool decimal_id = id && *id;
    if (id) for (const char *q = id; *q; ++q) if (*q < '0' || *q > '9') decimal_id = false;
    if (decimal_id && number(id, 1, 999999, false, &value)) c.id = (uint32_t)value;
    if (!c.id || length > IH_LINE_MAX || memchr(line, 0, length)) return c;
    if (copy && buf[copy - 1] == '\r') buf[copy - 1] = 0;
    for (size_t i = 0; i < copy; ++i) {
        unsigned char ch = (unsigned char)buf[i];
        if ((ch < 0x20 && ch != '\t' && ch != 0) || ch == 0x7f) return c;
    }
    char *cmd = token(&p);
    if (!cmd) return c;
    const char *names[] = {"PING", "STATUS", "MOVE", "CFG", "BTN", "CLICK", "SCROLL",
                           "KEY", "KREPORT", "CONSUMER", "UNPAIR", "NAME", "WIFI", "TOKEN", "STOP", "PAIR", "OTA"};
    unsigned k;
    for (k = 0; k < sizeof(names)/sizeof(names[0]); ++k) if (!strcmp(cmd, names[k])) break;
    if (k == sizeof(names)/sizeof(names[0])) { c.error = IH_UNKNOWN; return c; }
    c.kind = (ih_kind)k;
    c.error = IH_ARG;
    if (c.kind == IH_NAME) {
        while (ws(*p)) ++p;
        size_t n = strlen(p);
        if (!n || n > IH_NAME_MAX || !utf8_name((unsigned char *)p)) return c;
        memcpy(c.name, p, n + 1);
        c.error = IH_OK;
        return c;
    }
    char *a[8];
    size_t n = 0;
    char *t;
    while ((t = token(&p))) {
        if (n == 8) return c;
        a[n++] = t;
    }
    int32_t lows[7] = {0}, highs[7] = {0};
    size_t min = 0, max = 0;
    bool hex = false;
    switch (c.kind) {
    case IH_PING: case IH_STATUS: case IH_UNPAIR: case IH_STOP: case IH_PAIR: break;
    case IH_CFG:
        if (!n || n > 2) return c;
        for (size_t i = 0; i < n; ++i) {
            if (!strncmp(a[i], "step=", 5) && !c.has_step && number(a[i]+5, 1, 127, false, &c.args[0])) c.has_step = true;
            else if (!strncmp(a[i], "interval=", 9) && !c.has_interval && number(a[i]+9, 5, 100, false, &c.args[1])) c.has_interval = true;
            else return c;
        }
        c.error = IH_OK;
        return c;
    case IH_MOVE:
        min = max = 2; lows[0] = lows[1] = -IH_DELTA_MAX; highs[0] = highs[1] = IH_DELTA_MAX; break;
    case IH_SCROLL:
        min = 1; max = 2; lows[0] = lows[1] = -IH_DELTA_MAX; highs[0] = highs[1] = IH_DELTA_MAX; break;
    case IH_BTN: min = max = 1; highs[0] = 7; break;
    case IH_CLICK:
        max = 2; lows[0] = 1; highs[0] = 7; highs[1] = 65535;
        c.args[0] = 1; c.args[1] = 60; break;
    case IH_KEY:
        min = 2; max = 3; hex = true; highs[0] = highs[1] = 255; highs[2] = 65535;
        c.args[2] = 30; break;
    case IH_KREPORT:
        min = 2; max = 7; hex = true;
        for (size_t i = 0; i < 7; ++i) highs[i] = 255;
        break;
    case IH_CONSUMER:
        min = 1; max = 2; hex = true; highs[0] = highs[1] = 65535; c.args[1] = 30; break;
    case IH_WIFI:
        if (n == 1 && !strcmp(a[0], "off")) c.wifi_off = true;
        else if (n != 2 || !percent_decode(a[0], c.ssid, sizeof(c.ssid)) ||
                 !percent_decode(a[1], c.password, sizeof(c.password))) return c;
        c.error = IH_OK;
        return c;
    case IH_TOKEN:
        if (n != 1 || strlen(a[0]) < 32 || strlen(a[0]) > 64) return c;
        for (const char *q = a[0]; *q; ++q) if (hex_digit((unsigned char)*q) < 0) return c;
        strcpy(c.token, a[0]);
        c.error = IH_OK;
        return c;
    case IH_OTA:
        if ((n == 3 || (n == 4 && !strcmp(a[3], "BIN"))) && !strcmp(a[0], "BEGIN")) {
            c.ota_binary = n == 4;
            if (!number(a[1], 1, IH_OTA_SLOT_SIZE, false, &value) || strlen(a[2]) != 64) return c;
            c.ota_op = IH_OTA_BEGIN; c.ota_size = (uint32_t)value;
            for (size_t i = 0; i < 32; ++i) {
                int hi = hex_digit((unsigned char)a[2][i*2]), lo = hex_digit((unsigned char)a[2][i*2+1]);
                if (hi < 0 || lo < 0) return c;
                c.ota_hash[i] = (uint8_t)((hi << 4) | lo);
            }
        } else if (n == 2 && !strcmp(a[0], "DATA")) {
            c.ota_op = IH_OTA_DATA;
            if (!base64_decode(a[1], &c)) return c;
        } else if (n == 1 && !strcmp(a[0], "END")) c.ota_op = IH_OTA_END;
        else if (n == 1 && !strcmp(a[0], "ABORT")) c.ota_op = IH_OTA_ABORT;
        else return c;
        c.error = IH_OK;
        return c;
    case IH_NAME: return c;
    }
    if (n < min || n > max) return c;
    for (size_t i = 0; i < n; ++i) if (!number(a[i], lows[i], highs[i], hex, &c.args[i])) return c;
    c.error = IH_OK;
    return c;
}
bool ih_feed(ih_stream *s, unsigned char byte, ih_command *out)
{
    if (byte == '\n') {
        *out = ih_parse(s->data, s->overflow ? IH_LINE_MAX + 1 : s->length);
        *s = (ih_stream){0};
        return true;
    }
    if (s->length < IH_LINE_MAX) s->data[s->length++] = (char)byte;
    else s->overflow = true;
    return false;
}
const char *ih_error_name(ih_error e)
{
    static const char *names[] = {"OK", "PARSE", "UNKNOWN", "ARG", "NOCONN", "BUSY", "INTERNAL", "CANCELLED", "TIMEOUT", "SIZE", "HASH", "IMAGE"};
    return e <= IH_IMAGE && e >= IH_OK ? names[e] : "INTERNAL";
}
bool ih_next_step(int32_t *x, int32_t *y, unsigned limit, int16_t *dx, int16_t *dy)
{
    if (!limit || limit > 127 || (!*x && !*y)) return false;
    int32_t bound = (int32_t)limit;
    *dx = (int16_t)(*x > bound ? bound : (*x < -bound ? -bound : *x));
    *dy = (int16_t)(*y > bound ? bound : (*y < -bound ? -bound : *y));
    *x -= *dx;
    *y -= *dy;
    return true;
}
