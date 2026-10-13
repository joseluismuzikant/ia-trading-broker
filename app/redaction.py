"""Remove anything that looks like a credential from text that may be shown.

Error messages come from vendor SDKs and HTTP libraries, and some providers
echo a partly masked key back in their error body (``Incorrect API key
provided: sk-...abc``). Text that reaches a page, an event, a log line or a
trace goes through :func:`redact` first, so a credential cannot leak even when
the vendor includes one. It works on shape, not on knowing the secret, so it
also catches keys the application never held.
"""

from __future__ import annotations

import re

REDACTED = "[redacted]"

#: Credential shapes. Order matters only for readability: each is independent.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    # An Authorization header or a bare bearer token.
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{6,}"),
    # Vendor key prefixes, including the masked forms providers print
    # ("sk-proj-abc***xyz", "sk-...abc").
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_\-*.]{4,}"),
    re.compile(r"\bxai-[A-Za-z0-9_\-*.]{4,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-*.]{8,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_*]{8,}"),
    re.compile(r"\bAKIA[0-9A-Z*]{8,}"),
    # A JSON web token.
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{4,}"),
    # A private key block.
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S
    ),
    # key=value or key: value for names that hold a secret.
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|passwd|"
        r"client[_-]?key|api[_-]?secret)\b(\s*[=:]\s*)(['\"]?)[^\s'\",;]{4,}\3"
    ),
    # A long opaque run, the shape of any other token or Fernet key.
    re.compile(r"(?<![A-Za-z0-9_\-/.])[A-Za-z0-9_\-]{40,}={0,2}(?![A-Za-z0-9_\-/.])"),
)


def redact(text: object) -> str:
    """The text with every credential-shaped part replaced by ``[redacted]``."""
    cleaned = str(text)
    for pattern in _PATTERNS:
        if pattern.groups >= 2:
            cleaned = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", cleaned)
        else:
            cleaned = pattern.sub(REDACTED, cleaned)
    return cleaned
