"""MinicapSource against a FAKE in-process minicap TCP server (no adb, no phone)."""

from __future__ import annotations

import contextlib
import io
import socket
import struct
import threading
import time
from collections.abc import Iterator

import numpy as np
import pytest
from PIL import Image as PILImage

from bot2048.device.capture import MinicapConfig
from bot2048.device.minicap import (
    BANNER_SIZE,
    MinicapBanner,
    MinicapBannerError,
    MinicapError,
    MinicapSource,
)

# -- wire-format helpers -------------------------------------------------------


def make_banner(
    *,
    version: int = 1,
    banner_size: int = BANNER_SIZE,
    pid: int = 1,
    real: tuple[int, int] = (1080, 2400),
    virtual: tuple[int, int] = (1080, 2400),
    orientation: int = 0,
    quirks: int = 0,
    extra: bytes = b"",
) -> bytes:
    packed = struct.pack(
        "<BBIIIIIBB",
        version,
        banner_size,
        pid,
        real[0],
        real[1],
        virtual[0],
        virtual[1],
        orientation,
        quirks,
    )
    return packed + extra


def make_jpeg(color: tuple[int, int, int], size: tuple[int, int] = (8, 8)) -> bytes:
    arr = np.zeros((size[1], size[0], 3), dtype=np.uint8)
    arr[:, :] = color
    buf = io.BytesIO()
    PILImage.fromarray(arr).save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def frame_packet(jpeg: bytes) -> bytes:
    return struct.pack("<I", len(jpeg)) + jpeg


RED = (255, 0, 0)
GREEN = (0, 255, 0)


def dominant_channel(frame: np.ndarray) -> int:
    return int(np.argmax(frame.reshape(-1, 3).mean(axis=0)))


# -- fake server ----------------------------------------------------------------


class FakeMinicapServer:
    """Speaks the minicap wire protocol from a scripted payload per connection.

    ``payloads[i]`` is sent verbatim on the i-th accepted connection; the
    connection is then closed if ``i in close_after`` else held open (so the
    client's freshness ``select`` sees silence, not EOF). ``chunk_size``
    trickles bytes to exercise partial-read handling.

    Connections beyond the script are accepted and closed at once (counted in
    ``surplus_connections``): if the fake simply stopped accepting, a
    reconnecting client would sit in the listen backlog for its whole
    ``connect_timeout_s`` and, once 5 such zombies queue up, Windows RSTs the
    next connect with ``WinError 10061`` (see ``test_scrcpy.py`` module doc).
    """

    def __init__(
        self,
        payloads: list[bytes],
        *,
        close_after: frozenset[int] = frozenset(),
        chunk_size: int | None = None,
        chunk_delay_s: float = 0.0,
    ) -> None:
        self._payloads = payloads
        self._close_after = close_after
        self._chunk_size = chunk_size
        self._chunk_delay_s = chunk_delay_s
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(5)
        self.port: int = self._listener.getsockname()[1]
        self.surplus_connections = 0
        self._held: list[socket.socket] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _accept(self) -> socket.socket | None:
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return None  # listener closed by stop()
        return conn

    def _serve(self) -> None:
        for index, payload in enumerate(self._payloads):
            conn = self._accept()
            if conn is None:
                return
            with contextlib.suppress(OSError):
                self._send(conn, payload)
            if index in self._close_after:
                with contextlib.suppress(OSError):
                    conn.close()
            else:
                self._held.append(conn)
        # Script exhausted: keep draining the accept queue so a late/extra
        # connection gets an immediate EOF instead of a backlog stall.
        while (conn := self._accept()) is not None:
            self.surplus_connections += 1
            with contextlib.suppress(OSError):
                conn.close()

    def _send(self, conn: socket.socket, payload: bytes) -> None:
        if self._chunk_size is None:
            conn.sendall(payload)
            return
        for i in range(0, len(payload), self._chunk_size):
            conn.sendall(payload[i : i + self._chunk_size])
            if self._chunk_delay_s:
                time.sleep(self._chunk_delay_s)

    def stop(self) -> None:
        with contextlib.suppress(OSError):
            self._listener.close()
        for conn in self._held:
            with contextlib.suppress(OSError):
                conn.close()
        self._thread.join(timeout=2.0)


