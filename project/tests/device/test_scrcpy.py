"""ScrcpySource against a FAKE stream server + REAL host ffmpeg (no adb, no phone).

The fake TCP server plays canned H.264 clips (encoded on the fly with the
host ffmpeg -- the same binary the source uses for decoding); a fake runner
replaces the adb side, so no device is touched. Tests that need ffmpeg are
skipped if it is not on PATH.

Elementary-stream caveat baked into the assertions: without per-frame
framing, ffmpeg's demux/parse pipeline holds the newest access units until
further bytes (or EOF) arrive. Usually that is ~2 frames, but the exact
count depends on how bytes group into demuxer reads (timing!) and, under
load, this ffmpeg build occasionally holds the ENTIRE short clip (measured:
0 of 6 frames out of a held-open stream in ~1-3% of pipelines, all 6 on
EOF). The real device never leaves ffmpeg hanging like that: scrcpy-server
configures MediaCodec with ``KEY_REPEAT_PREVIOUS_FRAME_AFTER = 100 ms``, so a
static screen keeps re-emitting its last frame. :class:`FakeStreamServer`
models exactly that -- a held-open connection repeats the payload's last
frame every :data:`KEEPALIVE_INTERVAL_S` -- which is what makes "capture a
frame from a static stream" deterministic here. EOF (``close_after``)
flushes everything (decode is lossless -- see the ``-fflags nobuffer`` note
in ``device/scrcpy.py``).

Windows caveat for the fake's accept loop: once the scripted payloads are
exhausted the fake keeps accepting and immediately closes surplus
connections. If it simply stopped accepting, a reconnecting client would
complete the TCP handshake into the listen backlog and then wait the whole
``connect_timeout_s`` for a byte that never comes -- and after 5 such
zombies Windows RSTs the 6th connect with ``WinError 10061`` ("actively
refused"), which is how a 100 ms decode stall used to surface as an 80 s
"cannot connect" failure.
"""

from __future__ import annotations

import contextlib
import functools
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Iterator

import numpy as np
import pytest

from bot2048.device.capture import ScrcpyConfig, make_frame_source
from bot2048.device.minicap import MinicapSource
from bot2048.device.scrcpy import (
    ScrcpyError,
    ScrcpySecureError,
    ScrcpySource,
    _is_black_frame,
)

FFMPEG = shutil.which("ffmpeg")
requires_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not on PATH")

SIZE = (64, 64)  # (width, height) -- keep tiny so encode/decode is instant
RED = (255, 0, 0)
GREEN = (0, 255, 0)
BLACK = (0, 0, 0)

#: scrcpy-server's ``MediaFormat.KEY_REPEAT_PREVIOUS_FRAME_AFTER`` (100 ms): how
#: often a held-open fake connection re-sends its last frame (see module doc).
KEEPALIVE_INTERVAL_S = 0.1

#: Annex B 4-byte start code + SPS NAL header (nal_ref_idc=3, type 7) -- every
#: frame of our ``-g 1`` clips is an IDR preceded by repeated SPS/PPS, so this
#: marks each access-unit boundary.
_SPS_START = b"\x00\x00\x00\x01\x67"


# -- clip + polling helpers ------------------------------------------------------


@functools.cache
def encode_clip(colors: tuple[tuple[int, int, int], ...]) -> bytes:
    """Solid-color frames -> raw H.264 elementary stream (real host ffmpeg)."""
    assert FFMPEG is not None
    width, height = SIZE
    raw = b"".join(bytes(color) * (width * height) for color in colors)
    command = [
        FFMPEG,
        "-hide_banner",
        "-loglevel", "error",
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "-s", f"{width}x{height}",
        "-r", "30",
        "-i", "pipe:0",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-g", "1",
        "-pix_fmt", "yuv420p",
        "-f", "h264",
        "pipe:1",
    ]
    result = subprocess.run(command, input=raw, capture_output=True, check=True)
    assert result.stdout, f"ffmpeg produced no bitstream: {result.stderr.decode()}"
    return result.stdout


