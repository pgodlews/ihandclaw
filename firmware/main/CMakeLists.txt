idf_component_register(SRCS "main.c" "ble.c" "wifi_link.c" "ota.c" "ota_wire.c" "../protocol/protocol.c"
    INCLUDE_DIRS "." "../protocol"
    REQUIRES esp_hid bt esp_timer esp_system nvs_flash esp_driver_usb_serial_jtag esp_wifi esp_event esp_netif lwip app_update mbedtls)
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
