#ifndef IH_TEST_WIFI_H
#define IH_TEST_WIFI_H
#include "runtime.h"
typedef enum { WIFI_PS_NONE, WIFI_PS_MIN_MODEM, WIFI_PS_MAX_MODEM } wifi_ps_type_t;
esp_err_t esp_wifi_get_ps(wifi_ps_type_t *ps);
esp_err_t esp_wifi_set_ps(wifi_ps_type_t ps);
#endif