@contextlib.contextmanager
def serving(server: FakeMinicapServer) -> Iterator[FakeMinicapServer]:
    try:
        yield server
    finally:
        server.stop()


class FakeRunner:
    """Records the adb-side lifecycle calls; touches nothing."""

    def __init__(self) -> None:
        self.projections: list[str] = []
        self.forwards: list[int] = []
        self.removed: list[int] = []
        self.stopped = 0

    def start_minicap(self, projection: str) -> None:
        self.projections.append(projection)

    def forward(self, local_port: int) -> None:
        self.forwards.append(local_port)

    def remove_forward(self, local_port: int) -> None:
        self.removed.append(local_port)

    def stop_minicap(self) -> None:
        self.stopped += 1


def _config(port: int, **overrides: object) -> MinicapConfig:
    defaults: dict[str, object] = {
        "local_port": port,
        "connect_attempts": 3,
        "connect_retry_delay_s": 0.01,
        "connect_timeout_s": 2.0,
        "read_timeout_s": 2.0,
        "reconnect_attempts": 2,
    }
    defaults.update(overrides)
    return MinicapConfig(**defaults)  # type: ignore[arg-type]


# -- banner parsing --------------------------------------------------------------


def test_banner_parse_fields() -> None:
    raw = make_banner(pid=4242, real=(1080, 2400), virtual=(540, 1200), orientation=90, quirks=2)
    banner = MinicapBanner.parse(raw)
    assert banner.version == 1
    assert banner.banner_size == BANNER_SIZE
    assert banner.pid == 4242
    assert (banner.real_width, banner.real_height) == (1080, 2400)
    assert (banner.virtual_width, banner.virtual_height) == (540, 1200)
    assert banner.orientation == 90
    assert banner.quirks == 2


def test_banner_rejects_wrong_version() -> None:
    with pytest.raises(MinicapBannerError, match="version 2"):
        MinicapBanner.parse(make_banner(version=2))


def test_banner_rejects_wrong_length() -> None:
    with pytest.raises(MinicapBannerError, match="24 bytes"):
        MinicapBanner.parse(make_banner()[:-1])


# -- stream behaviour -------------------------------------------------------------


def test_start_parses_banner_and_capture_decodes_frame() -> None:
    payload = make_banner(pid=7) + frame_packet(make_jpeg(RED))
    with serving(FakeMinicapServer([payload])) as server:
        runner = FakeRunner()
        with MinicapSource(_config(server.port), runner=runner) as source:
            assert source.banner is not None
            assert source.banner.pid == 7
            assert (source.banner.real_width, source.banner.real_height) == (1080, 2400)
            frame = source.capture()
        assert frame.dtype == np.uint8
        assert frame.shape == (8, 8, 3)
        assert dominant_channel(frame) == 0  # red, i.e. RGB order (not BGR)
        assert runner.projections == ["1080x2400@1080x2400/0"]
        assert runner.forwards == [server.port]


def test_capture_returns_latest_buffered_frame() -> None:
    payload = make_banner() + frame_packet(make_jpeg(RED)) + frame_packet(make_jpeg(GREEN))
    with serving(FakeMinicapServer([payload])) as server:
        with MinicapSource(_config(server.port), runner=FakeRunner()) as source:
            frame = source.capture()
        assert dominant_channel(frame) == 1  # green: the newest of the two


def test_partial_reads_are_reassembled() -> None:
    payload = make_banner(pid=3) + frame_packet(make_jpeg(GREEN))
    server = FakeMinicapServer([payload], chunk_size=3, chunk_delay_s=0.001)
    with serving(server):
        with MinicapSource(_config(server.port), runner=FakeRunner()) as source:
            assert source.banner is not None
            assert source.banner.pid == 3
            frame = source.capture()
        assert frame.shape == (8, 8, 3)
        assert dominant_channel(frame) == 1


