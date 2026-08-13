"""Fail-closed abstraction around Windows UserConsentVerifier.

The Flow authority never invokes a real authentication prompt by itself. The
trusted native layer adapts Windows.Security.Credentials.UI.UserConsentVerifier
through ``WindowsUserConsentVerifier``; tests inject fakes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class OwnerVerificationStatus(str, Enum):
    VERIFIED = "verified"
    CANCELED = "canceled"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class OwnerVerificationResult:
    status: OwnerVerificationStatus

    @property
    def verified(self) -> bool:
        return self.status is OwnerVerificationStatus.VERIFIED


class UserConsentVerifier(Protocol):
    """Trusted owner-verification boundary used by Flow authority."""

    def verify(self, *, prompt: str) -> OwnerVerificationResult:
        """Return a normalized result without exposing authentication details."""


class FailClosedUserConsentVerifier:
    """Default verifier: Flow cannot activate until native verification is wired."""

    def verify(self, *, prompt: str) -> OwnerVerificationResult:
        del prompt
        return OwnerVerificationResult(OwnerVerificationStatus.UNAVAILABLE)


class WindowsUserConsentVerifier:
    """Adapter for a native UserConsentVerifier callback.

    ``request_verification`` performs the real Windows Hello/PIN request in the
    trusted native process and returns the WinRT result name. This adapter only
    normalizes that result and therefore never triggers authentication itself.
    """

    _UNAVAILABLE = {
        "devicenotpresent",
        "notconfiguredforuser",
        "disabledbypolicy",
    }
    _FAILED = {"devicebusy", "retriesexhausted"}

    def __init__(self, request_verification: Callable[[str], str]) -> None:
        self._request_verification = request_verification

    def verify(self, *, prompt: str) -> OwnerVerificationResult:
        try:
            raw = str(self._request_verification(prompt)).replace("_", "").casefold()
        except Exception:
            return OwnerVerificationResult(OwnerVerificationStatus.FAILED)
        if raw == "verified":
            status = OwnerVerificationStatus.VERIFIED
        elif raw == "canceled":
            status = OwnerVerificationStatus.CANCELED
        elif raw in self._UNAVAILABLE:
            status = OwnerVerificationStatus.UNAVAILABLE
        else:
            status = OwnerVerificationStatus.FAILED
        return OwnerVerificationResult(status)


__all__ = [
    "FailClosedUserConsentVerifier",
    "OwnerVerificationResult",
    "OwnerVerificationStatus",
    "UserConsentVerifier",
    "WindowsUserConsentVerifier",
]
