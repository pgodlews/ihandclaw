"""Shared phone API for MCP and other front ends. Importing never opens devices."""

from .phone import Phone

__all__ = ["Phone"]
