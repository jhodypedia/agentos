"""Refuse to persist credentials as ordinary memory.

The rule from the specification is absolute: API keys and secrets never go into
memory. Detection is deliberately conservative — it prefers a false positive
(memory rejected, agent told why) over a false negative (a live key written to a
table that is read back into every future prompt).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Anything that looks like a credential assignment: `api_key = "..."`.
_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|passwd|authorization|bearer|"
    r"client[_-]?secret|private[_-]?key|access[_-]?key|refresh[_-]?token)\b"
    r"\s*[:=]\s*\S{6,}"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[a-z0-9._\-]{12,}")
_KNOWN_PREFIXES = re.compile(
    r"\b(sk-[A-Za-z0-9_\-]{16,}"
    r"|ghp_[A-Za-z0-9]{20,}"
    r"|gho_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[baprs]-[A-Za-z0-9\-]{10,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|aos_[A-Za-z0-9]{20,}"
    r"|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})"
)
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_URL_CREDENTIALS = re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s/:@]+:[^\s/@]{6,}@")


@dataclass(frozen=True, slots=True)
class SensitivityVerdict:
    sensitive: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.sensitive


def inspect(text: str) -> SensitivityVerdict:
    """Classify text as secret-bearing or safe to store."""
    if not text:
        return SensitivityVerdict(False)
    if _PEM.search(text):
        return SensitivityVerdict(True, "contains a private key block")
    if _KNOWN_PREFIXES.search(text):
        return SensitivityVerdict(True, "contains something shaped like a provider token")
    if _BEARER.search(text):
        return SensitivityVerdict(True, "contains a bearer token")
    if _URL_CREDENTIALS.search(text):
        return SensitivityVerdict(True, "contains credentials embedded in a URL")
    if _ASSIGNMENT.search(text):
        return SensitivityVerdict(True, "assigns a value to a credential-like field")
    return SensitivityVerdict(False)


def redact(text: str) -> str:
    """Replace detected secrets with a marker; used before truncating for logs."""
    redacted = _PEM.sub("[REDACTED PRIVATE KEY]", text)
    redacted = _KNOWN_PREFIXES.sub("[REDACTED TOKEN]", redacted)
    redacted = _BEARER.sub("[REDACTED BEARER]", redacted)
    redacted = _URL_CREDENTIALS.sub("[REDACTED URL CREDENTIALS]", redacted)
    return _ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", redacted)