def last_access_unit(payload: bytes) -> bytes:
    """The final frame (SPS + PPS + IDR slice) of an :func:`encode_clip` payload.

    Re-sending it is a bit-exact model of the device encoder repeating the
    previous frame on a static screen. ``b""`` when the payload carries no
    such frame (opaque garbage payloads) -- callers then send no keepalive.
    """
    index = payload.rfind(_SPS_START)
    return payload[index:] if index >= 0 else b""


def dominant_channel(frame: np.ndarray) -> int:
    return int(np.argmax(frame.reshape(-1, 3).mean(axis=0)))


def wait_until(
    predicate: Callable[[], bool], *, timeout: float = 25.0, interval: float = 0.02
) -> None:
    # Generous default: the happy path returns in ~1 s, but a real host ffmpeg
    # can be slow to *start* when many decode-backed tests run back-to-back on
    # Windows (measured: steady-state decode is fast; only process startup
    # occasionally stalls a few seconds). A large ceiling only affects that
    # pathological slow-start, turning a spurious flake into a pass.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


# -- fake stream server + fake runner ---------------------------------------------


class FakeStreamServer:
    """Serves one scripted byte payload per accepted connection.

    ``payloads[i]`` goes verbatim to the i-th connection, which is then
    closed if ``i in close_after`` (EOF -> decoder flush) else held open like
    a static board -- and, like the device encoder, a held-open connection
    keeps re-sending the payload's last frame every
    :data:`KEEPALIVE_INTERVAL_S` (``repeat_last_frame=False`` gives pure
    silence instead). ``chunk_size`` trickles bytes to exercise partial-read
    reassembly.

    Connections beyond the script are accepted and closed at once (counted in
    ``surplus_connections``) so a client that reconnects past the script fails
    fast with a clean EOF rather than stalling in the listen backlog (module
    doc, Windows caveat).
    """

    def __init__(
        self,
        payloads: list[bytes],
        *,
        close_after: frozenset[int] = frozenset(),
        chunk_size: int | None = None,
        chunk_delay_s: float = 0.0,
        repeat_last_frame: bool = True,
    ) -> None:
        self._payloads = payloads
        self._close_after = close_after
        self._chunk_size = chunk_size
        self._chunk_delay_s = chunk_delay_s
        self._repeat_last_frame = repeat_last_frame
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(5)
        self.port: int = self._listener.getsockname()[1]
        self.surplus_connections = 0
        self._held: list[socket.socket] = []
        self._stopping = threading.Event()
        self._keepalive_threads: list[threading.Thread] = []
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
                continue
            self._held.append(conn)
            frame = last_access_unit(payload) if self._repeat_last_frame else b""
            if frame:
                thread = threading.Thread(target=self._keepalive, args=(conn, frame), daemon=True)
                self._keepalive_threads.append(thread)
                thread.start()
        # Script exhausted: keep draining the accept queue so a late/extra
        # connection gets an immediate EOF instead of a backlog stall.
        while (conn := self._accept()) is not None:
            self.surplus_connections += 1
            with contextlib.suppress(OSError):
                conn.close()

    def _keepalive(self, conn: socket.socket, frame: bytes) -> None:
        """Re-send ``frame`` until stop() or the peer goes away."""
        while not self._stopping.wait(KEEPALIVE_INTERVAL_S):
            try:
                self._send(conn, frame)
            except OSError:
                return  # client closed its end (pipeline teardown / death)

    def _send(self, conn: socket.socket, payload: bytes) -> None:
        if self._chunk_size is None:
            conn.sendall(payload)
            return
        for i in range(0, len(payload), self._chunk_size):
            conn.sendall(payload[i : i + self._chunk_size])
            if self._chunk_delay_s:
                time.sleep(self._chunk_delay_s)

    def stop(self) -> None:
        self._stopping.set()
        with contextlib.suppress(OSError):
            self._listener.close()
        for conn in self._held:
            with contextlib.suppress(OSError):
                conn.close()
        self._thread.join(timeout=2.0)
        for thread in self._keepalive_threads:
            thread.join(timeout=2.0)


