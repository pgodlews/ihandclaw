"""Host-independent catalog loader and schema validator for iOS navigation data.

This module provides data structures and strict schema validation for:
  - catalog/apps.yaml (stock iOS 18 apps and first-screen OCR landmarks)
  - catalog/settings.yaml (Settings navigation paths and Spotlight direct destinations)

Designed to run standalone without dependencies on the host/ package.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import urlparse

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]


# Allowed claim status labels according to project rules
VALID_STATUS_PREFIXES: Tuple[str, ...] = (
    "read",
    "measured",
    "estimated",
    "unknown - verify on device",
)


class CatalogError(Exception):
    """Base exception for catalog parsing and validation errors."""


class ValidationError(CatalogError):
    """Raised when catalog data fails schema validation rules."""


def _validate_status_label(status: str, context: str) -> None:
    """Validate that status begins with an approved claim classification."""
    if not isinstance(status, str) or not status.strip():
        raise ValidationError(f"{context}: status must be a non-empty string, got {status!r}")
    normalized = status.strip().lower()
    if not any(normalized == prefix or normalized.startswith(prefix) for prefix in VALID_STATUS_PREFIXES):
        raise ValidationError(
            f"{context}: status {status!r} must start with one of {VALID_STATUS_PREFIXES}"
        )


def _validate_url(url: Optional[str], context: str) -> None:
    """Validate that a non-null URL has a valid http/https scheme and netloc."""
    if url is None:
        return
    if not isinstance(url, str) or not url.strip():
        raise ValidationError(f"{context}: URL must be a non-empty string or null, got {url!r}")
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValidationError(f"{context}: invalid URL format {url!r}")


@dataclass(frozen=True)
class AppEntry:
    """Represents a stock iOS app entry in the catalog."""

    id: str
    display_name: str
    spotlight_query: str
    first_screen_landmarks: List[str]
    status: str
    bundle_id: Optional[str] = None
    source: Optional[str] = None
    first_screen_notes: Optional[str] = None
    verification_notes: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> AppEntry:
        """Construct and validate an AppEntry from a raw dictionary."""
        if not isinstance(data, dict):
            raise ValidationError(f"App entry must be a dictionary, got {type(data).__name__}")

        raw_id = data.get("id")
        if not isinstance(raw_id, str) or not raw_id.strip():
            raise ValidationError(f"App missing valid string 'id': {data}")
        app_id = raw_id.strip()
        ctx = f"App '{app_id}'"

        display_name = data.get("display_name")
        if not isinstance(display_name, str) or not display_name.strip():
            raise ValidationError(f"{ctx}: 'display_name' must be a non-empty string")

        spotlight_query = data.get("spotlight_query")
        if not isinstance(spotlight_query, str) or not spotlight_query.strip():
            raise ValidationError(f"{ctx}: 'spotlight_query' must be a non-empty string")

        landmarks = data.get("first_screen_landmarks")
        if not isinstance(landmarks, list) or len(landmarks) == 0:
            raise ValidationError(f"{ctx}: 'first_screen_landmarks' must be a non-empty list of strings")
        cleaned_landmarks: List[str] = []
        for idx, landmark in enumerate(landmarks):
            if not isinstance(landmark, str) or not landmark.strip():
                raise ValidationError(f"{ctx}: landmark #{idx} must be a non-empty string")
            cleaned_landmarks.append(landmark.strip())

        status = data.get("status")
        if not isinstance(status, str):
            raise ValidationError(f"{ctx}: 'status' is required and must be a string")
        _validate_status_label(status, ctx)

        source = data.get("source")
        if source is not None:
            _validate_url(source, f"{ctx} source")

        bundle_id = data.get("bundle_id")
        if bundle_id is not None and not isinstance(bundle_id, str):
            raise ValidationError(f"{ctx}: 'bundle_id' must be a string if provided")

        first_screen_notes = data.get("first_screen_notes")
        if first_screen_notes is not None and not isinstance(first_screen_notes, str):
            raise ValidationError(f"{ctx}: 'first_screen_notes' must be a string if provided")

        verification_notes = data.get("verification_notes")
        if verification_notes is not None and not isinstance(verification_notes, str):
            raise ValidationError(f"{ctx}: 'verification_notes' must be a string if provided")

        return cls(
            id=app_id,
            display_name=display_name.strip(),
            spotlight_query=spotlight_query.strip(),
            first_screen_landmarks=cleaned_landmarks,
            status=status.strip(),
            bundle_id=bundle_id.strip() if bundle_id else None,
            source=source.strip() if source else None,
            first_screen_notes=first_screen_notes.strip() if first_screen_notes else None,
            verification_notes=verification_notes.strip() if verification_notes else None,
        )


@dataclass(frozen=True)
class SettingEntry:
    """Represents a common Settings destination in the catalog."""

    id: str
    display_name: str
    navigation_path: List[str]
    spotlight_query: str
    spotlight_jumps_directly: Optional[bool]
    spotlight_direct_status: str
    landmark_texts: List[str]
    status: str
    spotlight_source_url: Optional[str] = None
    notes: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SettingEntry:
        """Construct and validate a SettingEntry from a raw dictionary."""
        if not isinstance(data, dict):
            raise ValidationError(f"Setting entry must be a dictionary, got {type(data).__name__}")

        raw_id = data.get("id")
        if not isinstance(raw_id, str) or not raw_id.strip():
            raise ValidationError(f"Setting missing valid string 'id': {data}")
        setting_id = raw_id.strip()
        ctx = f"Setting '{setting_id}'"

        display_name = data.get("display_name")
        if not isinstance(display_name, str) or not display_name.strip():
            raise ValidationError(f"{ctx}: 'display_name' must be a non-empty string")

        nav_path = data.get("navigation_path")
        if not isinstance(nav_path, list) or len(nav_path) < 2:
            raise ValidationError(f"{ctx}: 'navigation_path' must be a list with at least 2 steps")
        cleaned_nav: List[str] = []
        for idx, step in enumerate(nav_path):
            if not isinstance(step, str) or not step.strip():
                raise ValidationError(f"{ctx}: navigation step #{idx} must be a non-empty string")
            cleaned_nav.append(step.strip())
        if cleaned_nav[0] != "Settings":
            raise ValidationError(f"{ctx}: 'navigation_path' must start with 'Settings', got {cleaned_nav[0]!r}")

        spotlight_query = data.get("spotlight_query")
        if not isinstance(spotlight_query, str) or not spotlight_query.strip():
            raise ValidationError(f"{ctx}: 'spotlight_query' must be a non-empty string")

        spotlight_jumps_directly = data.get("spotlight_jumps_directly")
        if spotlight_jumps_directly is not None and not isinstance(spotlight_jumps_directly, bool):
            raise ValidationError(
                f"{ctx}: 'spotlight_jumps_directly' must be boolean or null, got {spotlight_jumps_directly!r}"
            )

        spotlight_direct_status = data.get("spotlight_direct_status")
        if not isinstance(spotlight_direct_status, str):
            raise ValidationError(f"{ctx}: 'spotlight_direct_status' must be a string")
        _validate_status_label(spotlight_direct_status, f"{ctx} spotlight_direct_status")

        spotlight_source_url = data.get("spotlight_source_url")
        if spotlight_source_url is not None:
            _validate_url(spotlight_source_url, f"{ctx} spotlight_source_url")

        # Specific brief rule: if spotlight jumps directly is marked as 'read', must cite an Apple source URL
        norm_direct_status = spotlight_direct_status.strip().lower()
        if norm_direct_status.startswith("read") and not spotlight_source_url:
            raise ValidationError(
                f"{ctx}: when spotlight_direct_status is marked 'read', an Apple source URL must be provided in 'spotlight_source_url'"
            )

        landmarks = data.get("landmark_texts")
        if not isinstance(landmarks, list) or len(landmarks) == 0:
            raise ValidationError(f"{ctx}: 'landmark_texts' must be a non-empty list of strings")
        cleaned_landmarks: List[str] = []
        for idx, landmark in enumerate(landmarks):
            if not isinstance(landmark, str) or not landmark.strip():
                raise ValidationError(f"{ctx}: landmark #{idx} must be a non-empty string")
            cleaned_landmarks.append(landmark.strip())

        status = data.get("status")
        if not isinstance(status, str):
            raise ValidationError(f"{ctx}: 'status' is required and must be a string")
        _validate_status_label(status, ctx)

        notes = data.get("notes")
        if notes is not None and not isinstance(notes, str):
            raise ValidationError(f"{ctx}: 'notes' must be a string if provided")

        return cls(
            id=setting_id,
            display_name=display_name.strip(),
            navigation_path=cleaned_nav,
            spotlight_query=spotlight_query.strip(),
            spotlight_jumps_directly=spotlight_jumps_directly,
            spotlight_direct_status=spotlight_direct_status.strip(),
            spotlight_source_url=spotlight_source_url.strip() if spotlight_source_url else None,
            landmark_texts=cleaned_landmarks,
            status=status.strip(),
            notes=notes.strip() if notes else None,
        )


@dataclass
class Catalog:
    """Full iOS navigation catalog containing apps and settings destinations."""

    apps: Dict[str, AppEntry] = field(default_factory=dict)
    settings: Dict[str, SettingEntry] = field(default_factory=dict)
    version: str = "1.0.0"
    os_version: str = "iOS 18"
    locale: str = "en_US"

    def get_app(self, identifier: str) -> Optional[AppEntry]:
        """Look up an app by exact ID or case-insensitive display name."""
        if identifier in self.apps:
            return self.apps[identifier]
        lowered = identifier.lower().strip()
        for app in self.apps.values():
            if app.id.lower() == lowered or app.display_name.lower() == lowered:
                return app
        return None

    def get_setting(self, identifier: str) -> Optional[SettingEntry]:
        """Look up a setting by exact ID or case-insensitive display name."""
        if identifier in self.settings:
            return self.settings[identifier]
        lowered = identifier.lower().strip()
        for setting in self.settings.values():
            if setting.id.lower() == lowered or setting.display_name.lower() == lowered:
                return setting
        return None

    def find_apps_by_landmark(self, text: str) -> List[AppEntry]:
        """Find apps that declare a specific landmark text (case-insensitive substring)."""
        target = text.lower().strip()
        matches: List[AppEntry] = []
        for app in self.apps.values():
            if any(target in landmark.lower() for landmark in app.first_screen_landmarks):
                matches.append(app)
        return matches

    def find_settings_by_landmark(self, text: str) -> List[SettingEntry]:
        """Find settings destinations that declare a specific landmark text (case-insensitive substring)."""
        target = text.lower().strip()
        matches: List[SettingEntry] = []
        for setting in self.settings.values():
            if any(target in landmark.lower() for landmark in setting.landmark_texts):
                matches.append(setting)
        return matches

    def search_spotlight(self, query: str) -> List[Union[AppEntry, SettingEntry]]:
        """Simulate Spotlight query resolution, ranking exact matches first."""
        target = query.lower().strip()
        exact_matches: List[Union[AppEntry, SettingEntry]] = []
        prefix_matches: List[Union[AppEntry, SettingEntry]] = []
        substr_matches: List[Union[AppEntry, SettingEntry]] = []

        combined: List[Union[AppEntry, SettingEntry]] = list(self.apps.values()) + list(self.settings.values())
        for item in combined:
            sq = item.spotlight_query.lower()
            dn = item.display_name.lower()
            if sq == target or dn == target:
                exact_matches.append(item)
            elif sq.startswith(target) or dn.startswith(target):
                prefix_matches.append(item)
            elif target in sq or target in dn:
                substr_matches.append(item)

        return exact_matches + prefix_matches + substr_matches


def _load_yaml_file(path: Path) -> Dict[str, Any]:
    """Load a YAML file safely."""
    if yaml is None:
        raise CatalogError(
            "PyYAML is not installed. Please install 'pyyaml' to load catalog files."
        )
    if not path.is_file():
        raise CatalogError(f"Catalog file not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValidationError(f"Catalog file {path.name} root must be a YAML mapping/dict")
    return data


def load_apps(path: Optional[Union[str, Path]] = None) -> List[AppEntry]:
    """Load and validate apps list from catalog/apps.yaml."""
    if path is None:
        path = Path(__file__).parent / "apps.yaml"
    else:
        path = Path(path)

    raw_data = _load_yaml_file(path)
    raw_apps = raw_data.get("apps")
    if not isinstance(raw_apps, list):
        raise ValidationError(f"{path.name}: 'apps' must be a list of app entries")

    entries: List[AppEntry] = []
    seen_ids: set[str] = set()
    for item in raw_apps:
        entry = AppEntry.from_dict(item)
        if entry.id in seen_ids:
            raise ValidationError(f"Duplicate app ID found: {entry.id!r}")
        seen_ids.add(entry.id)
        entries.append(entry)

    return entries


def load_settings(path: Optional[Union[str, Path]] = None) -> List[SettingEntry]:
    """Load and validate settings list from catalog/settings.yaml."""
    if path is None:
        path = Path(__file__).parent / "settings.yaml"
    else:
        path = Path(path)

    raw_data = _load_yaml_file(path)
    raw_destinations = raw_data.get("destinations")
    if not isinstance(raw_destinations, list):
        raise ValidationError(f"{path.name}: 'destinations' must be a list of setting entries")

    entries: List[SettingEntry] = []
    seen_ids: set[str] = set()
    for item in raw_destinations:
        entry = SettingEntry.from_dict(item)
        if entry.id in seen_ids:
            raise ValidationError(f"Duplicate setting ID found: {entry.id!r}")
        seen_ids.add(entry.id)
        entries.append(entry)

    return entries


def load_catalog(catalog_dir: Optional[Union[str, Path]] = None) -> Catalog:
    """Load the complete catalog (apps + settings) from catalog directory."""
    if catalog_dir is None:
        catalog_dir = Path(__file__).parent
    else:
        catalog_dir = Path(catalog_dir)

    apps_list = load_apps(catalog_dir / "apps.yaml")
    settings_list = load_settings(catalog_dir / "settings.yaml")

    apps_map = {app.id: app for app in apps_list}
    settings_map = {setting.id: setting for setting in settings_list}

    return Catalog(
        apps=apps_map,
        settings=settings_map,
    )
