"""Trusted runtime identity used to bind Flow grants to one local process."""

from __future__ import annotations

import ctypes
import getpass
import os
from dataclasses import dataclass
from typing import Protocol


class RuntimeBindingUnavailable(RuntimeError):
    """The current user/session/process identity could not be determined."""


@dataclass(frozen=True, slots=True)
class RuntimeBinding:
    user_id: str
    os_session_id: str
    process_id: int

    def complete(self) -> bool:
        return bool(self.user_id and self.os_session_id and self.process_id > 0)


class RuntimeBindingProvider(Protocol):
    def current(self) -> RuntimeBinding:
        """Return the trusted identity of the running backend process."""


class LocalRuntimeBindingProvider:
    """Resolve process identity without trusting request/model content."""

    def current(self) -> RuntimeBinding:
        process_id = os.getpid()
        if os.name == "nt":
            user_id = _windows_user_sid()
            os_session_id = _windows_session_id(process_id)
        else:
            user_id = f"uid:{os.getuid()}" if hasattr(os, "getuid") else getpass.getuser()
            os_session_id = f"sid:{os.getsid(0)}" if hasattr(os, "getsid") else ""
        binding = RuntimeBinding(user_id=user_id, os_session_id=os_session_id, process_id=process_id)
        if not binding.complete():
            raise RuntimeBindingUnavailable("runtime binding is unavailable")
        return binding


def _windows_session_id(process_id: int) -> str:
    from ctypes import wintypes

    session_id = wintypes.DWORD()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
    if not kernel32.ProcessIdToSessionId(process_id, ctypes.byref(session_id)):
        raise RuntimeBindingUnavailable("Windows session id is unavailable")
    return str(session_id.value)


def _windows_user_sid() -> str:
    from ctypes import wintypes

    token_query = 0x0008
    token_user_class = 1

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("sid", wintypes.LPVOID), ("attributes", wintypes.DWORD)]

    class TokenUser(ctypes.Structure):
        _fields_ = [("user", SidAndAttributes)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)):
        raise RuntimeBindingUnavailable("Windows user token is unavailable")
    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(token, token_user_class, None, 0, ctypes.byref(required))
        if required.value == 0:
            raise RuntimeBindingUnavailable("Windows user SID is unavailable")
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token, token_user_class, buffer, required, ctypes.byref(required)
        ):
            raise RuntimeBindingUnavailable("Windows user SID is unavailable")
        token_user = ctypes.cast(buffer, ctypes.POINTER(TokenUser)).contents
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(token_user.user.sid, ctypes.byref(sid_text)):
            raise RuntimeBindingUnavailable("Windows user SID is unavailable")
        try:
            value = sid_text.value or ""
        finally:
            kernel32.LocalFree(sid_text)
        if not value:
            raise RuntimeBindingUnavailable("Windows user SID is unavailable")
        return value
    finally:
        kernel32.CloseHandle(token)


__all__ = [
    "LocalRuntimeBindingProvider",
    "RuntimeBinding",
    "RuntimeBindingProvider",
    "RuntimeBindingUnavailable",
]
