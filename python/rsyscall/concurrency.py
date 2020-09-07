"Miscellaneous concurrency-management utilities."
from __future__ import annotations
import trio
import contextlib
from dataclasses import dataclass
import typing as t
import types
import outcome

@dataclass
class OneAtATime:
    """Used as part of multiplexing APIs.

    This class is used to control access to the core work loop of
    multiplexing APIs.

    In a multiplexing API, multiple coroutines want to wait for some
    event; one of the waiting coroutines should be selected to perform
    the actual work of polling for the event.

    A multiplexing API will make a OneAtATime, and when each coroutine
    wants to wait for some event, they will enter the needs_run async
    context manager.

    If needs_run yields true, then that coroutine is the first to be
    waiting, and they need to do the actual work.

    If needs_run yields false, then they should just do nothing; this
    is accomlished with an if-condition. The needs-run context manager
    will handle waiting for the working coroutine to complete their
    work.

    This is different from a lock in that once the coroutine doing the
    work has released the lock, *all* waiting threads are woken up
    instead of just one, like a condition variable. This is important
    because any of the waiting coroutine might have had their work
    done, and no longer need to wait.

    This is different from both a condition variable and a lock in
    that when the threads are woken up, they're informed whether
    someone has already done some work. This is important in our use
    case, where the same coroutines that are waiting for work to be
    done, are also the ones doing the work.

    You could add this information to a condition variable, but it
    would be a separate bit of state that you'd have to maintain; this
    class abstracts it away.

    This is a terrible API, but it works for now.

    A better API would be a "shared coroutine" which runs whenever any
    other coroutine is waiting on it, and is suspended if no other
    coroutine is waiting on it. A shared coroutine also must not
    require entering a contextmanager to create. We should try to get
    that kind of API merged into trio/asyncio.

    This is basically the primitive we need to do "combining", ala
    "flat combining".

    """
    running: t.Optional[trio.Event] = None

    @contextlib.asynccontextmanager
    async def needs_run(self) -> t.AsyncGenerator[bool, None]:
        "Yield a bool indiciating whether the caller should perform the actual work controlled by this OneAtATime."
        if self.running is not None:
            yield False
            await self.running.wait()
        else:
            running = trio.Event()
            self.running = running
            try:
                yield True
            finally:
                self.running = None
                running.set()

class MultiplexedEvent:
    """A one-shot event which, when waited on, selects one waiter to run a callable until it completes.

    The point of this class is that we have multiple callers wanting to wait on the
    completion of a single callable; there's no dedicated thread to run the callable,
    instead it's run directly on the stack of one of the callers. The callable might be
    cancelled, but it will keep being re-run until it successfully completes. Then this
    event is complete; a new one may be created with a new or same callable.

    """
    def __init__(self, try_running: t.Callable[[], t.Awaitable[None]]) -> None:
        self.flag = False
        self.try_running = try_running
        self.one_at_a_time = OneAtATime()

    async def wait(self) -> None:
        "Wait until this event is done, possibly performing work on the event if necessary."
        while not self.flag:
            async with self.one_at_a_time.needs_run() as needs_run:
                if needs_run:
                    # if we successfully complete this call, we set the flag;
                    # exceptions get propagated up to some arbitrary unlucky caller.
                    await self.try_running()
                    self.flag = True

T = t.TypeVar('T')
async def make_n_in_parallel(make: t.Callable[[], t.Awaitable[T]], count: int) -> t.List[T]:
    "Call `make` n times in parallel, and return all the results."
    pairs: t.List[t.Any] = [None]*count
    async with trio.open_nursery() as nursery:
        async def open_nth(n: int) -> None:
            pairs[n] = await make()
        for i in range(count):
            nursery.start_soon(open_nth, i)
    return pairs

async def run_all(callables: t.List[t.Callable[[], t.Awaitable[T]]]) -> t.List[T]:
    "Call all the functions passed to it, and return all the results."
    count = len(callables)
    results: t.List[t.Any] = [None]*count
    async with trio.open_nursery() as nursery:
        async def open_nth(n: int) -> None:
            results[n] = await callables[n]()
        for i in range(count):
            nursery.start_soon(open_nth, i)
    return results

@dataclass
class Future(t.Generic[T]):
    "A value that we might have to wait for."
    _outcome: t.Optional[outcome.Outcome]
    _event: trio.Event

    async def get(self) -> T:
        await self._event.wait()
        assert self._outcome is not None
        return self._outcome.unwrap()

@dataclass
class Promise(t.Generic[T]):
    "Our promise to provide a value for some Future."
    _future: Future[T]

    def _check_not_set(self) -> None:
        if self._future._outcome is not None:
            raise Exception("Future is already set to", self._future._outcome)

    def send(self, val: T) -> None:
        self._check_not_set()
        self._future._outcome = outcome.Value(val)
        self._future._event.set()

    def throw(self, exn: BaseException) -> None:
        self._check_not_set()
        self._future._outcome = outcome.Error(exn)
        self._future._event.set()

    def set(self, oc: outcome.Outcome) -> None:
        self._check_not_set()
        self._future._outcome = oc
        self._future._event.set()

