from __future__ import annotations

import mimetypes
import re
from pathlib import PurePosixPath
from urllib.parse import unquote

from django.contrib.staticfiles import finders
from django.contrib.staticfiles.storage import staticfiles_storage
from django.http import HttpResponse


_FINGERPRINT_RE = re.compile(r"(?:^|[._-])[0-9a-f]{8,}(?:[._-]|$)", re.IGNORECASE)


def normalize_static_path(path: str) -> str | None:
    raw = (path or "").strip()
    if not raw:
        return None

    try:
        cleaned = PurePosixPath(unquote(raw).lstrip("/"))
    except Exception:
        return None

    parts: list[str] = []
    for part in cleaned.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            return None
        parts.append(part)

    if not parts:
        return None
    return "/".join(parts)


def read_static_asset(path: str) -> tuple[str, bytes] | None:
    normalized = normalize_static_path(path)
    if not normalized:
        return None

    try:
        found_path = finders.find(normalized)
        if found_path:
            with open(found_path, "rb") as f:
                return normalized, f.read()
    except Exception:
        pass

    try:
        with staticfiles_storage.open(normalized, "rb") as f:
            return normalized, f.read()
    except Exception:
        return None


def static_cache_control(path: str, *, is_versioned: bool = False) -> str:
    normalized = normalize_static_path(path) or ""
    filename = normalized.rsplit("/", 1)[-1]
    if is_versioned or _FINGERPRINT_RE.search(filename):
        return "public, max-age=31536000, immutable"
    return "public, max-age=86400"


def build_static_response(
    path: str,
    *,
    method: str = "GET",
    cache_control: str | None = None,
    is_versioned: bool = False,
    range_header: str | None = None,
) -> HttpResponse | None:
    loaded = read_static_asset(path)
    if not loaded:
        return None

    normalized, content = loaded
    content_type, content_encoding = mimetypes.guess_type(normalized)

    is_head = str(method).upper() == "HEAD"
    is_video = PurePosixPath(normalized).suffix.lower() in {".mp4", ".webm"}
    status, content_range = 200, None
    body = content
    if is_video and not is_head and range_header:
        # Browser media controls need byte ranges to seek without restarting.
        # Ignore malformed or multipart ranges; serve a full representation.
        match = re.fullmatch(r"bytes=([0-9]{0,20})-([0-9]{0,20})", range_header.strip())
        if match and (match[1] or match[2]):
            size = len(content)
            start = int(match[1]) if match[1] else max(0, size - int(match[2]))
            end = min(int(match[2]), size - 1) if match[1] and match[2] else size - 1
            if start >= size or end < start or (not match[1] and int(match[2]) == 0):
                status, body, content_range = 416, b"", f"bytes */{size}"
            else:
                status, body, content_range = 206, content[start:end + 1], f"bytes {start}-{end}/{size}"
    response = HttpResponse(b"" if is_head else body, status=status, content_type=content_type or "application/octet-stream")
    response["Content-Length"] = str(len(content) if is_head else len(body))
    if is_video:
        response["Accept-Ranges"] = "bytes"
        # Keep the outer GZipMiddleware from transforming byte offsets.
        response["Content-Encoding"] = "identity"
    if content_range:
        response["Content-Range"] = content_range
    response["Cache-Control"] = cache_control or static_cache_control(
        normalized,
        is_versioned=is_versioned,
    )
    if content_encoding:
        response["Content-Encoding"] = content_encoding
    return response