@contextlib.contextmanager
def serving(server: FakeStreamServer) -> Iterator[FakeStreamServer]:
    try:
        yield server
    finally:
        server.stop()


class FakeRunner:
    """Records the adb-side lifecycle calls; touches nothing."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.forwards: list[tuple[int, str]] = []
        self.removed: list[int] = []
        self.stopped = 0

    def start_server(self, shell_command: str) -> None:
        self.commands.append(shell_command)

    def forward(self, local_port: int, socket_name: str) -> None:
        self.forwards.append((local_port, socket_name))

    def remove_forward(self, local_port: int) -> None:
        self.removed.append(local_port)

    def stop_server(self) -> None:
        self.stopped += 1


def _config(port: int, **overrides: object) -> ScrcpyConfig:
    # Generous default timeouts: under xdist the other workers saturate the CPU,
    # so ffmpeg spawn can be slow and a short client-side connect timeout may
    # abandon a handshake the kernel later completes -- that zombie connection
    # then eats a scripted FakeStreamServer payload and desyncs the sequence.
    # Tests that exercise timeout/failure paths override these explicitly.
    defaults: dict[str, object] = {
        "local_port": port,
        "size": SIZE,
        "connect_attempts": 3,
        "connect_retry_delay_s": 0.01,
        "connect_timeout_s": 10.0,
        "first_frame_timeout_s": 30.0,
        "reconnect_attempts": 2,
    }
    defaults.update(overrides)
    return ScrcpyConfig(**defaults)  # type: ignore[arg-type]


# -- config validation -------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"local_port": 0},
        {"local_port": 65536},
        {"scid": "1234567"},  # 7 chars
        {"scid": "012345678"},  # 9 chars
        {"scid": "xyz45678"},  # not hex
        {"scid": "ffffffff"},  # > 7fffffff (signed-int parse on device)
        {"size": (0, 2400)},
        {"video_bit_rate": 0},
        {"max_fps": 0},
        {"connect_attempts": 0},
        {"reconnect_attempts": -1},
        {"connect_timeout_s": 0.0},
        {"first_frame_timeout_s": 0.0},
        {"connect_retry_delay_s": -0.1},
    ],
)
def test_config_rejects_bad_values(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ScrcpyConfig(**overrides)  # type: ignore[arg-type]


def test_config_socket_name_and_frame_bytes() -> None:
    cfg = ScrcpyConfig(size=(1080, 2400))
    assert cfg.socket_name == "scrcpy_01234567"
    assert cfg.frame_bytes == 1080 * 2400 * 3


def test_server_and_ffmpeg_commands_match_verified_recipe() -> None:
    source = ScrcpySource(ScrcpyConfig(), runner=FakeRunner())
    command = source.server_command
    assert command.startswith(
        "CLASSPATH=/data/local/tmp/scrcpy-server app_process / "
        "com.genymobile.scrcpy.Server 4.0 "
    )
    for token in (
        "scid=01234567",
        "log_level=info",
        "audio=false",
        "control=false",
        "cleanup=false",
        "raw_stream=true",
        "send_dummy_byte=false",
        "max_size=0",
        "video_codec=h264",
        "tunnel_forward=true",
        "video_bit_rate=8000000",
        "max_fps=60",
    ):
        assert token in command
    decode = source.ffmpeg_command
    assert decode[0] == "ffmpeg"
    joined = " ".join(decode)
    for pair in (
        ("-probesize", "32"),
        ("-flags", "low_delay"),
        ("-f", "h264"),
        ("-i", "pipe:0"),
        ("-f", "rawvideo"),
        ("-pix_fmt", "rgb24"),
    ):
        assert " ".join(pair) in joined
    # Regression guard: nobuffer discards the packets consumed during
    # probing (frames lost / nothing decoded on a held-open stream).
    assert "nobuffer" not in joined
    assert decode[-1] == "pipe:1"


# -- lifecycle without ffmpeg -------------------------------------------------------


def test_capture_before_start_raises() -> None:
    source = ScrcpySource(_config(port=1), runner=FakeRunner())
    with pytest.raises(ScrcpyError, match="not started"):
        source.capture()


def test_connect_failure_cleans_up_server_and_forward() -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    dead_port: int = probe.getsockname()[1]
    probe.close()  # nothing listens there now
    runner = FakeRunner()
    source = ScrcpySource(
        _config(dead_port, connect_attempts=2, connect_timeout_s=0.5), runner=runner
    )
    with pytest.raises(ScrcpyError, match="cannot connect"):
        source.start()
    assert runner.stopped == 1
    assert runner.removed == [dead_port]
    assert not source.pipeline_alive


# -- decode pipeline against real ffmpeg --------------------------------------------


@requires_ffmpeg
def test_start_capture_and_full_teardown() -> None:
    payload = encode_clip((RED,) * 6)
    runner = FakeRunner()
    baseline = set(threading.enumerate())
    with serving(FakeStreamServer([payload])) as server:
        with ScrcpySource(_config(server.port), runner=runner) as source:
            frame = source.capture()
            assert frame.shape == (SIZE[1], SIZE[0], 3)
            assert frame.dtype == np.uint8
            assert dominant_channel(frame) == 0  # red
            assert source.pipeline_alive
        # closed: adb side torn down exactly once, pipeline gone
        assert runner.forwards == [(server.port, "scrcpy_01234567")]
        assert len(runner.commands) == 1
        assert runner.stopped == 1
        assert runner.removed == [server.port]
        # Local binding: sidesteps mypy's member narrowing from the assert above.
        alive_after_close = bool(source.pipeline_alive)
        assert not alive_after_close
        with pytest.raises(ScrcpyError, match="not started"):
            source.capture()
        source.close()  # idempotent
        assert runner.stopped == 1
    wait_until(lambda: set(threading.enumerate()) <= baseline, timeout=5.0)


@requires_ffmpeg
def test_capture_returns_latest_frame_not_first() -> None:
    # Held-open connection (static-board silence). The first 2 frames are red,
    # the rest green; the pipeline holds only the newest ~2-3 access units, so
    # once >= 3 are out "latest" is green, never the first red frame. The clip
    # is intentionally long: with the source's tiny -probesize, a very short
    # held-open clip can intermittently stall in ffmpeg's rate probe under
    # back-to-back-test load (measured), so extra frames keep the probe fed.
    payload = encode_clip((RED, RED) + (GREEN,) * 14)
    server = FakeStreamServer([payload])
    with serving(server), ScrcpySource(_config(server.port), runner=FakeRunner()) as source:
        wait_until(lambda: source.frames_decoded >= 3)
        assert dominant_channel(source.capture()) == 1  # green


@requires_ffmpeg
def test_chunked_trickled_stream_reassembles() -> None:
    payload = encode_clip((RED, RED) + (GREEN,) * 14)
    server = FakeStreamServer([payload], chunk_size=64, chunk_delay_s=0.0005)
    with serving(server), ScrcpySource(_config(server.port), runner=FakeRunner()) as source:
        wait_until(lambda: source.frames_decoded >= 3)
        assert dominant_channel(source.capture()) == 1


@requires_ffmpeg
def test_eof_flushes_all_frames_through_decoder() -> None:
    # close_after -> EOF -> ffmpeg flush: ALL 3 frames must come out.
    payload = encode_clip((RED, RED, GREEN))
    server = FakeStreamServer([payload], close_after=frozenset({0}))
    with serving(server), ScrcpySource(_config(server.port), runner=FakeRunner()) as source:
        wait_until(lambda: source.frames_decoded >= 3)


@requires_ffmpeg
def test_reconnects_after_stream_death() -> None:
    first = encode_clip((RED, RED, RED))
    second = encode_clip((GREEN,) * 6)  # held open: >= 3 decode, all green
    server = FakeStreamServer([first, second], close_after=frozenset({0}))
    runner = FakeRunner()
    with serving(server), ScrcpySource(_config(server.port), runner=runner) as source:
        wait_until(lambda: not source.pipeline_alive)  # conn 0 EOF -> dead
        frame = source.capture()  # must relaunch server + reconnect
        assert dominant_channel(frame) == 1  # green from connection 1
        assert len(runner.commands) == 2  # server launched twice
        assert runner.stopped >= 1  # killed before relaunch
        assert runner.forwards == [(server.port, "scrcpy_01234567")]  # forward kept
        assert source.pipeline_alive


@requires_ffmpeg
def test_capture_fails_cleanly_when_stream_never_comes_back() -> None:
    payload = encode_clip((RED,))
    server = FakeStreamServer([payload], close_after=frozenset({0}))
    runner = FakeRunner()
    with serving(server):
        source = ScrcpySource(
            _config(
                server.port,
                connect_attempts=2,
                connect_timeout_s=0.3,
                first_frame_timeout_s=2.0,
                reconnect_attempts=1,
            ),
            runner=runner,
        )
        with source:
            wait_until(lambda: not source.pipeline_alive)
            server.stop()  # no second connection will ever be accepted
            with pytest.raises(ScrcpyError, match="capture failed after 2 attempts"):
                source.capture()


def test_fake_server_drains_connections_past_its_script() -> None:
    """A reconnect past the scripted payloads must fail FAST with a clean EOF.

    Regression guard for the Windows backlog cascade (module doc): with no
    payloads the fake accepts and closes every connection at once, so each
    attempt sees "stream closed immediately" -- never a ``connect_timeout_s``
    stall in the listen backlog, never ``WinError 10061`` once it is full.
    """
    runner = FakeRunner()
    with serving(FakeStreamServer([])) as server:
        source = ScrcpySource(_config(server.port, connect_attempts=4), runner=runner)
        started = time.monotonic()
        with pytest.raises(ScrcpyError, match="stream closed immediately"):
            source.start()
        elapsed = time.monotonic() - started
        assert server.surplus_connections == 4  # every attempt was accepted + closed
    assert elapsed < 5.0  # far below 4 x connect_timeout_s (40 s) a backlog stall costs
    assert runner.stopped == 1
    assert runner.removed == [server.port]


@requires_ffmpeg
def test_last_access_unit_is_a_decodable_repeat_of_the_final_frame() -> None:
    """The keepalive frame is the clip's own last IDR and ffmpeg keeps decoding it.

    Six frames go in; with the held-open connection repeating the last
    (green) frame every 100 ms the decoder emits MORE than six frames and the
    latest stays green -- i.e. the repeat is a valid continuation of the
    stream, the same way the device encoder's repeated frame is.
    """
    payload = encode_clip((RED,) + (GREEN,) * 5)
    tail = last_access_unit(payload)
    assert tail.startswith(_SPS_START)
    assert 0 < len(tail) < len(payload)
    assert payload.endswith(tail)
    assert last_access_unit(b"\x00" * 64) == b""  # opaque garbage: no keepalive
    server = FakeStreamServer([payload])
    with serving(server), ScrcpySource(_config(server.port), runner=FakeRunner()) as source:
        wait_until(lambda: source.frames_decoded > 6)
        assert dominant_channel(source.capture()) == 1  # still green
        assert source.pipeline_alive


def test_missing_ffmpeg_binary_raises_scrcpy_error() -> None:
    payload = b"\x00" * 64  # never reaches a decoder
    server = FakeStreamServer([payload])
    runner = FakeRunner()
    with serving(server):
        source = ScrcpySource(
            _config(server.port, ffmpeg_path="definitely-not-a-real-ffmpeg-binary"),
            runner=runner,
        )
        with pytest.raises(ScrcpyError, match="cannot spawn ffmpeg"):
            source.start()
    assert runner.stopped == 1
    assert runner.removed == [server.port]


# -- factory -----------------------------------------------------------------------


def test_make_frame_source_kinds() -> None:
    scrcpy = make_frame_source("scrcpy", device_serial="192.168.1.120:5555")
    assert isinstance(scrcpy, ScrcpySource)
    assert scrcpy.config.device_serial == "192.168.1.120:5555"
    minicap = make_frame_source("minicap")
    assert isinstance(minicap, MinicapSource)
    with pytest.raises(ValueError, match="unknown frame source kind"):
        make_frame_source("screencap")


# -- black-frame / FLAG_SECURE detection ------------------------------------------


def test_is_black_frame_predicate() -> None:
    """Pure, synthetic (no ffmpeg): black iff every channel <= max_value."""
    black = np.zeros((4, 4, 3), dtype=np.uint8)
    near_black = np.full((4, 4, 3), 6, dtype=np.uint8)
    assert _is_black_frame(black, 8)
    assert _is_black_frame(near_black, 8)  # codec rounding tolerance
    assert not _is_black_frame(near_black, 5)  # one channel above threshold
    lit = black.copy()
    lit[0, 0, 1] = 200  # a single bright pixel -> not black
    assert not _is_black_frame(lit, 8)
    assert not _is_black_frame(np.zeros((0, 0, 3), dtype=np.uint8), 8)  # empty guard


@pytest.mark.parametrize("overrides", [{"black_frame_limit": -1}, {"black_pixel_max": 256}])
def test_config_rejects_bad_black_values(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ScrcpyConfig(**overrides)  # type: ignore[arg-type]


@requires_ffmpeg
def test_sustained_black_run_trips_secure_error() -> None:
    # 6 black frames, EOF-flushed; limit 3 -> secure trips before all are read.
    payload = encode_clip((BLACK,) * 6)
    server = FakeStreamServer([payload], close_after=frozenset({0}))
    runner = FakeRunner()
    cfg = _config(server.port, black_frame_limit=3, black_pixel_max=24)
    with serving(server), ScrcpySource(cfg, runner=runner) as source:
        wait_until(lambda: source.secure_detected)
        assert not source.pipeline_alive
        with pytest.raises(ScrcpySecureError, match="FLAG_SECURE"):
            source.capture()
        # sticky + NOT retried: no server relaunch, keeps raising
        with pytest.raises(ScrcpySecureError):
            source.capture()
        assert len(runner.commands) == 1  # server launched once, never relaunched


@requires_ffmpeg
def test_non_black_frame_resets_black_counter() -> None:
    # A non-black frame between black runs keeps the max consecutive run at 2
    # (< limit 3), so secure never trips. EOF flushes all 5 frames in order.
    payload = encode_clip((BLACK, BLACK, GREEN, BLACK, BLACK))
    server = FakeStreamServer([payload], close_after=frozenset({0}))
    cfg = _config(server.port, black_frame_limit=3, black_pixel_max=24)
    with serving(server), ScrcpySource(cfg, runner=FakeRunner()) as source:
        wait_until(lambda: source.frames_decoded >= 5)
        assert not source.secure_detected  # the green frame reset the counter


@requires_ffmpeg
def test_black_detection_disabled_when_limit_zero() -> None:
    # limit 0 -> a long all-black run never trips secure. EOF-flushed so all
    # frames decode deterministically regardless of the ffmpeg hold size.
    payload = encode_clip((BLACK,) * 8)
    server = FakeStreamServer([payload], close_after=frozenset({0}))
    cfg = _config(server.port, black_frame_limit=0, black_pixel_max=24)
    with serving(server), ScrcpySource(cfg, runner=FakeRunner()) as source:
        wait_until(lambda: source.frames_decoded >= 8)
        assert not source.secure_detected  # detection off despite 8 black frames


@requires_ffmpeg
def test_fresh_start_clears_secure_state() -> None:
    """Secure is sticky (survives close) but a fresh start() re-probes clean."""
    black = encode_clip((BLACK,) * 6)
    green = encode_clip((GREEN,) * 6)
    server = FakeStreamServer([black, green], close_after=frozenset({0}))
    cfg = _config(server.port, black_frame_limit=3, black_pixel_max=24)
    source = ScrcpySource(cfg, runner=FakeRunner())
    with serving(server):
        source.start()
        wait_until(lambda: source.secure_detected)
        source.close()
        assert source.secure_detected  # sticky across close, until a fresh start()
        source.start()  # second connection serves green -> re-probes, healthy again
        try:
            assert dominant_channel(source.capture()) == 1
            assert not source.secure_detected  # cleared by the fresh pipeline
        finally:
            source.close()


# -- server-side crop ---------------------------------------------------------------

#: (x, y, width, height) inside the 64x64 fake screen; even dims (H.264 4:2:0).
CROP = (16, 8, 32, 48)


def encode_clip_at(size: tuple[int, int], colors: tuple[tuple[int, int, int], ...]) -> bytes:
    """Like :func:`encode_clip` but at an arbitrary frame size (crop streams)."""
    assert FFMPEG is not None
    width, height = size
    raw = b"".join(bytes(color) * (width * height) for color in colors)
    command = [
        FFMPEG,
        "-hide_banner",
        "-loglevel", "error",
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "-s", f"{width}x{height}",
        "-r", "30",
        "-i", "pipe:0",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-g", "1",
        "-pix_fmt", "yuv420p",
        "-f", "h264",
        "pipe:1",
    ]
    result = subprocess.run(command, input=raw, capture_output=True, check=True)
    assert result.stdout, f"ffmpeg produced no bitstream: {result.stderr.decode()}"
    return result.stdout


@pytest.mark.parametrize(
    "crop",
    [
        (-2, 0, 32, 48),  # negative offset
        (0, 0, 0, 48),  # zero width
        (0, 0, 32, -2),  # negative height
        (0, 0, 31, 48),  # odd width (H.264 4:2:0)
        (0, 0, 32, 47),  # odd height
        (40, 0, 32, 48),  # x + width > screen width
        (0, 20, 32, 48),  # y + height > screen height
    ],
)
def test_config_rejects_bad_crop(crop: tuple[int, int, int, int]) -> None:
    with pytest.raises(ValueError):
        ScrcpyConfig(size=SIZE, crop=crop)


def test_config_crop_shrinks_frame_size_and_bytes() -> None:
    cfg = ScrcpyConfig(size=(1080, 2400), crop=(88, 740, 904, 904))
    assert cfg.frame_size == (904, 904)
    assert cfg.frame_bytes == 904 * 904 * 3
    full = ScrcpyConfig(size=(1080, 2400))
    assert full.frame_size == (1080, 2400)
    assert full.frame_bytes == 1080 * 2400 * 3


def test_server_command_carries_crop_only_when_configured() -> None:
    default = ScrcpySource(ScrcpyConfig(), runner=FakeRunner())
    assert "crop=" not in default.server_command  # default path byte-identical
    cropped = ScrcpySource(
        ScrcpyConfig(size=(1080, 2400), crop=(88, 740, 904, 904)), runner=FakeRunner()
    )
    # scrcpy-server v4.0 parseCrop order: WIDTH:HEIGHT:X:Y.
    assert "crop=904:904:88:740" in cropped.server_command


@requires_ffmpeg
def test_cropped_pipeline_decodes_crop_sized_frames() -> None:
    """With ``crop`` set the reader/geometry check expect crop-sized frames."""
    width, height = CROP[2], CROP[3]
    payload = encode_clip_at((width, height), (GREEN,) * 6)
    runner = FakeRunner()
    with serving(FakeStreamServer([payload])) as server:
        cfg = _config(server.port, crop=CROP)
        with ScrcpySource(cfg, runner=runner) as source:
            frame = source.capture()
            assert frame.shape == (height, width, 3)
            assert dominant_channel(frame) == 1  # green
            assert source.pipeline_alive
    assert any("crop=32:48:16:8" in command for command in runner.commands)


@requires_ffmpeg
def test_cropped_config_rejects_full_size_stream() -> None:
    """A full-size stream against a crop config is misframed -- pipeline dies."""
    payload = encode_clip((GREEN,) * 6)  # full 64x64 stream
    with serving(FakeStreamServer([payload])) as server:
        cfg = _config(server.port, crop=CROP, reconnect_attempts=0, first_frame_timeout_s=2.0)
        source = ScrcpySource(cfg, runner=FakeRunner())
        source.start()
        try:
            wait_until(lambda: not source.pipeline_alive)
            assert source._pipeline_error is not None
            assert "geometry" in source._pipeline_error
        finally:
            source.close()
