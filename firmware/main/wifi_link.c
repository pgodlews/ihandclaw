/* IDF API/source references and scheduling choices are in ../README.md. */
#include "link.h"
#include "ble.h"
#include "ota_wire.h"
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdatomic.h>
#include <string.h>
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "soc/soc_caps.h"
#include "nvs.h"
#include "lwip/sockets.h"
#include "lwip/tcp.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"

#define TCP_PORT 7878
#define AUTH_MS 5000
#define SEND_MS 2000
#define POLL_MS 5

typedef struct { char ssid[33], password[65]; } credentials;
typedef struct { uint32_t session; char line[IH_OUTPUT_MAX + 1]; } ih_tcp_output;
static SemaphoreHandle_t lock;
static QueueHandle_t tx;
static credentials desired;
static char auth_token[65], address[16] = "-";
static unsigned revision, token_revision;
static bool up, retry;
static _Atomic uint32_t session, slow_session;

static void take(void) { xSemaphoreTake(lock, portMAX_DELAY); }
static void give(void) { xSemaphoreGive(lock); }
uint32_t ih_wifi_session(void)
{
    return atomic_load(&session);
}
void ih_wifi_status(bool *connected, char *ip, size_t size)
{
    take(); *connected = up; snprintf(ip, size, "%s", address); give();
}
bool ih_wifi_output(uint32_t origin, const char *line)
{
    if (!origin) return true; /* Session publication follows queue initialization. */
    if (!tx) return true;
    ih_tcp_output message = {.session = origin};
    snprintf(message.line, sizeof(message.line), "%s\n", line);
    if (xQueueSend(tx, &message, 0) != pdTRUE) {
        atomic_store(&slow_session, origin);
        return false;
    }
    return true;
}
static void event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    take();
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        up = false; strcpy(address, "-"); retry = true;
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *got = data;
        up = true; retry = false;
        snprintf(address, sizeof(address), IPSTR, IP2STR(&got->ip_info.ip));
    } else if (base == IP_EVENT && id == IP_EVENT_STA_LOST_IP) {
        up = false; strcpy(address, "-"); retry = true;
    }
    give();
}
ih_error ih_wifi_provision(const ih_command *c)
{
    nvs_handle_t handle;
    if (nvs_open("link", NVS_READWRITE, &handle) != ESP_OK) return IH_INTERNAL;
    credentials next = {0};
    esp_err_t err;
    if (c->kind == IH_TOKEN) err = nvs_set_str(handle, "token", c->token);
    else {
        if (!c->wifi_off) {
            strcpy(next.ssid, c->ssid); strcpy(next.password, c->password);
        }
        /* A single blob makes SSID/password replacement atomic at commit. */
        err = nvs_set_blob(handle, "wifi", &next, sizeof(next));
    }
    if (err == ESP_OK) err = nvs_commit(handle);
    nvs_close(handle);
    if (err == ESP_OK) {
        take();
        if (c->kind == IH_TOKEN) { strcpy(auth_token, c->token); ++token_revision; }
        else { desired = next; ++revision; up = false; strcpy(address, "-"); }
        give();
    }
    memset(&next, 0, sizeof(next));
    return err == ESP_OK ? IH_OK : IH_INTERNAL;
}
static bool nonblocking(int fd)
{
    int flags = fcntl(fd, F_GETFL, 0);
    return flags >= 0 && fcntl(fd, F_SETFL, flags | O_NONBLOCK) == 0;
}
static void drop(int *fd)
{
    atomic_store(&session, 0);
    if (*fd >= 0) { shutdown(*fd, SHUT_RDWR); close(*fd); *fd = -1; }
}
static int listen_socket(void)
{
    int fd = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (fd < 0) return -1;
    int yes = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));
    struct sockaddr_in bind_address = {.sin_family = AF_INET, .sin_port = htons(TCP_PORT),
                                      .sin_addr.s_addr = htonl(INADDR_ANY)};
    if (!nonblocking(fd) || bind(fd, (struct sockaddr *)&bind_address, sizeof(bind_address)) < 0 ||
        listen(fd, 1) < 0) { close(fd); return -1; }
    return fd;
}
static bool auth_line(const ih_stream *s, const char *token)
{
    size_t n = s->length;
    if (n && s->data[n - 1] == '\r') --n;
    size_t length = strlen(token);
    if (s->overflow || n != 5 + length || memcmp(s->data, "AUTH ", 5)) return false;
    unsigned difference = 0;
    for (size_t i = 0; i < length; ++i) difference |= (unsigned char)s->data[i + 5] ^ (unsigned char)token[i];
    return difference == 0;
}
static void network_task(void *arg)
{
    (void)arg;
    unsigned applied = 0, applied_token = 0, backoff = 1;
    bool started = false, was_up = false;
    TickType_t attempt_at = 0, auth_at = 0, send_at = 0;
    int listener = -1, client = -1;
    uint32_t generation = 0, active = 0;
    ih_stream stream = {0};
    ih_ota_wire binary = {0};
    ih_tcp_output sending = {0};
    size_t offset = 0;
    for (;;) {
        TickType_t now = xTaskGetTickCount();
        credentials config;
        char token[65];
        take();
        config = desired; strcpy(token, auth_token);
        unsigned rev = revision, token_rev = token_revision;
        bool connected = up, failed = retry;
        retry = false;
        uint32_t slow = atomic_exchange(&slow_session, 0);
        give();
        if (applied != rev) {
            drop(&client); active = 0;
            if (listener >= 0) { close(listener); listener = -1; }
            if (started) {
                esp_err_t err = esp_wifi_stop();
                if (err != ESP_OK) ih_emit("@0 EVT wifi_error code=%d", (int)err);
            }
            started = false;
            take(); up = false; retry = false; strcpy(address, "-"); give();
            connected = failed = was_up = false;
            if (config.ssid[0]) {
                wifi_config_t wifi = {0};
                memcpy(wifi.sta.ssid, config.ssid, strlen(config.ssid));
                memcpy(wifi.sta.password, config.password, strlen(config.password));
                esp_err_t err = esp_wifi_set_config(WIFI_IF_STA, &wifi);
                memset(&wifi, 0, sizeof(wifi));
                if (err == ESP_OK) err = esp_wifi_start();
                if (err == ESP_OK) {
                    started = true;
#if SOC_WIFI_SUPPORT_5G
                    err = esp_wifi_set_band_mode(WIFI_BAND_MODE_AUTO);
#endif
                }
                if (err != ESP_OK) ih_emit("@0 EVT wifi_error code=%d", (int)err);
                attempt_at = now; backoff = 1;
            }
            applied = rev;
        }
        memset(&config, 0, sizeof(config));
        if (failed && started) {
            attempt_at = now + pdMS_TO_TICKS(backoff * 1000);
            backoff = backoff < 16 ? backoff * 2 : 30;
        }
        if (connected && !was_up) backoff = 1;
        was_up = connected;
        if (started && !connected && (int32_t)(now - attempt_at) >= 0) {
            /* Connect only here, never recursively in the event callback. */
            esp_err_t err = esp_wifi_connect();
            attempt_at = now + pdMS_TO_TICKS(30000);
            if (err != ESP_OK) {
                attempt_at = now + pdMS_TO_TICKS(backoff * 1000);
                backoff = backoff < 16 ? backoff * 2 : 30;
            }
        }
        if (applied_token != token_rev || !connected || !token[0] || (active && slow == active)) {
            drop(&client); active = 0;
            if (listener >= 0) { close(listener); listener = -1; }
            applied_token = token_rev;
        }
        if (connected && token[0] && listener < 0) listener = listen_socket();
        if (listener >= 0) {
            int next = accept(listener, NULL, NULL);
            if (next >= 0) {
                drop(&client); active = 0; client = next;
                int yes = 1;
                setsockopt(client, IPPROTO_TCP, TCP_NODELAY, &yes, sizeof(yes));
                if (!nonblocking(client)) drop(&client);
                stream = (ih_stream){0};
                ih_ota_wire_reset(&binary, 0);
                memset(&sending, 0, sizeof(sending)); offset = 0;
                auth_at = now;
            }
        }
        if (client >= 0 && !active && now - auth_at >= pdMS_TO_TICKS(AUTH_MS)) drop(&client);
        /* Keep parsing on overload so an authenticated STOP can always arrive. */
        for (unsigned i = 0; client >= 0 && i < 512; ++i) {
            if (active && binary.total && binary.received < binary.total) {
                void *data;
                size_t room = ih_ota_wire_room(&binary, &data);
                if (binary.failed) { drop(&client); active = 0; break; }
                if (!room) break;
                int count = recv(client, data, room, 0);
                if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) break;
                if (count <= 0) { drop(&client); active = 0; break; }
                ih_ota_frame *frame = ih_ota_wire_commit(&binary, (size_t)count);
                if (binary.failed) { drop(&client); active = 0; break; }
                if (frame) {
                    ih_request request = {.command = {.kind = IH_OTA, .ota_op = IH_OTA_DATA,
                        .id = frame->id}, .session = active, .tcp = true, .frame = frame};
                    ih_submit(&request);
                }
                continue;
            }
            unsigned char byte;
            int n = recv(client, &byte, 1, 0);
            if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) break;
            if (n <= 0) { drop(&client); active = 0; break; }
            if (!active) {
                if (byte != '\n') {
                    if (stream.length < IH_LINE_MAX) stream.data[stream.length++] = (char)byte;
                    else stream.overflow = true;
                    continue;
                }
                if (!auth_line(&stream, token)) { drop(&client); break; }
                active = ++generation;
                if (!active) active = ++generation;
                atomic_store(&session, active);
                stream = (ih_stream){0};
                sending = (ih_tcp_output){.session = active};
                strcpy(sending.line, "@0 OK auth\n"); offset = 0; send_at = now;
            } else {
                ih_command command;
                if (!ih_feed(&stream, byte, &command)) continue;
                ih_check_transport(&command, true);
                if (command.error == IH_OK && command.kind == IH_OTA &&
                    command.ota_op == IH_OTA_BEGIN && command.ota_binary)
                    ih_ota_wire_reset(&binary, command.ota_size);
                ih_request request = {.command = command, .session = active, .tcp = true};
                ih_submit(&request);
                memset(&command, 0, sizeof(command));
                memset(&request, 0, sizeof(request));
            }
        }
        if (client >= 0 && active) {
            if (!sending.line[0]) {
                while (xQueueReceive(tx, &sending, 0) == pdTRUE) {
                    if (sending.session == active) { offset = 0; send_at = now; break; }
                    sending.line[0] = 0;
                }
            }
            if (sending.line[0]) {
                size_t length = strlen(sending.line);
                int n = send(client, sending.line + offset, length - offset, 0);
                if (n > 0) offset += (size_t)n;
                if (offset == length) sending.line[0] = 0;
                else if ((n < 0 && errno != EAGAIN && errno != EWOULDBLOCK) ||
                         now - send_at >= pdMS_TO_TICKS(SEND_MS)) { drop(&client); active = 0; }
            }
        }
        if (client < 0) {
            ih_ota_wire_reset(&binary, 0);
            stream = (ih_stream){0}; sending.line[0] = 0;
            ih_tcp_output stale;
            while (xQueueReceive(tx, &stale, 0) == pdTRUE) { }
        }
        memset(token, 0, sizeof(token));
        vTaskDelay(pdMS_TO_TICKS(POLL_MS));
    }
}
void ih_wifi_init(void)
{
    lock = xSemaphoreCreateMutex(); tx = xQueueCreate(64, sizeof(ih_tcp_output));
    configASSERT(lock && tx);
    nvs_handle_t handle;
    esp_err_t err = nvs_open("link", NVS_READONLY, &handle);
    if (err == ESP_OK) {
        size_t size = sizeof(desired);
        err = nvs_get_blob(handle, "wifi", &desired, &size);
        if (err != ESP_OK || size != sizeof(desired)) memset(&desired, 0, sizeof(desired));
        size = sizeof(auth_token);
        if (nvs_get_str(handle, "token", auth_token, &size) != ESP_OK) auth_token[0] = 0;
        nvs_close(handle);
        /* Stored data originates in the portable parser; terminate defensively. */
        desired.ssid[32] = 0; desired.password[64] = 0; auth_token[64] = 0;
    } else ESP_ERROR_CHECK(err == ESP_ERR_NVS_NOT_FOUND ? ESP_OK : err);
    revision = token_revision = 1;
    /* Driver diagnostics must not disclose provisioned network identifiers. */
    esp_log_level_set("wifi", ESP_LOG_NONE);
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_t *netif = esp_netif_create_default_wifi_sta();
    configASSERT(netif);
    ESP_ERROR_CHECK(esp_netif_set_hostname(netif, "iphone-hand"));
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    /* Keep modem sleep: coexistence still time-slices RF with PS_NONE, and
     * software coexistence force-selects disconnected station power saving. */
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_MIN_MODEM));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, WIFI_EVENT_STA_DISCONNECTED, event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, ESP_EVENT_ANY_ID, event, NULL));
    ESP_ERROR_CHECK(xTaskCreate(network_task, "wifi_link", 6144, NULL, 4, NULL) == pdPASS ? ESP_OK : ESP_ERR_NO_MEM);
}
