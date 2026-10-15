#include "protocol.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

static ih_command parse(const char *s) { return ih_parse(s, strlen(s)); }
int main(void)
{
    const char *hash = "0123456789abcdef0123456789ABCDEF0123456789abcdef0123456789abcdef";
    char line[300];
    snprintf(line, sizeof(line), "999999 OTA BEGIN %u %s", IH_OTA_SLOT_SIZE, hash);
    ih_command c = parse(line);
    assert(c.error == IH_OK && c.kind == IH_OTA && c.ota_op == IH_OTA_BEGIN);
    assert(c.ota_size == IH_OTA_SLOT_SIZE && c.ota_hash[0] == 1 && c.ota_hash[15] == 0xef);
    ih_check_transport(&c, true); assert(c.error == IH_OK);
    ih_check_transport(&c, false); assert(c.error == IH_ARG);
    snprintf(line, sizeof(line), "1 OTA BEGIN 32768 %s BIN", hash);
    c = parse(line); assert(c.error == IH_OK && c.ota_binary && c.ota_size == 32768);
    snprintf(line, sizeof(line), "1 OTA BEGIN 32768 %s BINARY", hash);
    assert(parse(line).error == IH_ARG);
    const char *bad[] = {"1 OTA", "1 OTA END x", "1 OTA ABORT x", "1 OTA DATA",
        "1 OTA DATA A", "1 OTA DATA A===", "1 OTA DATA ====", "1 OTA DATA AA=A",
        "1 OTA DATA AA==AAAA", "1 OTA DATA AB==", "1 OTA DATA AAB=", "1 OTA DATA AA-_",
        "1 OTA DATA AA== x", "1 OTA BEGIN 0 a", "1 OTA begin 1 a", "1 OTA BEGIN 1 a"};
    for (size_t i = 0; i < sizeof(bad)/sizeof(bad[0]); ++i) assert(parse(bad[i]).error == IH_ARG);
    snprintf(line, sizeof(line), "1 OTA BEGIN 2031617 %s", hash); assert(parse(line).error == IH_ARG);
    snprintf(line, sizeof(line), "1 OTA BEGIN 1 %s", hash); line[strlen(line)-1] = 'g';
    assert(parse(line).error == IH_ARG);
    c = parse("1 OTA DATA AA=="); assert(c.error == IH_OK && c.ota_length == 1 && c.ota_data[0] == 0);
    c = parse("1 OTA DATA AP8="); assert(c.error == IH_OK && c.ota_length == 2 && c.ota_data[1] == 255);
    /* Decoded LF/CR/NUL must stay payload; they cannot form injected commands. */
    c = parse("1 OTA DATA AAoN"); assert(c.error == IH_OK && c.ota_length == 3);
    assert(c.ota_data[0] == 0 && c.ota_data[1] == 10 && c.ota_data[2] == 13);
    strcpy(line, "999999 OTA DATA "); memset(line+16, 'A', 224); line[240] = 0;
    c = parse(line); assert(c.error == IH_OK && c.ota_length == IH_OTA_CHUNK_MAX);
    memset(line+16, 'A', 228); line[244] = 0; assert(parse(line).error == IH_ARG);
    ih_stream stream = {0}; unsigned replies = 0;
    const char *frames = "1 OTA DATA AAoN\r\n2 PING\n3 OTA END\n4 OTA ABORT\n";
    for (size_t i = 0; frames[i]; ++i) if (ih_feed(&stream, (unsigned char)frames[i], &c)) {
        ++replies; assert(c.error == IH_OK && c.id == replies);
        if (replies == 2) assert(c.kind == IH_PING);
    }
    assert(replies == 4);
    strcpy(line, "5 OTA DATA "); memset(line+11, 'A', 270); line[281] = '\n'; line[282] = 0;
    for (size_t i = 0; line[i]; ++i) if (ih_feed(&stream, (unsigned char)line[i], &c))
        assert(c.error == IH_PARSE && c.id == 5);
    for (const char *p = "6 PING\n"; *p; ++p) if (ih_feed(&stream, (unsigned char)*p, &c))
        assert(c.error == IH_OK && c.id == 6);
    puts("OTA parser/framing tests passed");
}
