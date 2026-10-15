CC = cc
CFLAGS = -std=c11 -O2 -Wall -Wextra -Werror -pedantic
BUILD = ../build/parser-test
.PHONY: all test sanitize clean
all: test
$(BUILD)/test_protocol: test_protocol.c ../protocol/protocol.c ../protocol/protocol.h
	mkdir -p $(BUILD)
	$(CC) $(CFLAGS) -I../protocol test_protocol.c ../protocol/protocol.c -o $@
$(BUILD)/test_runtime $(BUILD)/test_runtime_c3: test_runtime.c ../main/main.c ../main/ble.h ../main/link.h ../main/ota.h ../main/ota_wire.c ../main/ota_wire.h ../protocol/protocol.c $(wildcard runtime_stubs/*.h)
	mkdir -p $(BUILD)
	$(CC) $(CFLAGS) -DCONFIG_IDF_TARGET='"$(if $(filter %_c3,$@),esp32c3,esp32c5)"' -Iruntime_stubs -I../protocol test_runtime.c ../main/ota_wire.c ../protocol/protocol.c -o $@
$(BUILD)/test_ble: test_ble.c ble_runtime.h ../main/ble.c ../main/ble.h ../protocol/protocol.h
	mkdir -p $(BUILD)
	$(CC) $(CFLAGS) -I../protocol test_ble.c -o $@
$(BUILD)/test_ota_parser: test_ota_parser.c ../protocol/protocol.c ../protocol/protocol.h
	mkdir -p $(BUILD)
	$(CC) $(CFLAGS) -I../protocol test_ota_parser.c ../protocol/protocol.c -o $@
$(BUILD)/test_ota: test_ota.c ../main/ota.c ../main/ota.h ../protocol/protocol.h $(wildcard runtime_stubs/*.h runtime_stubs/psa/*.h)
	mkdir -p $(BUILD)
	$(CC) $(CFLAGS) -Iruntime_stubs -I../protocol test_ota.c -o $@
$(BUILD)/test_ota_wire: test_ota_wire.c ../main/ota_wire.c ../main/ota_wire.h
	mkdir -p $(BUILD)
	$(CC) $(CFLAGS) -I../protocol test_ota_wire.c ../main/ota_wire.c -o $@
test: $(BUILD)/test_ota_parser $(BUILD)/test_ota $(BUILD)/test_ota_wire $(BUILD)/test_protocol $(BUILD)/test_runtime $(BUILD)/test_runtime_c3 $(BUILD)/test_ble
	$(BUILD)/test_protocol
	$(BUILD)/test_ota_parser
	$(BUILD)/test_ota
	$(BUILD)/test_ota_wire
	$(BUILD)/test_runtime
	$(BUILD)/test_runtime_c3
	$(BUILD)/test_ble
sanitize:
	mkdir -p $(BUILD)
	$(CC) $(CFLAGS) -g -fsanitize=address,undefined -I../protocol test_ota_wire.c ../main/ota_wire.c -o $(BUILD)/test_ota_wire_sanitize
	$(BUILD)/test_ota_wire_sanitize
	mkdir -p $(BUILD)
	$(CC) $(CFLAGS) -g -fsanitize=address,undefined -I../protocol test_ota_parser.c ../protocol/protocol.c -o $(BUILD)/test_ota_parser_sanitize
	$(BUILD)/test_ota_parser_sanitize
	$(CC) $(CFLAGS) -g -fsanitize=address,undefined -Iruntime_stubs -I../protocol test_ota.c -o $(BUILD)/test_ota_sanitize
	$(BUILD)/test_ota_sanitize
	$(CC) $(CFLAGS) -g -fsanitize=address,undefined -I../protocol test_protocol.c ../protocol/protocol.c -o $(BUILD)/test_protocol_sanitize
	$(BUILD)/test_protocol_sanitize
	$(CC) $(CFLAGS) -DCONFIG_IDF_TARGET='"esp32c5"' -g -fsanitize=address,undefined -Iruntime_stubs -I../protocol test_runtime.c ../main/ota_wire.c ../protocol/protocol.c -o $(BUILD)/test_runtime_sanitize
	$(BUILD)/test_runtime_sanitize
	$(CC) $(CFLAGS) -g -fsanitize=address,undefined -I../protocol test_ble.c -o $(BUILD)/test_ble_sanitize
	$(BUILD)/test_ble_sanitize
clean:
	rm -rf $(BUILD)

# Optional integration check using the installed IDF source, without hardware.
.PHONY: check-map
check-map:
	test -n "$(IDF_PATH)"
	mkdir -p $(BUILD)
	$(CC) -std=c11 -O2 -Wall -Wextra -Wno-format -Wno-unused-parameter -Iidf_stubs -I"$(IDF_PATH)/components/esp_hid/include" -c "$(IDF_PATH)/components/esp_hid/src/esp_hid_common.c" -o $(BUILD)/idf_hid_parser.o
	$(CC) $(CFLAGS) -I../main -I"$(IDF_PATH)/components/esp_hid/include" test_report_map.c $(BUILD)/idf_hid_parser.o -o $(BUILD)/test_report_map
	$(BUILD)/test_report_map
