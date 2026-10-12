"""iPhone Hand Record/Replay Simulator (SPEC v0.2).

Offline record/replay simulator so agents can be evaluated without physical hardware.
"""
from .builder import DatasetBuilder
from .graph import ScreenGraph, ScreenNode, Transition
from .hash import compute_dhash, compute_phash, hamming_distance, screens_match
from .manifest import ActionRecord, SessionManifest, StepRecord, UIElement
from .recorder import SessionRecorder
from .server import ReplayServer, create_replay_app

__all__ = [
    "DatasetBuilder",
    "ScreenGraph",
    "ScreenNode",
    "Transition",
    "compute_dhash",
    "compute_phash",
    "hamming_distance",
    "screens_match",
    "ActionRecord",
    "SessionManifest",
    "StepRecord",
    "UIElement",
    "SessionRecorder",
    "ReplayServer",
    "create_replay_app",
]
