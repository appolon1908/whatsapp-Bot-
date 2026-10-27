"""HTTP deadlines include executor cleanup; no external network effects."""

import asyncio
import importlib.util
import threading
import time

import pytest


def test_runtime_bounds_resolver_work_and_close_without_late_continuation():
    assert importlib.util.find_spec("whatsapp_bot.http_runtime"), "shared bounded HTTP runtime missing"
    from whatsapp_bot.http_runtime import HttpRuntime

    release = threading.Event()
    resolved = []
    continuations = []
    active = []
    active_lock = threading.Lock()
    before = {t.ident for t in threading.enumerate()}
    runtime = HttpRuntime(resolver_workers=2, resolver_pending=2, max_inflight=4)
    threads = {t.ident for t in threading.enumerate() if t.name.startswith("commerce-http")}

    def resolve():
        with active_lock:
            active.append(True)
        release.wait(2)
        resolved.append(True)

    async def fetch():
        await asyncio.to_thread(resolve)
        continuations.append(True)

    try:
        for _ in range(20):
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                runtime.run(fetch, timeout=0.01)
            assert time.monotonic() - started < 0.1
        assert len(active) <= 2
        assert {t.ident for t in threading.enumerate() if t.name.startswith("commerce-http")} == threads
        started = time.monotonic()
        runtime.close()
        assert time.monotonic() - started < 0.25
        with pytest.raises(TimeoutError):
            runtime.run(fetch, timeout=0.01)
    finally:
        release.set()
        runtime.close()
    # Underlying uncancellable OS-style work may finish; cancelled request must not resume.
    deadline = time.monotonic() + 1
    while len(resolved) < len(active) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert resolved
    assert continuations == []
    owned = threads - before
    deadline = time.monotonic() + 1
    while owned & {t.ident for t in threading.enumerate()} and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not owned & {t.ident for t in threading.enumerate()}


def test_runtime_deadline_cancellation_and_reuse():
    assert importlib.util.find_spec("whatsapp_bot.http_runtime"), "shared bounded HTTP runtime missing"
    from whatsapp_bot.http_runtime import HttpRuntime

    runtime = HttpRuntime(resolver_workers=1, resolver_pending=1, max_inflight=2)
    cancelled = threading.Event()

    async def stalled():
        try:
            await asyncio.sleep(1)
        finally:
            cancelled.set()

    async def immediate():
        return 42

    try:
        with pytest.raises(TimeoutError):
            runtime.run(stalled, timeout=0.02)
        assert cancelled.wait(0.2)
        assert runtime.run(immediate, timeout=0.2) == 42

        # A synchronous caller can itself have an active event loop.
        async def caller():
            return runtime.run(immediate, timeout=0.2)

        assert asyncio.run(caller()) == 42
    finally:
        runtime.close()


def test_expired_waiting_callbacks_do_not_start_and_admission_recovers():
    from concurrent.futures import ThreadPoolExecutor

    from whatsapp_bot.http_runtime import HttpRuntime

    runtime = HttpRuntime(resolver_workers=1, resolver_pending=1, max_inflight=4)
    entered, release = threading.Event(), threading.Event()
    starts = []

    async def delayed_loop():
        entered.set()
        release.wait(0.5)

    async def forbidden_late_request():
        starts.append(True)

    async def immediate():
        return 42

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            blocked = pool.submit(runtime.run, delayed_loop, timeout=0.01)
            assert entered.wait(1)
            try:
                for _ in range(12):
                    with pytest.raises(TimeoutError):
                        runtime.run(forbidden_late_request, timeout=0.005)
            finally:
                release.set()
            with pytest.raises(TimeoutError):
                blocked.result(timeout=1)
        assert starts == []
        # A barrier after queued callbacks verifies permits were released even when
        # their request coroutine never started.
        time.sleep(0.02)
        assert runtime.run(immediate, timeout=0.2) == 42
        assert starts == []
    finally:
        release.set()
        runtime.close()
