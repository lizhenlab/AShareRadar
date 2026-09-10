"""Stream signed objects without forwarding account headers or logging URLs."""

from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import ipaddress
from pathlib import Path
import re
import socket
from urllib.parse import urlsplit

import httpx

from app.services.fuyao_dumps_validation import FuyaoDumpError
from app.services.lifecycle_cleanup import await_cleanup
from app.services.fuyao_sync_control import FuyaoSyncControl
from app.utils.clock import utc_now


def validate_download_url(url: object, allowed_hosts: tuple[str, ...]) -> str:
    if not isinstance(url, str) or len(url) > 16384:
        raise FuyaoDumpError("下载地址缺失或不符合约定")
    try:
        parsed = urlsplit(url)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise FuyaoDumpError("下载地址格式无效") from None
    if parsed.scheme != "https" or not host or port not in (None, 443) or parsed.username is not None or parsed.password is not None:
        raise FuyaoDumpError("下载地址必须使用不含认证信息的 HTTPS")
    if parsed.fragment or host not in allowed_hosts or re.fullmatch(r"[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?", host) is None:
        raise FuyaoDumpError("下载域名未配置为受信任的官方对象存储域名")
    if "." not in host or host.endswith((".local", ".localhost", ".internal")):
        raise FuyaoDumpError("下载域名必须指向公网对象存储")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise FuyaoDumpError("下载域名不接受直接 IP 地址")


async def require_public_dns(host: str) -> None:
    try:
        entries = await asyncio.to_thread(socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM)
        addresses = [ipaddress.ip_address(entry[4][0]) for entry in entries]
    except (OSError, ValueError):
        raise FuyaoDumpError("无法确认对象存储的公网地址") from None
    if not addresses or any(not address.is_global for address in addresses):
        raise FuyaoDumpError("对象存储解析到非公网地址，已拒绝下载")


def signed_url(response: dict[str, object]) -> str:
    data = response.get("data")
    if type(response.get("code")) is not int or response["code"] != 0 or not isinstance(data, dict):
        raise FuyaoDumpError("签名接口没有返回成功信封")
    url, expires = data.get("presigned_url"), data.get("presigned_url_expires_at")
    if not isinstance(url, str) or not isinstance(expires, str):
        raise FuyaoDumpError("签名接口缺少下载地址或有效期")
    try:
        instant = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        valid = instant.tzinfo is not None and instant > utc_now()
    except ValueError:
        valid = False
    if not valid:
        raise FuyaoDumpError("下载签名已过期或有效期格式无效，请重新同步")
    return url


async def download_dump(url: str, path: Path, *, allowed_hosts: tuple[str, ...], max_bytes: int,
                        transport: httpx.AsyncBaseTransport | None = None, control: FuyaoSyncControl | None = None) -> str:
    control = control or FuyaoSyncControl()
    control.checkpoint()
    host = validate_download_url(url, allowed_hosts)
    if type(max_bytes) is not int or max_bytes <= 0:
        raise FuyaoDumpError("下载容量上限必须为正整数")
    await require_public_dns(host)
    control.checkpoint()
    active = transport or httpx.AsyncHTTPTransport(retries=0, trust_env=False)
    try:
        request = httpx.Request("GET", url, headers={"accept": "application/octet-stream", "accept-encoding": "identity"},
                                extensions={"timeout": {"connect": 15.0, "read": 60.0, "write": 15.0, "pool": 15.0}})
        # Direct transport omits AsyncClient's INFO log containing the signed URL.
        async with asyncio.timeout(1800):
            response = await active.handle_async_request(request)
            try:
                return await _stream_file(response, path, max_bytes, control)
            finally:
                await await_cleanup(asyncio.create_task(response.aclose()))
    except (httpx.HTTPError, TimeoutError):
        raise FuyaoDumpError("对象存储下载失败或超时，请重新同步") from None
    finally:
        if transport is None:
            await await_cleanup(asyncio.create_task(active.aclose()))


async def _stream_file(response: httpx.Response, path: Path, max_bytes: int, control: FuyaoSyncControl | None = None) -> str:
    control = control or FuyaoSyncControl()
    control.checkpoint()
    if response.status_code != 200:
        raise FuyaoDumpError("对象存储未返回文件；拒绝重定向或过期签名")
    if response.headers.get("content-encoding", "identity").lower() != "identity":
        raise FuyaoDumpError("拒绝压缩传输编码，避免下载容量校验失真")
    declared = response.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or len(declared) > 22 or int(declared) > max_bytes):
        raise FuyaoDumpError("下载文件声明容量超过上限或无效")
    digest, size = hashlib.sha256(), 0
    control.checkpoint(current=0, total=int(declared) if declared is not None else None, unit="bytes")
    with path.open("xb") as output:
        async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
            control.checkpoint()
            size += len(chunk)
            if size > max_bytes:
                raise FuyaoDumpError("下载文件实际容量超过上限")
            output.write(chunk)
            digest.update(chunk)
            control.checkpoint(current=size)
    if not size or (declared is not None and int(declared) != size):
        raise FuyaoDumpError("下载文件为空或实际长度与声明不一致")
    return digest.hexdigest()
