from __future__ import annotations

from collections.abc import Iterable
import json
import re
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urlsplit, urlunsplit


REDACTED = "<redacted>"
_URL_RE = re.compile(r"https?://[^\s<>{}\"']+", re.IGNORECASE)
_AUTHORIZATION_RE = re.compile(
    r"(?i)\b(?P<key>(?:proxy[_-]?)?authorization)(?P<separator>\s*[:=]\s*)[^\r\n]+"
)
_BEARER_RE = re.compile(
    r"(?i)\b(?P<label>Bearer)(?P<spacing>\s+)(?P<quote>['\"]?)"
    r"(?P<value>[A-Za-z0-9._~+/=-]+)(?P=quote)"
)
_PREFIXED_API_KEY_RE = re.compile(r"(?i)\b(?:sk|ak)-[a-z0-9._-]{6,}\b")
_SENSITIVE_KEY_PATTERN = (
    r"(?:proxy[_-]?)?authorization|access[_-]?token|refresh[_-]?token|auth[_-]?token|token|"
    r"x[_-]?api[_-]?key|api[_ -]?key|apikey|subscription[_-]?key|app[_-]?key|access[_-]?key|"
    r"secret[_-]?key|client[_-]?secret|secret|password|passwd|pwd|credential|signature|ut"
)
_QUOTED_SECRET_ASSIGNMENT_RE = re.compile(
    rf"(?i)(?P<key_quote>['\"])(?P<key>{_SENSITIVE_KEY_PATTERN})(?P=key_quote)"
    r"(?P<separator>\s*[:=]\s*)(?P<value_quote>['\"])(?P<value>.*?)(?P=value_quote)"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    rf"(?i)\b(?P<key>{_SENSITIVE_KEY_PATTERN})"
    r"(?P<separator>\s*(?::|=)\s*|\s+)"
    r"(?P<quote>['\"]?)(?P<value>[^\s,;，；&'\"\]\)]+)(?P=quote)"
)
_SENSITIVE_QUERY_KEYS = {
    "access_token",
    "refresh_token",
    "auth_token",
    "token",
    "api_key",
    "apikey",
    "x_api_key",
    "subscription_key",
    "app_key",
    "access_key",
    "secret_key",
    "key",
    "client_secret",
    "secret",
    "password",
    "passwd",
    "pwd",
    "authorization",
    "auth",
    "credential",
    "signature",
    "sig",
    "code",
    "ut",
}
_URL_TRAILING_PUNCTUATION = ".,;:!?)，。；！）"


class ProviderError(RuntimeError):
    """Base class for provider failures with explicit operational semantics."""


class ProviderCoverageMiss(ProviderError):
    """The provider is reachable but does not cover the requested instrument."""


class ProviderTransportError(ProviderError):
    """The provider could not be reached or timed out."""


class ProviderProtocolError(ProviderError):
    """The provider response was malformed, invalid, or unexpectedly stale."""


CoverageMissError = ProviderCoverageMiss


def is_provider_coverage_miss(exc: BaseException) -> bool:
    return isinstance(exc, ProviderCoverageMiss)


def sanitize_provider_error(value: object, *, sensitive_values: Iterable[object] = ()) -> str:
    text = str(value)
    if not text:
        return text
    sensitive = tuple(sensitive_values)
    text = _redact_sensitive_values(text, sensitive)
    text = _QUOTED_SECRET_ASSIGNMENT_RE.sub(_sanitize_quoted_secret_assignment, text)
    text = _URL_RE.sub(_sanitize_url_match, text)
    text = _AUTHORIZATION_RE.sub(_sanitize_authorization, text)
    text = _BEARER_RE.sub(_sanitize_bearer, text)
    text = _PREFIXED_API_KEY_RE.sub(REDACTED, text)
    text = _SECRET_ASSIGNMENT_RE.sub(_sanitize_secret_assignment, text)
    return _redact_sensitive_values(text, sensitive)


def _sanitize_url_match(match: re.Match[str]) -> str:
    raw = match.group(0)
    url, suffix = _split_url_suffix(raw)
    try:
        parsed = urlsplit(url)
        netloc = parsed.netloc
        if "@" in netloc:
            netloc = REDACTED + "@" + netloc.rsplit("@", 1)[-1]
        query = _sanitize_url_parameters(parsed.query)
        fragment = _sanitize_url_parameters(parsed.fragment)
        return urlunsplit((parsed.scheme, netloc, parsed.path, query, fragment)) + suffix
    except (TypeError, ValueError):
        return _redact_url_userinfo_fallback(url) + suffix


def _sanitize_url_parameters(value: str) -> str:
    if not value or "=" not in value:
        return value
    sanitized = urlencode(
        [
            (key, REDACTED if _sensitive_query_key(key) else item_value)
            for key, item_value in parse_qsl(value, keep_blank_values=True)
        ],
        doseq=True,
    )
    return sanitized.replace(quote_plus(REDACTED, safe=""), REDACTED)


def _split_url_suffix(value: str) -> tuple[str, str]:
    end = len(value)
    while end > 0 and value[end - 1] in _URL_TRAILING_PUNCTUATION:
        end -= 1
    return value[:end], value[end:]


def _redact_url_userinfo_fallback(value: str) -> str:
    scheme, marker, remainder = value.partition("://")
    if not marker or "@" not in remainder:
        return value
    return f"{scheme}{marker}{REDACTED}@{remainder.rsplit('@', 1)[-1]}"


def _sensitive_query_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")
    return normalized in _SENSITIVE_QUERY_KEYS or normalized.endswith(
        ("_token", "_secret", "_password", "_credential", "_signature", "_api_key")
    )


def _sanitize_authorization(match: re.Match[str]) -> str:
    return f"{match.group('key')}{match.group('separator')}{REDACTED}"


def _sanitize_bearer(match: re.Match[str]) -> str:
    quote_mark = match.group("quote")
    return f"{match.group('label')}{match.group('spacing')}{quote_mark}{REDACTED}{quote_mark}"


def _sanitize_secret_assignment(match: re.Match[str]) -> str:
    quote_mark = match.group("quote")
    return f"{match.group('key')}{match.group('separator')}{quote_mark}{REDACTED}{quote_mark}"


def _sanitize_quoted_secret_assignment(match: re.Match[str]) -> str:
    key_quote = match.group("key_quote")
    value_quote = match.group("value_quote")
    return (
        f"{key_quote}{match.group('key')}{key_quote}{match.group('separator')}"
        f"{value_quote}{REDACTED}{value_quote}"
    )


def _redact_sensitive_values(text: str, sensitive_values: Iterable[object]) -> str:
    variants: set[str] = set()
    for value in sensitive_values:
        if value is None:
            continue
        raw = str(value)
        if not raw:
            continue
        variants.update(_sensitive_value_variants(raw))
    for variant in sorted(variants, key=len, reverse=True):
        text = text.replace(variant, REDACTED)
    return text


def _sensitive_value_variants(value: str) -> set[str]:
    variants: set[str] = set()
    for candidate in {value, value.strip(), " ".join(value.split())} - {""}:
        escaped_ascii = json.dumps(candidate, ensure_ascii=True)[1:-1]
        escaped_unicode = json.dumps(candidate, ensure_ascii=False)[1:-1]
        variants.update(
            {
                candidate,
                quote(candidate, safe=""),
                quote_plus(candidate, safe=""),
                escaped_ascii,
                escaped_ascii.replace("\\", "\\\\"),
                escaped_unicode,
                escaped_unicode.replace("\\", "\\\\"),
            }
        )
    return variants
