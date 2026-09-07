"""The IPC dial must not be able to hang forever.

kicad-python dials with ``pynng.Req0(block_on_dial=True)``. Its send/recv timeouts
bound individual messages, but the dial itself blocks indefinitely when the socket
file exists and KiCad is not servicing it — a modal dialog or a busy editor is
enough. Observed in practice as a unit run stuck for 12 minutes on 3 seconds of
CPU, parked in ``nni_plat_cv_wait``.
"""

from __future__ import annotations

import threading
import time

import pytest

from kicad_mcp.errors import KiCadConnectionTimeoutError, KiCadNotRunningError
from kicad_mcp.kicad.session import KiCadSession


class _Config:
    """Minimal stand-in for the runtime config the session reads."""

    def __init__(self, timeout: float = 0.2, retries: int = 0) -> None:
        self.ipc_connection_timeout = timeout
        self.ipc_retries = retries
        self.ipc_cache_ttl = 5.0
        self.kicad_socket_path = None
        self.kicad_token = ""
        self.client_name = "test"
        self.timeout_ms = int(timeout * 1000)


def _session(factory, timeout: float = 0.2, retries: int = 0) -> KiCadSession:
    return KiCadSession(
        client_factory=factory,
        logger=None,
        config_factory=lambda: _Config(timeout, retries),
        sleep=lambda _seconds: None,
    )


class _Client:
    def __init__(self, **_kwargs: object) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_a_hanging_dial_raises_instead_of_blocking() -> None:
    """The whole point: a dial that never returns must not stall the caller."""
    started = threading.Event()

    def never_connects(**_kwargs: object) -> object:
        started.set()
        time.sleep(30)  # stands in for pynng's uninterruptible block_on_dial
        return _Client()

    session = _session(never_connects, timeout=0.2)
    begin = time.monotonic()
    with pytest.raises(KiCadConnectionTimeoutError, match="not servicing it"):
        session.client()
    elapsed = time.monotonic() - begin

    assert started.is_set()
    assert elapsed < 5.0, f"gave up after {elapsed:.1f}s — the deadline did not apply"


def test_a_healthy_dial_still_returns_its_client() -> None:
    client = _Client()
    session = _session(lambda **_kwargs: client, timeout=5.0)
    assert session.client() is client


def test_a_slow_but_in_time_dial_succeeds() -> None:
    def slow(**_kwargs: object) -> object:
        time.sleep(0.05)
        return _Client()

    session = _session(slow, timeout=2.0)
    assert isinstance(session.client(), _Client)


def test_a_late_arriving_connection_is_closed_not_leaked() -> None:
    """If the dial completes after we gave up, that socket must not be left open."""
    created: list[_Client] = []
    release = threading.Event()

    def late(**_kwargs: object) -> object:
        release.wait(5.0)
        client = _Client()
        created.append(client)
        return client

    session = _session(late, timeout=0.1)
    with pytest.raises(KiCadConnectionTimeoutError):
        session.client()

    release.set()
    deadline = time.monotonic() + 5.0
    while not created and time.monotonic() < deadline:
        time.sleep(0.02)

    assert created, "the abandoned dial never completed"
    deadline = time.monotonic() + 2.0
    while not created[0].closed and time.monotonic() < deadline:
        time.sleep(0.02)
    assert created[0].closed, "late connection was leaked instead of closed"


def test_the_dial_thread_is_a_daemon_so_it_cannot_block_exit() -> None:
    def never_connects(**_kwargs: object) -> object:
        time.sleep(30)
        return _Client()

    session = _session(never_connects, timeout=0.1)
    with pytest.raises(KiCadConnectionTimeoutError):
        session.client()

    dialers = [t for t in threading.enumerate() if t.name == "kicad-ipc-dial"]
    assert dialers, "expected the abandoned dial thread to still exist"
    assert all(t.daemon for t in dialers), "a non-daemon dial thread would block interpreter exit"


def test_a_dial_that_raises_propagates_the_original_error() -> None:
    def refuses(**_kwargs: object) -> object:
        raise ConnectionRefusedError("socket refused")

    session = _session(refuses, timeout=1.0, retries=0)
    with pytest.raises(KiCadNotRunningError) as caught:
        session.client()
    # A refused socket is a disconnect, not our dial deadline.
    assert not isinstance(caught.value, KiCadConnectionTimeoutError)


def test_each_retry_gets_its_own_deadline() -> None:
    """Retries must stay bounded: N attempts of at most the timeout each."""
    attempts = 0

    def never_connects(**_kwargs: object) -> object:
        nonlocal attempts
        attempts += 1
        time.sleep(30)
        return _Client()

    session = _session(never_connects, timeout=0.1, retries=2)
    begin = time.monotonic()
    with pytest.raises(KiCadConnectionTimeoutError):
        session.client()
    elapsed = time.monotonic() - begin

    assert attempts == 3, f"expected 3 attempts, saw {attempts}"
    assert elapsed < 5.0, f"three bounded attempts took {elapsed:.1f}s"
