#include <stdbool.h>
#include <assert.h>
#include <stdio.h>
#include "esp_hid_common.h"
#include "report_map.h"
int main(void)
{
    esp_hid_report_map_t *map = esp_hid_parse_report_map(ih_report_map, sizeof(ih_report_map));
    assert(map && map->reports_len == 7);
    unsigned inputs = 0, outputs = 0, boots = 0;
    for (unsigned i = 0; i < map->reports_len; ++i) {
        const esp_hid_report_item_t *r = &map->reports[i];
        if (r->protocol_mode == ESP_HID_PROTOCOL_MODE_BOOT) { ++boots; continue; }
        if (r->report_type == ESP_HID_REPORT_TYPE_OUTPUT) {
            assert(r->report_id == 1 && r->value_len == 1); ++outputs; continue;
        }
        assert(r->report_type == ESP_HID_REPORT_TYPE_INPUT);
        assert(r->report_id >= 1 && r->report_id <= 3);
        unsigned expected[] = {0, 8, 7, 2};
        assert(r->value_len == expected[r->report_id]);
        inputs |= 1u << r->report_id;
    }
    assert(inputs == 14 && outputs == 1 && boots == 3);
    esp_hid_free_report_map(map);
    printf("PASS: installed IDF parses keyboard 8+1, mouse 7, consumer 2 bytes; 3 boot entries\n");
    return 0;
}
