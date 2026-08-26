"""Optional, read-only Choice SDK session isolated from the application process."""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import date, datetime
import importlib
import importlib.util
import json
import multiprocessing as mp
from multiprocessing.connection import Connection
from pathlib import Path
import time
from typing import Any


READ_METHODS = frozenset({"csd", "css", "ctr", "sector", "tradedates", "datastatistics"})


class ChoiceError(RuntimeError):
    pass


def _json_default(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError("Choice returned an unsupported value type")


def _response(result: Any) -> dict[str, Any]:
    # Do not expose login details or native exceptions. No token file is read here.
    return json.loads(json.dumps({
        "error_code": int(result.ErrorCode),
        "codes": getattr(result, "Codes", []),
        "indicators": getattr(result, "Indicators", []),
        "dates": getattr(result, "Dates", []),
        "data": getattr(result, "Data", {}),
    }, ensure_ascii=False, allow_nan=False, default=_json_default))


def _sdk_worker(pipe: Connection) -> None:
    sdk = None
    logged_in = False
    try:
        sdk = importlib.import_module("EmQuantAPI").c
        login = sdk.start("ForceLogin=0,HTTPTimeout=10,RecordLoginInfo=0", lambda _: 1)
        logged_in = login.ErrorCode == 0
        pipe.send(_response(login))
        if not logged_in:
            return
        while True:
            request = pipe.recv()
            if request is None:
                return
            method, args = request
            if method not in READ_METHODS:
                raise ChoiceError("unsupported Choice operation")
            pipe.send(_response(getattr(sdk, method)(*args)))
    except (EOFError, BrokenPipeError):
        pass
    except Exception as exc:
        try:
            pipe.send({"worker_error": type(exc).__name__})
        except (EOFError, BrokenPipeError):
            pass
    finally:
        if sdk is not None and logged_in:
            sdk.stop()
        pipe.close()


class ChoiceSDKClient(AbstractContextManager["ChoiceSDKClient"]):
    """One login, serial calls, hard timeout; a timed-out native worker is killed."""

    def __init__(self, *, timeout: float = 45, interval: float = 1.0) -> None:
        if not 1 <= timeout <= 180 or not 0 <= interval <= 10:
            raise ValueError("invalid Choice timeout or call interval")
        self.timeout = timeout
        self.interval = interval
        self._process: Any = None
        self._pipe: Connection | None = None
        self._last_call = 0.0

    def __enter__(self) -> ChoiceSDKClient:
        context = mp.get_context("spawn")
        parent, child = context.Pipe()
        self._pipe = parent
        self._process = context.Process(target=_sdk_worker, args=(child,), daemon=True)
        ready = False
        try:
            self._process.start()
            child.close()
            self._check(self._receive(), "login")
            ready = True
        finally:
            if not ready:
                child.close()
                self.close()
        return self

    def request(self, method: str, args: list[str]) -> dict[str, Any]:
        if method not in READ_METHODS:
            raise ChoiceError("only read-only Choice research methods are permitted")
        if self._pipe is None or not self._process.is_alive():
            raise ChoiceError("Choice session is not active")
        delay = self.interval - (time.monotonic() - self._last_call)
        if delay > 0:
            time.sleep(delay)
        try:
            self._pipe.send((method, args))
            result = self._receive()
        except (OSError, EOFError) as exc:
            self.close()
            raise ChoiceError("Choice worker disconnected; resume after checking account/network") from exc
        self._last_call = time.monotonic()
        self._check(result, method)
        return result

    def _receive(self) -> dict[str, Any]:
        assert self._pipe is not None
        if not self._pipe.poll(self.timeout):
            self.close()
            raise ChoiceError("Choice request timed out; native worker terminated, no request marked complete")
        result = self._pipe.recv()
        if "worker_error" in result:
            error_type = result["worker_error"]
            self.close()
            raise ChoiceError(f"Choice SDK worker failed ({error_type}); check SDK installation and activation")
        return result

    @staticmethod
    def _check(result: dict[str, Any], stage: str) -> None:
        code = result.get("error_code")
        if code != 0:
            raise ChoiceError(f"Choice {stage} rejected the request (error_code={code}); no automatic retry")

    def close(self) -> None:
        process, pipe = self._process, self._pipe
        self._process = self._pipe = None
        if process is not None and process.pid is not None:
            if pipe is not None and process.is_alive():
                try:
                    pipe.send(None)
                except (OSError, EOFError):
                    pass
            process.join(2)
            if process.is_alive():
                process.terminate()
                process.join(2)
            if process.is_alive():
                process.kill()
                process.join(2)
            process.close()
        if pipe is not None:
            pipe.close()

    def __exit__(self, *_: object) -> None:
        self.close()


def sdk_available() -> bool:
    """Check installation without loading the native library or logging in."""
    return importlib.util.find_spec("EmQuantAPI") is not None


def safe_directory(path: Path) -> Path:
    from app.artifacts.io import path_has_only_trusted_aliases

    if ".workbuddy-ai" in path.parts or not path_has_only_trusted_aliases(path):
        raise ChoiceError("Choice output/control directory cannot traverse a symlink or protected directory")
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()
