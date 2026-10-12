"""Perceptual hashing for iPhone screen matching across recording sessions.

Supports DCT-based perceptual hash (pHash), difference hash (dHash), and average hash (aHash),
with status-bar and home-indicator masking to eliminate timestamp, battery,
and Wi-Fi icon jitter between runs.
"""
from __future__ import annotations

import cv2
import numpy as np
from PIL import Image


def mask_status_bar(
    image: Image.Image,
    top_ratio: float = 0.06,
    bottom_ratio: float = 0.05,
) -> Image.Image:
    """Mask out the iPhone top status bar (clock/battery) and bottom home indicator.

    On iPhone XS (375x812 pt), status bar is ~44 pt (~5.4%) and home bar is ~34 pt (~4.2%).
    Masking these areas ensures recordings taken at different minutes/battery levels match cleanly.
    """
    img = image.copy().convert("RGB")
    w, h = img.size
    top_px = int(h * top_ratio)
    bottom_px = int(h * (1.0 - bottom_ratio))

    arr = np.array(img)
    if top_px > 0:
        arr[:top_px, :] = 0
    if bottom_px < h:
        arr[bottom_px:, :] = 0
    return Image.fromarray(arr)


def _bits_to_hex(bits: np.ndarray) -> str:
    """Convert 1D boolean array to hexadecimal string."""
    hex_chars = []
    for i in range(0, len(bits), 4):
        chunk = bits[i : i + 4]
        val = 0
        for b in chunk:
            val = (val << 1) | int(b)
        hex_chars.append(f"{val:x}")
    return "".join(hex_chars)


def compute_dhash(
    image: Image.Image,
    hash_size: int = 8,
    mask_status: bool = True,
) -> str:
    """Compute difference hash (dHash) of an image.

    Resizes to (hash_size + 1, hash_size), compares adjacent pixels,
    and returns a hexadecimal string representation of the resulting bit array.
    """
    img = mask_status_bar(image) if mask_status else image
    gray = img.convert("L").resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS)
    arr = np.array(gray, dtype=np.int32)
    diff = arr[:, 1:] > arr[:, :-1]
    return _bits_to_hex(diff.flatten())


def compute_ahash(
    image: Image.Image,
    hash_size: int = 8,
    mask_status: bool = True,
) -> str:
    """Compute average hash (aHash) of an image based on mean intensity."""
    img = mask_status_bar(image) if mask_status else image
    gray = img.convert("L").resize((hash_size, hash_size), Image.Resampling.LANCZOS)
    arr = np.array(gray, dtype=np.float32)
    mean = np.mean(arr)
    bits = (arr > mean).flatten()
    return _bits_to_hex(bits)


def compute_phash(
    image: Image.Image,
    hash_size: int = 8,
    highfreq_factor: float = 4,
    mask_status: bool = True,
) -> str:
    """Compute DCT-based perceptual hash (pHash) of an image.

    Resizes to (hash_size * 4, hash_size * 4), performs 2D DCT via OpenCV,
    extracts the top-left low-frequency 8x8 AC coefficients, and computes a
    median-thresholded hash.
    """
    img = mask_status_bar(image) if mask_status else image
    size = int(hash_size * highfreq_factor)
    gray = img.convert("L").resize((size, size), Image.Resampling.LANCZOS)
    arr = np.array(gray, dtype=np.float32)

    dct = cv2.dct(arr)
    # Extract top-left 8x8 low frequencies
    sub = dct[:hash_size, :hash_size]
    # Median of AC frequencies (exclude DC component at [0, 0])
    ac = sub.flatten()[1:]
    med = float(np.median(ac)) if len(ac) > 0 else 0.0
    bits = (sub > med).flatten()
    return _bits_to_hex(bits)


def hamming_distance(hash1: str, hash2: str) -> int:
    """Compute bitwise Hamming distance between two hexadecimal hash strings."""
    if len(hash1) != len(hash2):
        raise ValueError(f"Hash lengths do not match: {len(hash1)} vs {len(hash2)}")
    val1 = int(hash1, 16)
    val2 = int(hash2, 16)
    return bin(val1 ^ val2).count("1")


def screens_match(
    img1: Image.Image,
    img2: Image.Image,
    threshold: int = 6,
    method: str = "phash",
    mask_status: bool = True,
) -> bool:
    """Determine whether two screens represent the same UI state within Hamming threshold.

    Defaults to DCT-based 'phash' for accurate perceptual matching.
    """
    if method == "dhash":
        h1 = compute_dhash(img1, mask_status=mask_status)
        h2 = compute_dhash(img2, mask_status=mask_status)
    elif method == "ahash":
        h1 = compute_ahash(img1, mask_status=mask_status)
        h2 = compute_ahash(img2, mask_status=mask_status)
    else:
        h1 = compute_phash(img1, mask_status=mask_status)
        h2 = compute_phash(img2, mask_status=mask_status)
    return hamming_distance(h1, h2) <= threshold