def test_reconnects_after_stream_drop() -> None:
    first = make_banner(pid=1) + frame_packet(make_jpeg(RED))
    second = make_banner(pid=2) + frame_packet(make_jpeg(GREEN))
    server = FakeMinicapServer([first, second], close_after=frozenset({0}))
    with (
        serving(server),
        MinicapSource(_config(server.port), runner=FakeRunner()) as source,
    ):
        assert dominant_channel(source.capture()) == 0  # red from connection 1
        assert dominant_channel(source.capture()) == 1  # green after reconnect
        assert source.banner is not None
        assert source.banner.pid == 2  # fresh handshake happened


def test_banner_extension_bytes_are_skipped() -> None:
    payload = make_banner(banner_size=BANNER_SIZE + 2, extra=b"\x00\x00")
    payload += frame_packet(make_jpeg(RED))
    with (
        serving(FakeMinicapServer([payload])) as server,
        MinicapSource(_config(server.port), runner=FakeRunner()) as source,
    ):
        assert source.capture().shape == (8, 8, 3)


def test_bad_banner_version_fails_start() -> None:
    payload = make_banner(version=2) + frame_packet(make_jpeg(RED))
    with serving(FakeMinicapServer([payload])) as server:
        runner = FakeRunner()
        source = MinicapSource(_config(server.port), runner=runner)
        with pytest.raises(MinicapBannerError):
            source.start()
        # failed start must still have cleaned up the adb side
        assert runner.stopped == 1
        assert runner.removed == [server.port]


def test_close_releases_process_forward_and_socket() -> None:
    payload = make_banner() + frame_packet(make_jpeg(RED))
    with serving(FakeMinicapServer([payload])) as server:
        runner = FakeRunner()
        source = MinicapSource(_config(server.port), runner=runner)
        source.start()
        source.close()
        source.close()  # idempotent
        assert runner.stopped == 1
        assert runner.removed == [server.port]
        assert source.banner is None
        with pytest.raises(MinicapError, match="not started"):
            source.capture()


def test_capture_before_start_raises() -> None:
    source = MinicapSource(_config(1313), runner=FakeRunner())
    with pytest.raises(MinicapError, match="not started"):
        source.capture()


def test_connect_failure_raises_minicap_error() -> None:
    # grab a port with no listener on it
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()
    source = MinicapSource(
        _config(dead_port, connect_attempts=2, connect_timeout_s=0.5),
        runner=FakeRunner(),
    )
    with pytest.raises(MinicapError, match="cannot connect"):
        source.start()


def test_fake_server_drains_connections_past_its_script() -> None:
    """Connecting past the script fails FAST with a clean EOF, not a backlog stall.

    Each attempt is accepted and closed at once ("connection closed
    mid-read" while reading the banner), so the whole start() costs
    milliseconds -- not ``connect_attempts x connect_timeout_s`` of zombies in
    the listen backlog followed by ``WinError 10061`` (see the class doc).
    """
    runner = FakeRunner()
    with serving(FakeMinicapServer([])) as server:
        source = MinicapSource(_config(server.port, connect_attempts=4), runner=runner)
        started = time.monotonic()
        with pytest.raises(MinicapError, match=r"cannot connect.*closed mid-read"):
            source.start()
        elapsed = time.monotonic() - started
        assert server.surplus_connections == 4
    assert elapsed < 5.0  # far below 4 x read_timeout_s (8 s) a backlog stall costs
    assert runner.stopped == 1
    assert runner.removed == [server.port]


def test_projection_uses_virtual_size_when_set() -> None:
    payload = make_banner(virtual=(540, 1200)) + frame_packet(make_jpeg(RED, size=(4, 4)))
    with serving(FakeMinicapServer([payload])) as server:
        runner = FakeRunner()
        config = _config(server.port, virtual_size=(540, 1200))
        with MinicapSource(config, runner=runner) as source:
            assert source.capture().shape == (4, 4, 3)
        assert runner.projections == ["1080x2400@540x1200/0"]
