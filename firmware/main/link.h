#ifndef IPHONE_HAND_LINK_H
#define IPHONE_HAND_LINK_H
#include "protocol.h"
#define IH_OUTPUT_MAX 512
typedef struct ih_ota_frame ih_ota_frame;
typedef struct { ih_ota_frame *frame; ih_command command; uint32_t session; bool tcp; uint32_t cancel_epoch; } ih_request;
/* STOP bypasses the FIFO. Overload replies BUSY without pausing ingress. */
void ih_submit(ih_request *request);
void ih_wifi_init(void);
ih_error ih_wifi_provision(const ih_command *command);
void ih_wifi_status(bool *up, char *ip, size_t size);
uint32_t ih_wifi_session(void);
/* Nonblocking; a slow TCP consumer is disconnected rather than blocking BLE. */
bool ih_wifi_output(uint32_t session, const char *line);
#endif
