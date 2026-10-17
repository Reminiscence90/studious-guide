"""HTTPS requests that trust the operating system's certificate store.

Some Python builds cannot find root certificates on their own, which shows up as
``CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate``. Antivirus or
company networks that inspect HTTPS add their own root certificate to the OS store,
which Python's default bundle does not see either. ``truststore`` makes Python use the
OS store; ``certifi`` (Mozilla's bundle) is the fallback.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from functools import cache
from typing import Any

USER_AGENT = "Mozilla/5.0 (trade-journal)"


class HttpError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@cache
def ssl_context() -> ssl.SSLContext:
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())


def get_json(url: str, headers: dict[str, str] | None = None, timeout: float = 20) -> Any:
    """GET ``url`` and decode JSON. Raises :class:`HttpError` with a readable message."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl_context()) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            body = json.loads(exc.read() or b"{}")
            detail = body.get("errorMessage") or body.get("message") or body.get("msg") or ""
        except (ValueError, AttributeError):
            pass
        raise HttpError(f"HTTP {exc.code}{': ' + detail if detail else ''}", exc.code) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise HttpError(f"could not connect ({reason})") from exc