def make_future() -> t.Tuple[Future, Promise]:
    fut = Future[T](None, trio.Event())
    return fut, Promise(fut)

@dataclass
class FIFOFuture(t.Generic[T]):
    "A value that we might have to wait for."
    _outcome: t.Optional[outcome.Outcome]
    _event: trio.Event
    _retrieved: trio.Event
    _cancel_scope: t.Optional[trio.CancelScope]

    @staticmethod
    def make() -> t.Tuple[FIFOFuture, FIFOPromise]:
        fut = FIFOFuture[T](None, trio.Event(), trio.Event(), None)
        return fut, FIFOPromise(fut)

    async def get(self) -> T:
        await self._event.wait()
        assert self._outcome is not None
        return self._outcome.unwrap()

    def set_retrieved(self):
        if self._cancel_scope:
            self._cancel_scope.cancel()
        self._retrieved.set()

@dataclass
class FIFOPromise(t.Generic[T]):
    "Our promise to provide a value for some Future."
    _future: FIFOFuture[T]

    def set(self, oc: outcome.Outcome) -> None:
        if self._future._outcome is not None:
            raise Exception("Future is already set to", self._future._outcome)
        self._future._outcome = oc
        self._future._event.set()

    async def wait_for_retrieval(self) -> None:
        await self._future._retrieved.wait()

    def set_cancel_scope(self, cancel_scope: trio.CancelScope) -> None:
        self._future._cancel_scope = cancel_scope

@types.coroutine
def _yield(value: t.Any) -> t.Any:
    return (yield value)

@dataclass
class DynvarRequest:
    prompt: Dynvar

class Dynvar(t.Generic[T]):
    async def get(self) -> t.Optional[t.Any]:
        try:
            return await _yield(DynvarRequest(self))
        except (RuntimeError, TypeError) as e:
            # These are what asyncio and trio, respectively, inject on violating the yield protocol
            return None

    async def bind(self, value: T, coro: t.Coroutine) -> t.Any:
        send_value: outcome.Outcome = outcome.Value(None)
        while True:
            try: yield_value = send_value.send(coro)
            except StopIteration as e: return e.value
            # handle DynvarRequests for this dynvar, and yield everything else up
            if isinstance(yield_value, DynvarRequest) and yield_value.prompt is self:
                send_value = outcome.Value(value)
            else:
                send_value = (await outcome.acapture(_yield, yield_value))

@dataclass
class SuspendRequest:
    prompt: SuspendableCoroutine
    cancels: t.List[trio.Cancelled]

class SuspendableCoroutine:
    def __init__(self, run_func: t.Callable[[SuspendableCoroutine], t.Coroutine]) -> None:
        self._coro: t.Coroutine = run_func(self)
        self._run_func = run_func
        self._lock = trio.Lock()

    def __del__(self) -> None:
        # suppress the warning about unawaited coroutine that we'd get
        # if we never got the chance to drive this coro
        try:
            self._coro.close()
        except RuntimeError as e:
            if "generator didn't stop after throw" in str(e):
                # hack-around pending python 3.7.9 upgrade
                pass
            else:
                raise

    async def drive(self) -> None:
        async with self._lock:
            await trio.sleep(0)
            send_value: outcome.Outcome = outcome.Value(None)
            while True:
                try: yield_value = send_value.send(self._coro)
                except StopIteration as e: return e.value
                # handle SuspendRequests for us, and yield everything else up
                if isinstance(yield_value, SuspendRequest) and yield_value.prompt is self:
                    raise trio.MultiError(yield_value.cancels)
                else:
                    send_value = (await outcome.acapture(_yield, yield_value))

    @contextlib.asynccontextmanager
    async def running(self) -> t.AsyncIterator[None]:
        done = False
        def handle_cancelled(exn: BaseException) -> t.Optional[BaseException]:
            if isinstance(exn, trio.Cancelled) and done:
                return None
            else:
                return exn
        with trio.MultiError.catch(handle_cancelled):
            async with trio.open_nursery() as nursery:
                nursery.start_soon(self.drive)
                yield
                done = True
                nursery.cancel_scope.cancel()

    @contextlib.asynccontextmanager
    async def suspend_if_cancelled(self) -> t.AsyncIterator[None]:
        cancels = []
        def handle_cancelled(exn: BaseException) -> t.Optional[BaseException]:
            if isinstance(exn, trio.Cancelled):
                cancels.append(exn)
                return None
            else:
                return exn
        with trio.MultiError.catch(handle_cancelled):
            yield
        if cancels:
            await _yield(SuspendRequest(self, cancels))

    async def with_running(self, func: t.Callable[[], t.Awaitable[t.Any]]) -> t.Any:
        async with self.running():
            return await func()

    async def wait(self, func: t.Callable[[], t.Any]) -> t.Any:
        while True:
            async with self.suspend_if_cancelled():
                return await func()
