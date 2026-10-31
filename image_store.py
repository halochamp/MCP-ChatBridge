"""Pure helpers for collecting generated images: filter, validate, and save.

The browser layer enumerates *every* image in the owned assistant turn and this
module filters afterwards.  Provider UIs change their image components often;
keeping the policy here (size, host, magic bytes, dedupe) means a UI change only
touches one tuple or threshold instead of a DOM selector chain.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_IMAGES_LIMIT = 8
MIN_IMAGE_EDGE = 128
DEFAULT_SAVE_DIR = Path("~/Pictures/MCP-ChatBridge/gemini")

# Hosts the page may be asked to fetch from.  blob:/data:image are page-local.
ALLOWED_IMAGE_HOST_ROOTS = ("googleusercontent.com", "gstatic.com", "google.com")
# ChatGPT serves generated images from chatgpt.com (estuary/content) or OpenAI file hosts.
CHATGPT_IMAGE_HOST_ROOTS = ("chatgpt.com", "oaiusercontent.com", "oaistatic.com", "openai.com")

_FULL_SIZE_SUFFIX = re.compile(r"=(?:s\d+|w\d+|h\d+)[A-Za-z0-9-]*$")
_GOOGLE_USER_CONTENT = "googleusercontent.com"


class ImageStoreError(RuntimeError):
    """Raised for invalid image candidates or an unsafe save location."""


def url_is_allowed(src: object, host_roots: tuple[str, ...] = ALLOWED_IMAGE_HOST_ROOTS) -> bool:
    if not isinstance(src, str) or not src or len(src) > 8192:
        return False
    if src.startswith("blob:"):
        return True
    if src.startswith("data:image/"):
        return True
    parsed = urlparse(src)
    host = (parsed.hostname or "").lower().rstrip(".")
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and parsed.port in (None, 443)
        and any(host == root or host.endswith(f".{root}") for root in host_roots)
    )


def filter_candidates(
    candidates: object,
    max_images: int,
    host_roots: tuple[str, ...] = ALLOWED_IMAGE_HOST_ROOTS,
) -> list[dict[str, Any]]:
    """Keep real, visible, content-sized images from the raw page enumeration."""
    if not isinstance(candidates, list):
        return []
    limit = max(1, min(int(max_images), MAX_IMAGES_LIMIT))
    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in candidates:
        if not isinstance(item, dict):
            continue
        src = item.get("src")
        if not url_is_allowed(src, host_roots) or src in seen:
            continue
        try:
            width = int(item.get("width", 0))
            height = int(item.get("height", 0))
        except (TypeError, ValueError):
            continue
        if not item.get("visible") or min(width, height) < MIN_IMAGE_EDGE:
            continue
        seen.add(src)
        kept.append({"src": src, "width": width, "height": height})
        if len(kept) >= limit:
            break
    return kept


def full_resolution_url(src: str) -> str | None:
    """Return the original-size variant of a googleusercontent URL, if it has a size suffix."""
    if not src.startswith("https://"):
        return None
    host = (urlparse(src).hostname or "").lower()
    if host != _GOOGLE_USER_CONTENT and not host.endswith(f".{_GOOGLE_USER_CONTENT}"):
        return None
    if not _FULL_SIZE_SUFFIX.search(src):
        return None
    return _FULL_SIZE_SUFFIX.sub("=s0", src)


def sniff_image(data: bytes) -> tuple[str, str] | None:
    """Identify PNG/JPEG/WEBP by magic bytes; the response header is never trusted."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None


def resolve_save_dir(value: str | None, repository_root: Path, provider: str = "gemini") -> Path:
    # Default: ~/Pictures/MCP-ChatBridge/<provider> (gemini keeps the original DEFAULT_SAVE_DIR).
    default = DEFAULT_SAVE_DIR if provider == "gemini" else DEFAULT_SAVE_DIR.parent / provider
    raw = Path(value).expanduser() if value else default.expanduser()
    if not raw.is_absolute():
        raise ImageStoreError("save_dir must be an absolute path (or start with ~).")
    resolved = raw.resolve()
    root = repository_root.resolve()
    if resolved == root or root in resolved.parents:
        raise ImageStoreError("save_dir must be outside the MCP-ChatBridge repository.")
    return resolved


MAX_ATTACHMENTS = 4


def validate_attachments(paths: object, *, max_files: int = MAX_ATTACHMENTS) -> list[str]:
    """Resolve user-supplied attachment paths; only real PNG/JPEG/WEBP files within the size cap.

    Attachments are read by the browser and uploaded to the provider, so the gate is strict:
    explicit absolute (or ~) paths, regular files, image magic bytes, bounded count and size.
    """
    if paths is None:
        return []
    if not isinstance(paths, (list, tuple)):
        raise ImageStoreError("attach_images must be a list of file paths.")
    if len(paths) > max_files:
        raise ImageStoreError(f"At most {max_files} images can be attached.")
    resolved_paths: list[str] = []
    for value in paths:
        if not isinstance(value, str) or not value.strip():
            raise ImageStoreError("Each attached image must be a non-empty path string.")
        raw = Path(value).expanduser()
        if not raw.is_absolute():
            raise ImageStoreError(f"Attached image path must be absolute (or start with ~): {value!r}")
        resolved = raw.resolve()
        if not resolved.is_file():
            raise ImageStoreError(f"Attached image does not exist or is not a regular file: {value!r}")
        size = resolved.stat().st_size
        if size == 0 or size > MAX_IMAGE_BYTES:
            raise ImageStoreError(
                f"Attached image must be between 1 byte and {MAX_IMAGE_BYTES // (1024 * 1024)} MB: {value!r}"
            )
        with resolved.open("rb") as handle:
            head = handle.read(16)
        if sniff_image(head) is None:
            raise ImageStoreError(f"Attached file is not a PNG, JPEG, or WEBP image: {value!r}")
        if str(resolved) not in resolved_paths:
            resolved_paths.append(str(resolved))
    return resolved_paths


def save_image(data: bytes, directory: Path, *, provider: str) -> dict[str, Any]:
    """Validate and write one image; returns path/mime/size/sha256 (never the bytes)."""
    if not data:
        raise ImageStoreError("The downloaded image was empty.")
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageStoreError(f"The downloaded image exceeds the {MAX_IMAGE_BYTES // (1024 * 1024)} MB limit.")
    kind = sniff_image(data)
    if kind is None:
        raise ImageStoreError("The downloaded file is not a PNG, JPEG, or WEBP image.")
    ext, mime = kind
    digest = hashlib.sha256(data).hexdigest()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    path = directory / f"{provider}-{stamp}-{digest[:12]}.{ext}"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Same content within the same second: already saved, treat as success.
        return {"path": str(path), "mime": mime, "bytes": len(data), "sha256": digest, "duplicate": True}
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    return {"path": str(path), "mime": mime, "bytes": len(data), "sha256": digest}


__all__ = [
    "ALLOWED_IMAGE_HOST_ROOTS",
    "CHATGPT_IMAGE_HOST_ROOTS",
    "DEFAULT_SAVE_DIR",
    "ImageStoreError",
    "MAX_ATTACHMENTS",
    "MAX_IMAGES_LIMIT",
    "MAX_IMAGE_BYTES",
    "MIN_IMAGE_EDGE",
    "filter_candidates",
    "full_resolution_url",
    "resolve_save_dir",
    "save_image",
    "sniff_image",
    "url_is_allowed",
    "validate_attachments",
]
