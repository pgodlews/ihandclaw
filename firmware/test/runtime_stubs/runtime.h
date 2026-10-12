#ifndef IH_RUNTIME_STUBS_H
#define IH_RUNTIME_STUBS_H
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdarg.h>
typedef int esp_err_t;
#define ESP_OK 0
#define ESP_ERR_INVALID_STATE 1
#define ESP_ERR_NO_MEM 2
#define ESP_ERROR_CHECK(x) assert((x) == ESP_OK)
#define configASSERT(x) assert(x)
#define pdTRUE 1
#define pdPASS 1
#define portMAX_DELAY UINT32_MAX
#define pdMS_TO_TICKS(x) (x)
typedef uint32_t TickType_t;
typedef int BaseType_t;
typedef struct test_queue *QueueHandle_t;
typedef void *TaskHandle_t;
QueueHandle_t xQueueCreate(unsigned capacity, size_t item_size);
BaseType_t xQueueSend(QueueHandle_t queue, const void *item, TickType_t timeout);
BaseType_t xQueueReceive(QueueHandle_t queue, void *item, TickType_t timeout);
BaseType_t xTaskCreate(void (*fn)(void *), const char *name, unsigned stack,
                      void *arg, unsigned priority, TaskHandle_t *handle);
void xTaskNotifyGive(TaskHandle_t handle);
uint32_t ulTaskNotifyTake(BaseType_t clear, TickType_t wait);
int64_t esp_timer_get_time(void);
typedef enum { ESP_RST_POWERON, ESP_RST_SW, ESP_RST_PANIC, ESP_RST_INT_WDT,
               ESP_RST_TASK_WDT, ESP_RST_WDT, ESP_RST_BROWNOUT, ESP_RST_DEEPSLEEP, ESP_RST_USB } esp_reset_reason_t;
esp_reset_reason_t esp_reset_reason(void);
void esp_restart(void);
typedef struct { unsigned timeout_ms, idle_core_mask; bool trigger_panic; } esp_task_wdt_config_t;
esp_err_t esp_task_wdt_reconfigure(const esp_task_wdt_config_t *config);
esp_err_t esp_task_wdt_init(const esp_task_wdt_config_t *config);
esp_err_t esp_task_wdt_add(TaskHandle_t handle);
esp_err_t esp_task_wdt_reset(void);
esp_err_t nvs_flash_init(void);
void esp_log_set_vprintf(int (*fn)(const char *, va_list));
typedef struct { unsigned rx_buffer_size, tx_buffer_size; } usb_serial_jtag_driver_config_t;
#define USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT() ((usb_serial_jtag_driver_config_t){256, 256})
esp_err_t usb_serial_jtag_driver_install(const usb_serial_jtag_driver_config_t *config);
bool usb_serial_jtag_is_connected(void);
int usb_serial_jtag_write_bytes(const void *data, size_t length, uint32_t timeout);
int usb_serial_jtag_read_bytes(void *data, size_t length, uint32_t timeout);
#endif
