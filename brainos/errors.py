"""Structured errors for the desktop API (ux04, Q5-04).

The browser explains an error by its machine-readable ``code`` (and the HTTP
status the *device* or cloud returned, ``device_status``) -- never by guessing
from numbers or words in the message. Everything that raises a ValueError still
answers HTTP 400 as before; these helpers only add fields to the JSON body.
"""

# Body fields of a device/cloud refusal the desktop passes on to the browser (field-level errors).
# ux05 (Q6-08): the runtime's names -- ``reset_in_progress_s`` (not ``in_flight_s``), ``cause`` (why a reset
# failed: ``latch_guard_reset_grace`` / ``stop_during_reset``), ``hint`` (the operator's way out), and for
# parameter refusals ``problem``, ``constraint`` (e.g. ``{"min_length": 2}``), ``expected`` and ``step``.
PASS_FIELDS = ('param', 'parameter', 'field', 'min', 'max', 'choices', 'options', 'value', 'limit',
               'capability', 'reset_in_progress_s', 'cause', 'hint', 'problem', 'constraint', 'expected',
               'step', 'value_length')
# Longer free text the browser shows as is (the runtime's operator hint).
_LONG_TEXT = {'hint': 400}


class CodedError(ValueError):
    """A desktop-side refusal with a stable ``code`` (e.g. ``authorization_required``)."""

    def __init__(self, message, code, **extra):
        super().__init__(message)
        self.code = code
        self.extra = extra


def _clean(value, depth=0, limit=200):
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value if value == value and abs(value) != float('inf') else None
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, list) and depth == 0:
        return [_clean(v, 1) for v in value[:50]]
    if isinstance(value, dict) and depth == 0:
        # a small flat object such as ``constraint: {"min": 1, "max": 3}`` or ``{"choices": [...]}``
        out = {}
        for k, v in list(value.items())[:10]:
            if isinstance(k, str) and len(k) <= 40:
                cleaned = _clean(v, 0 if isinstance(v, list) else 1)
                if cleaned is not None:
                    out[k] = cleaned
        return out or None
    return None


def refusal_fields(body):
    """The field-level part of a device/cloud error body, sanitised (scalars, short lists)."""
    out = {}
    if not isinstance(body, dict):
        return out
    for key in PASS_FIELDS:
        if key in body:
            value = _clean(body[key], limit=_LONG_TEXT.get(key, 200))
            if key == 'constraint' and not isinstance(value, dict):
                value = None
            if value is not None:
                out[key] = value
    return out


def error_payload(exc):
    """JSON body for an error answered by the desktop API."""
    out = {'error': str(exc)}
    code = getattr(exc, 'code', None)
    if isinstance(code, str) and code:
        out['code'] = code[:64]
    status = getattr(exc, 'status', None)
    if isinstance(status, int) and not isinstance(status, bool):
        out['device_status'] = status
    if isinstance(getattr(exc, 'body', None), dict):
        out.update(refusal_fields(exc.body))
    if isinstance(getattr(exc, 'extra', None), dict):
        out.update(refusal_fields(exc.extra))
        if isinstance(exc.extra.get('next'), str):
            out['next'] = exc.extra['next'][:300]
    return out
