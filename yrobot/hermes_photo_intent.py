"""Reachy client for the authenticated one-shot Hermes local-photo intent."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Callable
from typing import Any
from urllib.request import Request, urlopen

PROTOCOL_VERSION = "v1"
INTENT_TIMEOUT_S = 1.5


def sign_photo_intent(secret: str, timestamp: int, nonce: str) -> str:
    """Return the canonical HMAC-SHA256 signature shared with Hermes."""
    message = f"{PROTOCOL_VERSION}:{timestamp}:{nonce}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


class HermesPhotoIntentNotifier:
    """Send a signed, no-content local-photo intent to Hermes.

    The notifier intentionally has no logger: signatures, nonces, and payloads
    are authentication material and must not leak into diagnostic logs.
    """

    def __init__(
        self,
        url: str,
        secret: str,
        *,
        now: Callable[[], float] | None = None,
        nonce_factory: Callable[[], str] | None = None,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self._url = url
        self._secret = secret
        self._now = now or time.time
        self._nonce_factory = nonce_factory or (lambda: uuid.uuid4().hex)
        self._opener = opener

    @property
    def enabled(self) -> bool:
        return bool(self._url and self._secret)

    def notify(self) -> bool:
        """Return true only if Hermes accepted the one-shot intent."""
        try:
            if not self.enabled:
                return False
            timestamp = int(self._now())
            nonce = self._nonce_factory()
            payload = {
                "timestamp": timestamp,
                "nonce": nonce,
                "signature": sign_photo_intent(self._secret, timestamp, nonce),
            }
            request = Request(
                self._url,
                data=json.dumps(payload, separators=(",", ":")).encode(),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with self._opener(request, timeout=INTENT_TIMEOUT_S) as response:
                status = getattr(response, "status", None)
                if status is None:
                    status = response.getcode()
                body = json.loads(response.read().decode())
            return status == 202 and body.get("ok") is True
        except Exception:  # fail closed; never log signed authentication material
            return False
