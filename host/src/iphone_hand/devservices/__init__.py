"""Structured developer-services layer. Imports perform no device I/O."""

from .bridge import App, Backend, DevServices, DevServicesError, Screenshot
from .captions import Element, parse_caption
from .geometry import GeometryMatch, OCRBox, match_to_ocr

__all__ = ["App", "Backend", "DevServices", "DevServicesError", "Element",
           "GeometryMatch", "OCRBox", "Screenshot", "match_to_ocr", "parse_caption"]
