"""Shared safe error types for account runtime modules."""

from __future__ import annotations


class BrowserRuntimeError(RuntimeError):
    """Safe, structured runtime error; ``detail`` never contains secrets."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail[:255]
        message = code if not self.detail else f"{code}: {self.detail}"
        super().__init__(message)
