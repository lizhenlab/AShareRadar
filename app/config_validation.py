from __future__ import annotations

from urllib.parse import SplitResult, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_LLM_HTTP_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def normalized_llm_base_url(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    text = value.strip()
    parsed, host = _parse_llm_base_url(text)
    scheme = parsed.scheme.casefold()
    _validate_llm_url_policy(parsed, scheme, host)
    return urlunsplit((scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def normalized_timezone_name(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("legacy_audit_timezone 不能为空")
    try:
        timezone = ZoneInfo(text)
    except (ValueError, ZoneInfoNotFoundError):
        raise ValueError("legacy_audit_timezone 必须是有效的 IANA 时区名称") from None
    return timezone.key


def _parse_llm_base_url(text: str) -> tuple[SplitResult, str]:
    if any(char.isspace() for char in text):
        raise ValueError("llm_base_url 必须是绝对 HTTP(S) URL")
    try:
        parsed = urlsplit(text)
        host = parsed.hostname
        _ = parsed.port
    except ValueError:
        raise ValueError("llm_base_url 必须是合法的绝对 HTTP(S) URL") from None
    if "@" in parsed.netloc or parsed.username is not None or parsed.password is not None:
        raise ValueError("llm_base_url 不允许包含 userinfo")
    if host is None:
        raise ValueError("llm_base_url 必须是绝对 HTTP(S) URL")
    return parsed, host


def _validate_llm_url_policy(parsed: SplitResult, scheme: str, host: str) -> None:
    if scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("llm_base_url 必须是绝对 HTTP(S) URL")
    if scheme == "http" and host.casefold() not in _LLM_HTTP_LOOPBACK_HOSTS:
        raise ValueError("llm_base_url 必须使用 HTTPS，只有 localhost、127.0.0.1 和 [::1] 可使用 HTTP")
    if parsed.query or parsed.fragment:
        raise ValueError("llm_base_url 不允许包含查询参数或片段")


_normalized_llm_base_url = normalized_llm_base_url


__all__ = ["normalized_llm_base_url", "normalized_timezone_name"]
