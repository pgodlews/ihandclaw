#include "protocol.h"
#include <assert.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>

static unsigned checks;
#define CHECK(expr) do { ++checks; assert(expr); } while (0)
static ih_command parse(const char *s) { return ih_parse(s, strlen(s)); }
static void commands(void)
{
    const struct { const char *text; ih_kind kind; } cases[] = {
        {"1 PING", IH_PING}, {"999999 STATUS", IH_STATUS}, {"2 MOVE -2000 2000", IH_MOVE},
        {"3 CFG step=127 interval=100", IH_CFG}, {"4 BTN 7", IH_BTN},
        {"5 CLICK", IH_CLICK}, {"6 SCROLL -2000 2000", IH_SCROLL},
        {"7 KEY 0x80 0x2c", IH_KEY}, {"8 KREPORT 0 1 2 3 4 5 6", IH_KREPORT},
        {"9 CONSUMER 0x223", IH_CONSUMER}, {"10 UNPAIR", IH_UNPAIR},
        {"11 NAME iPhone Hand", IH_NAME}, {"12 STOP", IH_STOP}, {"13 PAIR", IH_PAIR}
    };
    for (size_t i = 0; i < sizeof(cases)/sizeof(cases[0]); ++i) {
        ih_command c = parse(cases[i].text);
        CHECK(c.error == IH_OK); CHECK(c.kind == cases[i].kind); CHECK(c.id >= 1);
    }
    ih_command c = parse("42 CLICK"); CHECK(c.args[0] == 1); CHECK(c.args[1] == 60);
    c = parse("42 CLICK 3 800"); CHECK(c.args[0] == 3); CHECK(c.args[1] == 800);
    c = parse("42 CLICK 2"); CHECK(c.args[1] == 60);
    c = parse("42 KEY 128 0"); CHECK(c.args[0] == 128); CHECK(c.args[1] == 0); CHECK(c.args[2] == 30);
    c = parse("42 KEY 255 255 0"); CHECK(c.error == IH_OK); CHECK(c.args[2] == 0);
    c = parse("42 KREPORT 0xFF 0x04"); CHECK(c.error == IH_OK); CHECK(c.args[2] == 0); CHECK(c.args[6] == 0);
    c = parse("42 KREPORT 0 0"); CHECK(c.error == IH_OK);
    c = parse("42 CONSUMER 65535 65535"); CHECK(c.error == IH_OK); CHECK(c.args[1] == 65535);
    c = parse("42 CONSUMER 0"); CHECK(c.args[1] == 30);
    c = parse("42 MOVE 2000 -2000"); CHECK(c.error == IH_OK);
    c = parse("42 SCROLL -2000"); CHECK(c.error == IH_OK);
    c = parse("42 SCROLL 999"); CHECK(c.error == IH_OK); CHECK(c.args[1] == 0);
    c = parse("42 CFG step=1"); CHECK(c.error == IH_OK); CHECK(c.has_step); CHECK(!c.has_interval);
    c = parse("42 CFG interval=5"); CHECK(c.error == IH_OK); CHECK(!c.has_step); CHECK(c.has_interval);
    c = parse("42 CFG interval=15 step=8"); CHECK(c.error == IH_OK);
    c = parse(" 42\tPING\r"); CHECK(c.error == IH_OK); CHECK(c.id == 42);
    c = parse("42 NAME Zażółć 🖱"); CHECK(c.error == IH_OK); CHECK(!strcmp(c.name, "Zażółć 🖱"));
    c = parse("42 NAME two  spaces "); CHECK(c.error == IH_OK); CHECK(!strcmp(c.name, "two  spaces "));
    c = parse("42 KEY 00 09"); CHECK(c.error == IH_OK); CHECK(c.args[1] == 9);
}
static void provisioning(void)
{
    ih_command c = parse("42 WIFI lab%20network pass%25word%2bplus");
    CHECK(c.error == IH_OK); CHECK(c.kind == IH_WIFI);
    CHECK(!strcmp(c.ssid, "lab network")); CHECK(!strcmp(c.password, "pass%word+plus"));
    c = parse("42 WIFI off"); CHECK(c.error == IH_OK); CHECK(c.wifi_off);
    c = parse("42 WIFI off password"); CHECK(c.error == IH_OK); CHECK(!c.wifi_off);
    c = parse("42 WIFI %c3%a9 pass+word"); CHECK(c.error == IH_OK);
    CHECK(!strcmp(c.ssid, "é")); CHECK(!strcmp(c.password, "pass+word"));
    const char *bad[] = {"42 WIFI", "42 WIFI OFF", "42 WIFI a b c", "42 WIFI a% b",
        "42 WIFI a%0 b", "42 WIFI a%GG b", "42 WIFI a b%00", "42 WIFI a b%0A",
        "42 WIFI a b%09", "42 WIFI a b%7F", "42 TOKEN", "42 TOKEN xyz"};
    for (size_t i = 0; i < sizeof(bad)/sizeof(bad[0]); ++i) CHECK(parse(bad[i]).error == IH_ARG);
    char line[256];
    for (unsigned n = 31; n <= 65; ++n) {
        strcpy(line, "42 TOKEN "); memset(line + 9, 'a', n); line[9 + n] = 0;
        c = parse(line); CHECK(c.error == (n >= 32 && n <= 64 ? IH_OK : IH_ARG));
        ih_check_transport(&c, true); CHECK(c.error == IH_ARG); CHECK(c.id == 42);
    }
    strcpy(line, "42 TOKEN "); memset(line + 9, 'F', 64); line[73] = 0;
    c = parse(line); CHECK(c.error == IH_OK);
    line[40] = 'g'; CHECK(parse(line).error == IH_ARG);
    for (unsigned n = 32; n <= 33; ++n) {
        strcpy(line, "42 WIFI "); memset(line + 8, 's', n); strcpy(line + 8 + n, " password");
        CHECK(parse(line).error == (n == 32 ? IH_OK : IH_ARG));
    }
    for (unsigned n = 64; n <= 65; ++n) {
        strcpy(line, "42 WIFI ssid "); memset(line + 13, 'p', n); line[13 + n] = 0;
        CHECK(parse(line).error == (n == 64 ? IH_OK : IH_ARG));
    }
    c = parse("42 WIFI off"); ih_check_transport(&c, false); CHECK(c.error == IH_OK);
    ih_check_transport(&c, true); CHECK(c.error == IH_ARG);
    c = parse("42 WIFI a b"); ih_check_transport(&c, true); CHECK(c.error == IH_ARG);
    c = parse("42 PING"); ih_check_transport(&c, true); CHECK(c.error == IH_OK);
    c = parse("42 STOP"); ih_check_transport(&c, true); CHECK(c.error == IH_OK);
    c = parse("42 PAIR"); ih_check_transport(&c, true); CHECK(c.error == IH_OK);
}
static void errors(void)
{
    const char *bad_ids[] = {"", "PING", "0 PING", "1000000 PING", "-1 PING", "+1 PING", "0x1 PING", "1x PING", "999999999999999999999999 PING"};
    for (size_t i = 0; i < sizeof(bad_ids)/sizeof(bad_ids[0]); ++i) {
        ih_command c = parse(bad_ids[i]); CHECK(c.error == IH_PARSE); CHECK(c.id == 0);
    }
    CHECK(parse("42").error == IH_PARSE);
    CHECK(parse("42 ping").error == IH_UNKNOWN);
    CHECK(parse("42 WAT").id == 42);
    const char *bad[] = {
        "42 STOP 1", "42 PAIR 120", "42 MOVE -2001 0", "42 MOVE 0 2001",
        "42 SCROLL -2001", "42 SCROLL 0 2001", "42 SCROLL -2147483648", "42 SCROLL 2147483647",
        "42 PING 1", "42 STATUS x", "42 UNPAIR x", "42 MOVE", "42 MOVE 1", "42 MOVE 1 2 3",
        "42 MOVE -32768 0", "42 MOVE 32768 0", "42 MOVE 0x10 0", "42 MOVE 1.0 2", "42 MOVE 1e2 0",
        "42 CFG", "42 CFG step=0", "42 CFG step=128", "42 CFG interval=4", "42 CFG interval=101",
        "42 CFG step=1 step=2", "42 CFG interval=5 interval=6", "42 CFG nonsense=1", "42 CFG step=0x10",
        "42 BTN", "42 BTN -1", "42 BTN 8", "42 BTN 1 2", "42 CLICK 0", "42 CLICK 8", "42 CLICK 1 -1",
        "42 CLICK 1 65536", "42 CLICK 1 2 3", "42 CLICK mask=1", "42 SCROLL", "42 SCROLL 2147483648",
        "42 SCROLL -2147483649", "42 SCROLL 1 2 3", "42 KEY 0", "42 KEY -1 4", "42 KEY 256 4",
        "42 KEY 0 256", "42 KEY 0 4 65536", "42 KEY 0 4 1 2", "42 KEY 0 0x", "42 KEY 0 0xZZ",
        "42 KEY 0 99999999999999999999999", "42 KREPORT 0", "42 KREPORT 0 1 2 3 4 5 6 7",
        "42 KREPORT 0 256", "42 KREPORT 0 1 2 3 4 5 6 7 8", "42 CONSUMER", "42 CONSUMER -1",
        "42 CONSUMER 65536", "42 CONSUMER 1 65536", "42 CONSUMER 1 2 3", "42 NAME",
        "42 NAME \xc0\xaf", "42 NAME \xed\xa0\x80", "42 NAME \xf4\x90\x80\x80", "42 NAME \xe2\x82",
        "42 NAME \xc2\x80", "42 NAME \xf0\x80\x80\x80", "42 NAME bad\tname"
    };
    for (size_t i = 0; i < sizeof(bad)/sizeof(bad[0]); ++i) {
        ih_command c = parse(bad[i]); CHECK(c.error == IH_ARG); CHECK(c.id == 42);
    }
    CHECK(parse("42 PING\n").error == IH_PARSE);
    CHECK(parse("42 NAME bad\rname").error == IH_PARSE);
    CHECK(ih_parse("42 PING\0junk", 12).error == IH_PARSE);
    for (int i = IH_OK; i <= IH_TIMEOUT; ++i) CHECK(strlen(ih_error_name((ih_error)i)) > 0);
    CHECK(!strcmp(ih_error_name((ih_error)99), "INTERNAL"));
}
static void framing(void)
{
    char line[260];
    memset(line, ' ', sizeof(line));
    memcpy(line, "42 PING", 7);
    CHECK(ih_parse(line, 256).error == IH_OK);
    CHECK(ih_parse(line, 257).error == IH_PARSE);
    CHECK(ih_parse(line, 257).id == 42);
    ih_stream s = {0}; ih_command c;
    for (unsigned i = 0; i < 256; ++i) CHECK(!ih_feed(&s, (unsigned char)line[i], &c));
    CHECK(ih_feed(&s, '\n', &c)); CHECK(c.error == IH_OK);
    for (unsigned i = 0; i < 2000; ++i) CHECK(!ih_feed(&s, (unsigned char)line[i % 256], &c));
    CHECK(ih_feed(&s, '\n', &c)); CHECK(c.error == IH_PARSE); CHECK(c.id == 42);
    const char *next = "43 PING\r\n44 STATUS\n";
    unsigned replies = 0;
    for (const char *p = next; *p; ++p) if (ih_feed(&s, (unsigned char)*p, &c)) { CHECK(c.error == IH_OK); ++replies; }
    CHECK(replies == 2);
    CHECK(ih_feed(&s, '\n', &c)); CHECK(c.error == IH_PARSE);
    const unsigned char nul[] = {'4','2',' ','P','I','N','G',0,'x','\n'};
    for (size_t i = 0; i < sizeof(nul); ++i) if (ih_feed(&s, nul[i], &c)) CHECK(c.error == IH_PARSE);
    memset(line, 'a', sizeof(line)); memcpy(line, "1 NAME ", 7);
    CHECK(ih_parse(line, 255).error == IH_OK); /* 248 name bytes. */
    CHECK(ih_parse(line, 256).error == IH_ARG);
}
static void split(int32_t x, int32_t y, unsigned limit)
{
    int32_t original_x = x, original_y = y;
    int64_t sum_x = 0, sum_y = 0;
    int16_t dx, dy;
    unsigned reports = 0;
    while (ih_next_step(&x, &y, limit, &dx, &dy)) {
        CHECK(dx >= -(int)limit && dx <= (int)limit); CHECK(dy >= -(int)limit && dy <= (int)limit);
        CHECK(dx || dy); sum_x += dx; sum_y += dy; ++reports;
    }
    CHECK(sum_x == original_x); CHECK(sum_y == original_y);
    int64_t ax = original_x < 0 ? -(int64_t)original_x : original_x;
    int64_t ay = original_y < 0 ? -(int64_t)original_y : original_y;
    int64_t largest = ax > ay ? ax : ay;
    CHECK(reports == (largest + limit - 1) / limit);
}
static void splitting(void)
{
    split(0, 0, 8); split(20, -9, 8); split(-2000, 2000, 1); split(2000, -1, 127);
    split(300, -260, 127); split(-128, 128, 127); split(0, -1000, 127);
    /* Boundary step without iterating 17 million SCROLL reports. */
    int32_t x = INT32_MIN, y = INT32_MAX; int16_t dx, dy;
    CHECK(ih_next_step(&x, &y, 127, &dx, &dy)); CHECK(dx == -127); CHECK(dy == 127);
    CHECK(x == INT32_MIN + 127); CHECK(y == INT32_MAX - 127);
    CHECK(!ih_next_step(&x, &y, 0, &dx, &dy)); CHECK(!ih_next_step(&x, &y, 128, &dx, &dy));
    for (unsigned l = 1; l <= 127; ++l) {
        split(513, -255, l); split(-9, 300, l);
    }
}
int main(void)
{
    commands(); provisioning(); errors(); framing(); splitting();
    printf("PASS: %u checks (all commands, rejection, framing, MOVE/SCROLL splitting)\n", checks);
    return 0;
}
