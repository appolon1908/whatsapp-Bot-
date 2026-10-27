"""Bounded synchronous HTTP bridge, including uncancellable OS resolver work.

One daemon loop and four daemon resolver threads are shared per process. Timed-out
requests are cancelled without waiting for resolver shutdown. Resolver work and its
queue are bounded separately from live HTTP tasks; exhaustion fails closed. A DNS
call already inside the OS may finish later, but its cancelled HTTP task cannot
resume a request. No per-call threads or asyncio.run executor shutdown are used.
"""

import asyncio
import atexit
import concurrent.futures
import contextvars
import queue
import threading
import time

_BUDGET = contextvars.ContextVar("http_budget", default=None)


def _check_budget():
    budget = _BUDGET.get()
    if budget is not None and (budget[1].is_set() or time.monotonic() >= budget[0]):
        raise TimeoutError("HTTP deadline exceeded")


class _ResolverExecutor(concurrent.futures.Executor):
    """Small daemon pool with a physically bounded queue, including cancelled work."""

    def __init__(self, workers, pending):
        self._queue = queue.Queue(maxsize=pending)
        self._lock = threading.Lock()
        self._closed = False
        self._threads = [
            threading.Thread(target=self._work, name=f"commerce-http-resolver-{i}", daemon=True)
            for i in range(workers)
        ]
        for thread in self._threads:
            thread.start()

    def submit(self, fn, /, *args, **kwargs):
        future = concurrent.futures.Future()
        with self._lock:
            if self._closed:
                raise TimeoutError("HTTP runtime is closed")
            try:
                self._queue.put_nowait((future, fn, args, kwargs))
            except queue.Full:
                raise TimeoutError("HTTP resolver capacity exhausted") from None
        return future

    def _work(self):
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                future, fn, args, kwargs = item
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(fn(*args, **kwargs))
                    except BaseException as exc:
                        future.set_exception(exc)
            finally:
                self._queue.task_done()

    def shutdown(self, wait=True, *, cancel_futures=False):
        with self._lock:
            if not self._closed:
                self._closed = True
                # Runtime shutdown always abandons queued resolver work. Running OS
                # calls cannot be forcibly killed; daemon threads exit when it returns.
                while True:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if item is not None:
                        item[0].cancel()
                    self._queue.task_done()
                for _ in self._threads:
                    self._queue.put_nowait(None)
        if wait:
            for thread in self._threads:
                thread.join()


class _HttpLoop(asyncio.SelectorEventLoop):
    def __init__(self, executor):
        super().__init__()
        self._resolver = executor

    def run_in_executor(self, executor, func, *args):
        _check_budget()
        future = super().run_in_executor(executor or self._resolver, func, *args)

        async def checked_result():
            result = await future
            # Recheck after uncancellable DNS returns, even if a ready callback races
            # the loop's timeout callback. Never continue an expired request.
            _check_budget()
            return result

        return asyncio.ensure_future(checked_result(), loop=self)


class HttpRuntime:
    def __init__(self, *, resolver_workers=4, resolver_pending=4, max_inflight=16):
        if not 1 <= resolver_workers <= resolver_pending <= 64 or not 1 <= max_inflight <= 64:
            raise ValueError("Invalid HTTP runtime capacity")
        self._executor = _ResolverExecutor(resolver_workers, resolver_pending)
        self._loop = _HttpLoop(self._executor)
        self._admission = threading.BoundedSemaphore(max_inflight)
        self._lock = threading.Lock()
        self._closed = False
        self._thread = threading.Thread(target=self._serve, name="commerce-http-loop", daemon=True)
        self._thread.start()

    def _serve(self):
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def run(self, factory, *, timeout):
        if timeout <= 0:
            raise TimeoutError("HTTP deadline exceeded")
        deadline = time.monotonic() + timeout
        # Reserve a small part of the caller's budget for transport cancellation.
        request_deadline = deadline - min(0.01, timeout / 10)
        cancelled = threading.Event()
        future = concurrent.futures.Future()
        with self._lock:
            if self._closed or not self._admission.acquire(blocking=False):
                raise TimeoutError("HTTP runtime unavailable")

            async def invoke():
                token = _BUDGET.set((request_deadline, cancelled))
                try:
                    _check_budget()
                    async with asyncio.timeout_at(request_deadline):
                        return await factory()
                finally:
                    _BUDGET.reset(token)

            def start():
                if self._closed or cancelled.is_set() or time.monotonic() >= request_deadline:
                    self._admission.release()
                    future.cancel()
                    return
                task = self._loop.create_task(invoke())

                def completed(task):
                    # Release only when the actual task finishes, including cancellation
                    # before its first coroutine step. The caller's Future can finish sooner.
                    self._admission.release()
                    try:
                        result = task.result()
                    except asyncio.CancelledError:
                        future.cancel()
                    except BaseException as exc:
                        if not future.done():
                            try:
                                future.set_exception(exc)
                            except concurrent.futures.InvalidStateError:
                                pass
                    else:
                        if not future.done():
                            try:
                                future.set_result(result)
                            except concurrent.futures.InvalidStateError:
                                pass

                def cancel_request(completion):
                    if completion.cancelled():
                        self._loop.call_soon_threadsafe(task.cancel)

                task.add_done_callback(completed)
                future.add_done_callback(cancel_request)

            self._loop.call_soon_threadsafe(start)
        try:
            return future.result(timeout=max(0, deadline - time.monotonic()))
        except concurrent.futures.TimeoutError:
            cancelled.set()
            future.cancel()
            raise TimeoutError("HTTP deadline exceeded") from None
        except concurrent.futures.CancelledError:
            raise TimeoutError("HTTP request cancelled") from None

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True

        async def drain():
            tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.wait(tasks, timeout=0.05)

        future = asyncio.run_coroutine_threadsafe(drain(), self._loop)
        try:
            future.result(timeout=0.1)
        except (TimeoutError, concurrent.futures.CancelledError):
            future.cancel()
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=0.05)
            self._executor.shutdown(wait=False, cancel_futures=True)


_runtime = None
_runtime_lock = threading.Lock()


def run_http(factory, *, timeout):
    global _runtime
    started = time.monotonic()
    with _runtime_lock:
        if _runtime is None:
            _runtime = HttpRuntime()
        runtime = _runtime
    return runtime.run(factory, timeout=timeout - (time.monotonic() - started))


def close_http_runtime():
    with _runtime_lock:
        runtime = _runtime
    if runtime is not None:
        runtime.close()


atexit.register(close_http_runtime)
