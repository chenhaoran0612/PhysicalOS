"""Error types shared by the runtime. Each maps to an HTTP status and code."""
from __future__ import annotations


class RuntimeApiError(Exception):
    """Base error. ``code`` is a stable machine-readable string."""

    status = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None, extra: dict | None = None):
        super().__init__(message)
        if code:
            self.code = code
        if status:
            self.status = status
        self.extra = extra or {}

    def to_dict(self) -> dict:
        return {"error": str(self), "code": self.code, **self.extra}


class BadRequest(RuntimeApiError):
    status, code = 400, "bad_request"


class NotFound(RuntimeApiError):
    status, code = 404, "not_found"


class NotAuthorized(RuntimeApiError):
    """A motion command or skill run without ``hardware_authorized=true``."""
    status, code = 403, "hardware_not_authorized"


class Conflict(RuntimeApiError):
    status, code = 409, "conflict"


class SafetyRejected(RuntimeApiError):
    """The safety supervisor refused a command (limits, latch, ...)."""
    status, code = 409, "safety_rejected"


class EStopLatched(SafetyRejected):
    code = "estop_latched"


class Incompatible(RuntimeApiError):
    status, code = 409, "incompatible"


class AdapterFault(RuntimeApiError):
    """The adapter raised or returned data violating the capability spec."""
    status, code = 502, "adapter_fault"


class UnknownOutcome(RuntimeApiError):
    """A previous request with this id has no confirmed result; never replayed."""
    status, code = 409, "outcome_unknown"
