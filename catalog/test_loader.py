"""Tests for catalog/loader.py and YAML schema integrity.

Host-independent test suite compatible with pytest and unittest.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from catalog.loader import (
    AppEntry,
    Catalog,
    CatalogError,
    SettingEntry,
    ValidationError,
    load_apps,
    load_catalog,
    load_settings,
)


class CatalogLoaderTests(unittest.TestCase):
    """Test suite for catalog loading, querying, and schema validation."""

    def setUp(self) -> None:
        self.catalog_dir = Path(__file__).parent
        self.catalog = load_catalog(self.catalog_dir)

    def test_load_real_catalog(self) -> None:
        """Verify real YAML files load without error and return populated catalog."""
        self.assertIsInstance(self.catalog, Catalog)
        self.assertGreater(len(self.catalog.apps), 25, "Expected at least 25 stock iOS apps")
        self.assertGreater(len(self.catalog.settings), 15, "Expected at least 15 settings destinations")

    def test_required_apps_from_brief_present(self) -> None:
        """Verify core stock apps referenced in the specification are present."""
        expected_apps = [
            "settings",
            "calculator",
            "camera",
            "clock",
            "calendar",
            "notes",
            "safari",
            "photos",
            "messages",
            "reminders",
            "shortcuts",
            "phone",
            "maps",
            "mail",
            "weather",
            "passwords",
        ]
        for app_id in expected_apps:
            app = self.catalog.get_app(app_id)
            self.assertIsNotNone(app, f"Expected app '{app_id}' to be present in catalog")
            self.assertTrue(len(app.first_screen_landmarks) > 0)
            self.assertTrue(app.status.startswith(("read", "measured", "estimated", "unknown - verify on device")))

    def test_required_settings_from_brief_present(self) -> None:
        """Verify all settings destinations explicitly named in the brief are present."""
        expected_settings = [
            "wifi",
            "bluetooth",
            "display_and_brightness",
            "dark_mode",
            "assistivetouch",
            "voice_control",
            "hardware_keyboard",
            "auto_lock",
            "screen_time",
            "general_about",
            "battery",
        ]
        for setting_id in expected_settings:
            setting = self.catalog.get_setting(setting_id)
            self.assertIsNotNone(setting, f"Expected setting '{setting_id}' to be present in catalog")
            self.assertTrue(len(setting.landmark_texts) > 0)
            self.assertEqual(setting.navigation_path[0], "Settings")

    def test_settings_spotlight_direct_rule(self) -> None:
        """Brief Rule: whether Spotlight jumps there directly must be 'read' with Apple URL or 'unknown - verify on device'."""
        for setting_id, setting in self.catalog.settings.items():
            status = setting.spotlight_direct_status.lower()
            if status.startswith("read"):
                self.assertIsNotNone(
                    setting.spotlight_source_url,
                    f"Setting '{setting_id}' marked as 'read' must have an Apple source URL in spotlight_source_url",
                )
                self.assertTrue(
                    setting.spotlight_source_url.startswith("https://support.apple.com"),
                    f"Setting '{setting_id}' URL must be an official Apple Support URL: {setting.spotlight_source_url}",
                )
            else:
                self.assertTrue(
                    status.startswith("unknown - verify on device"),
                    f"Setting '{setting_id}' spotlight_direct_status must be 'read' or 'unknown - verify on device', got {status!r}",
                )

    def test_status_claims_labeled(self) -> None:
        """Brief Rule: label every claim measured / read / estimated / unknown - verify on device."""
        valid_prefixes = ("read", "measured", "estimated", "unknown - verify on device")
        for app in self.catalog.apps.values():
            self.assertTrue(
                app.status.lower().startswith(valid_prefixes),
                f"App '{app.id}' has invalid status: {app.status!r}",
            )
        for setting in self.catalog.settings.values():
            self.assertTrue(
                setting.status.lower().startswith(valid_prefixes),
                f"Setting '{setting.id}' has invalid status: {setting.status!r}",
            )

    def test_shortcuts_md_exists_and_contains_10_actions(self) -> None:
        """Verify catalog/shortcuts.md exists and covers all 10 proposed shortcuts from the brief."""
        shortcuts_path = self.catalog_dir / "shortcuts.md"
        self.assertTrue(shortcuts_path.is_file(), "catalog/shortcuts.md must exist")
        content = shortcuts_path.read_text(encoding="utf-8")

        required_shortcuts = [
            "Toggle Dark Mode",
            "Toggle Low Power Mode",
            "Toggle Do Not Disturb",
            "Open URL in Safari",
            "Set Timer 5 Min",
            "Toggle AssistiveTouch",
            "Toggle Voice Control",
            "Toggle Wi-Fi",
            "Toggle Bluetooth",
            "Mute Volume",
        ]
        for shortcut in required_shortcuts:
            self.assertIn(shortcut, content, f"shortcuts.md must document shortcut '{shortcut}'")

    def test_app_query_helpers(self) -> None:
        """Verify App query and fuzzy lookup methods on Catalog."""
        # By ID
        app = self.catalog.get_app("settings")
        self.assertIsNotNone(app)
        self.assertEqual(app.id, "settings")

        # By Display Name (case-insensitive)
        app_name = self.catalog.get_app("App Store")
        self.assertIsNotNone(app_name)
        self.assertEqual(app_name.id, "app_store")

        # By Landmark
        results = self.catalog.find_apps_by_landmark("Airplane Mode")
        self.assertTrue(any(a.id == "settings" for a in results))

    def test_settings_query_helpers(self) -> None:
        """Verify Setting query and lookup methods on Catalog."""
        # By ID
        setting = self.catalog.get_setting("wifi")
        self.assertIsNotNone(setting)
        self.assertEqual(setting.id, "wifi")

        # By Display Name (case-insensitive)
        setting_name = self.catalog.get_setting("battery")
        self.assertIsNotNone(setting_name)
        self.assertEqual(setting_name.id, "battery")

        # By Landmark
        results = self.catalog.find_settings_by_landmark("Low Power Mode")
        self.assertTrue(any(s.id == "battery" for s in results))

    def test_search_spotlight_simulation(self) -> None:
        """Verify Spotlight search simulation ordering."""
        results = self.catalog.search_spotlight("Wi-Fi")
        self.assertGreater(len(results), 0)
        # First result should be exact or prefix match
        first = results[0]
        self.assertIn("wi-fi", (first.spotlight_query.lower(), first.display_name.lower()))

    def test_validation_rejects_invalid_app(self) -> None:
        """Verify schema validator detects malformed app entries."""
        # Missing id
        with self.assertRaises(ValidationError):
            AppEntry.from_dict({"display_name": "Test"})

        # Missing landmarks
        with self.assertRaises(ValidationError):
            AppEntry.from_dict({
                "id": "test",
                "display_name": "Test",
                "spotlight_query": "Test",
                "first_screen_landmarks": [],
                "status": "read",
            })

        # Invalid status prefix
        with self.assertRaises(ValidationError):
            AppEntry.from_dict({
                "id": "test",
                "display_name": "Test",
                "spotlight_query": "Test",
                "first_screen_landmarks": ["Test"],
                "status": "guessed",
            })

        # Invalid URL
        with self.assertRaises(ValidationError):
            AppEntry.from_dict({
                "id": "test",
                "display_name": "Test",
                "spotlight_query": "Test",
                "first_screen_landmarks": ["Test"],
                "status": "read",
                "source": "not-a-valid-url",
            })

    def test_validation_rejects_invalid_setting(self) -> None:
        """Verify schema validator detects malformed setting entries."""
        # Navigation path not starting with 'Settings'
        with self.assertRaises(ValidationError):
            SettingEntry.from_dict({
                "id": "test",
                "display_name": "Test",
                "navigation_path": ["General", "About"],
                "spotlight_query": "About",
                "spotlight_jumps_directly: true": True,
                "spotlight_direct_status": "unknown - verify on device",
                "landmark_texts": ["About"],
                "status": "read",
            })

        # 'read' direct status without source URL
        with self.assertRaises(ValidationError):
            SettingEntry.from_dict({
                "id": "test",
                "display_name": "Test",
                "navigation_path": ["Settings", "Test"],
                "spotlight_query": "Test",
                "spotlight_jumps_directly": True,
                "spotlight_direct_status": "read",
                "spotlight_source_url": None,
                "landmark_texts": ["Test"],
                "status": "read",
            })


if __name__ == "__main__":
    unittest.main()
