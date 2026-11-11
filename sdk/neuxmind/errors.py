"""The single error type raised by the SDK."""
from __future__ import annotations

from typing import Any


class NeuxMindError(Exception):
    """An error from the SDK, the edge runtime or the cloud.

    Attributes
    ----------
    code:
        Stable machine-readable string. Server errors keep the server's code
        (e.g. ``estop_latched``, ``incompatible``, ``insufficient_credits``).
        Errors raised locally by the SDK use their own codes:

        ``authorization_required``  motion call without ``authorize=True`` (nothing was sent)
        ``connection_failed``       the server could not be reached (nothing was processed)
        ``outcome_unknown``         a request was sent but no answer came back; it may or may
                                    not have been executed — query state, do not blindly retry
        ``wait_timeout``            :meth:`RobotClient.wait` gave up waiting
        ``invalid_package``         local skill package validation failed
        ``config_error``            missing / invalid local configuration
        ``spec_not_found`` / ``runtime_not_found``  repository packages not found
    status:
        HTTP status for server errors, else ``None``.
    details:
        Extra fields from the server error body (e.g. ``compatibility``) or local context.
    """

    def __init__(self, message: str, code: str = "error", status: int | None = None,
                 details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"error": self.message, "code": self.code}
        if self.status is not None:
            out["status"] = self.status
        if self.details:
            out["details"] = self.details
        return out

    def __str__(self) -> str:
        return f"{self.message}（代码 {self.code}）"
