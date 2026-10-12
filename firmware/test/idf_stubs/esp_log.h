#ifndef IH_TEST_ESP_LOG_H
#define IH_TEST_ESP_LOG_H
#include <stdio.h>
#include <stdlib.h>
/* Host-only shim for testing the installed IDF HID descriptor parser. */
#define ESP_LOGE(tag, ...) do { (void)(tag); fprintf(stderr, __VA_ARGS__); fputc('\n', stderr); } while (0)
#endif
