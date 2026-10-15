#ifndef IH_TEST_APP_DESC_H
#define IH_TEST_APP_DESC_H
#include "runtime.h"
typedef struct { char version[32], project_name[32]; uint8_t app_elf_sha256[32]; } esp_app_desc_t;
const esp_app_desc_t *esp_app_get_description(void);
int esp_app_get_elf_sha256(char *dst, size_t size);
#endif
