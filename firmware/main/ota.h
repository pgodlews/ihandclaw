#ifndef IPHONE_HAND_OTA_H
#define IPHONE_HAND_OTA_H
#include "link.h"
/* All calls belong to the executor; network task only publishes session state. */
void ih_ota_init(void);
void ih_ota_poll(void);
ih_error ih_ota_execute(const ih_request *request, char *detail, size_t size);
/* Called only after authenticated STATUS got a successful BLE host response. */
ih_error ih_ota_confirm(uint32_t session, bool wifi_up);
bool ih_ota_pending(void);
void ih_ota_status(char *detail, size_t size);
#endif
