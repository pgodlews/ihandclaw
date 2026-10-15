#ifndef IH_TEST_OTA_OPS_H
#define IH_TEST_OTA_OPS_H
#include "runtime.h"
#include "esp_app_desc.h"
typedef struct { size_t size; char label[16]; } esp_partition_t;
typedef uint32_t esp_ota_handle_t;
typedef enum { ESP_OTA_IMG_VALID, ESP_OTA_IMG_PENDING_VERIFY, ESP_OTA_IMG_ABORTED, ESP_OTA_IMG_UNDEFINED } esp_ota_img_states_t;
#define ESP_ERR_OTA_VALIDATE_FAILED 3
#define OTA_WITH_SEQUENTIAL_WRITES 0xfffffffe
const esp_partition_t *esp_ota_get_running_partition(void);
const esp_partition_t *esp_ota_get_next_update_partition(const esp_partition_t *p);
esp_err_t esp_ota_get_state_partition(const esp_partition_t *p, esp_ota_img_states_t *state);
esp_err_t esp_ota_begin(const esp_partition_t *p, size_t size, esp_ota_handle_t *handle);
esp_err_t esp_ota_write(esp_ota_handle_t handle, const void *data, size_t size);
esp_err_t esp_ota_abort(esp_ota_handle_t handle);
esp_err_t esp_ota_end(esp_ota_handle_t handle);
esp_err_t esp_ota_get_partition_description(const esp_partition_t *p, esp_app_desc_t *desc);
esp_err_t esp_ota_set_boot_partition(const esp_partition_t *p);
esp_err_t esp_ota_mark_app_valid_cancel_rollback(void);
#endif
