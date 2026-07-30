"""Strategy backed by the Go expectimax engine in ``tools/engine2048``.

The engine (a port of nneonneo/2048-ai, see ``tools/engine2048``) runs as a
persistent subprocess speaking a line-based stdio protocol: one request
line -> one reply line.

Protocol (ASCII, space-separated):

* ``search <16hex> <depth> <p4> <adaptive 0|1> [mode 0|1] [budget_ms]``
  -> ``<dir> <value> <ms> <depth_limit>`` (``dir`` = -1 when no legal move).
  ``mode`` 1 selects the farm objective; a positive ``budget_ms`` switches
  the engine to time-budgeted iterative deepening (``depth``/``adaptive``
  are then ignored and ``<depth_limit>`` reports the deepest completed
  level).
* ``move <16hex> <dir>`` -> ``<16hex-newboard> <score> <moved 0|1>``
* ``prefetch <16hex> <depth> <p4> <adaptive 0|1> <mode 0|1> <budget_ms>``
  -> ``ok <n_states>``. Speculative: the engine enumerates every spawn state
  of the given post-swipe board (each empty cell x {2, 4}, 2-spawns first)
  and computes, in one background goroutine, the decision an equivalent
  ``search`` would return (``budget_ms`` 0 = fixed depth). Replies
  immediately; any later non-``lookup`` command aborts the speculation
  within a sub-millisecond poll window, so it never delays a real search.
* ``lookup <16hex>`` -> ``hit <dir> <value> <depth>`` | ``miss`` (read-only
  probe of the prefetch cache; never searches, never interrupts a prefetch).
* ``version`` -> ``version protocol=<n> go=<ver> <FARM_KEY>=<value> ...``
  (the farm weights the engine actually applied at startup — see
  :func:`engine_version`).
* ``ping`` -> ``pong``; any error -> ``err <message>``

``<16hex>`` is 16 hex digits where digit ``j`` (leftmost = ``j = 0``) is the
exponent of row-major cell ``j``. Direction ints match
:class:`~bot2048.core.types.Direction`.

``p4`` is the probability that a spawned tile is a 4 (exponent 2) rather than
a 2 (exponent 1); the engine spreads spawns uniformly over empty cells.
"""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import os
import queue
import subprocess
import threading
import time
import weakref
from pathlib import Path
from typing import IO

from bot2048.ai.strategies import Decision, NoLegalMoveError
from bot2048.core import engine
from bot2048.core.types import Board, Direction, MoveResult

__all__ = [
    "GoEngineError",
    "GoExpectimaxStrategy",
    "board_to_hex",
    "engine_build_sha256",
    "engine_source_sha256",
    "engine_version",
    "find_engine_binary",
    "hex_to_board",
    "resolve_engine_binary",
]

ENGINE_ENV_VAR = "BOT2048_ENGINE2048_BIN"
_HEX_DIGITS = "0123456789abcdef"

#: Floor for the engine reply watchdog (seconds). Engine DEATH already raises
#: (empty readline -> GoEngineError, recoverable), but a STALL -- a hung
#: iterative-deepening loop or a wedged pipe -- would block ``choose()`` FOREVER
#: with no exception for the never-crash loop to catch. The watchdog converts
#: that hang into the same recoverable ``GoEngineError``. It is a HANG guard,
#: not a search deadline: it must sit far above the worst LEGITIMATE reply so a
#: slow-but-honest search is never false-aborted. 15 s comfortably covers a
#: fixed-depth deep search AND the live endgame budget (up to ~9 s, escalatable
#: toward ~15-20 s); budget mode scales to ``_REPLY_TIMEOUT_BUDGET_FACTOR`` x the
#: budget, which the engine's own hard abort keeps well under.
_REPLY_TIMEOUT_FLOOR_S: float = 15.0

#: Budget-mode multiplier (see :meth:`GoExpectimaxStrategy._reply_timeout_s`).
_REPLY_TIMEOUT_BUDGET_FACTOR: float = 8.0


class GoEngineError(RuntimeError):
    """The Go engine process died or replied with a protocol error."""


def _tools_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "tools" / "engine2048"


def _default_binary_path() -> Path:
    name = "engine2048.exe" if os.name == "nt" else "engine2048"
    return _tools_dir() / name


def find_engine_binary() -> Path | None:
    """Locate an already-built engine binary, or ``None`` if not present.

    Checked in order: the ``BOT2048_ENGINE2048_BIN`` environment variable,
    then ``tools/engine2048/engine2048[.exe]`` relative to the repository
    root (which only exists when ``bot2048`` is used from a source checkout).
    """
    env = os.environ.get(ENGINE_ENV_VAR)
    if env:
        path = Path(env)
        return path if path.is_file() else None
    candidate = _default_binary_path()
    return candidate if candidate.is_file() else None


def _build_engine(binary: Path) -> Path:
    """Build the Go engine to ``binary``; raise ``RuntimeError`` on failure."""
    proc = subprocess.run(
        ["go", "build", "-o", str(binary), "."],
        cwd=_tools_dir(),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not binary.is_file():
        raise RuntimeError(
            "failed to build the Go engine "
            f"(go build exited {proc.returncode}): {proc.stderr.strip()}"
        )
    return binary


def _is_stale(binary: Path) -> bool:
    """True when a ``.go`` source is newer than ``binary`` (mtime).

    A binary built before the last source edit silently keeps running the OLD
    heuristic — every simulation after a ``git pull`` that touched ``farm.go``
    then measures code nobody is reading. Only binaries that live in the tools
    dir are checked: an explicitly supplied binary from elsewhere is not
    something we built and not something we may overwrite.
    """
    tools = _tools_dir()
    if binary.parent.resolve() != tools.resolve():
        return False
    built = binary.stat().st_mtime
    return any(src.stat().st_mtime > built for src in tools.glob("*.go"))


def resolve_engine_binary(binary: Path | None = None) -> Path:
    """Locate the engine binary, (re)building it when missing or stale.

    Resolution order: explicit ``binary`` -> ``BOT2048_ENGINE2048_BIN`` ->
    ``tools/engine2048/engine2048[.exe]``.
    """
    if binary is not None:
        target = Path(binary)
    else:
        env = os.environ.get(ENGINE_ENV_VAR)
        target = Path(env) if env else _default_binary_path()
    if not target.is_file() or _is_stale(target):
        target = _build_engine(target)
    return target


def engine_build_sha256(binary: Path) -> str:
    """SHA-256 of the engine binary — this exact artefact's fingerprint.

    Identifies the file that ran, and nothing more. It is NOT a portable
    identity for the engine's *code*: a Go binary embeds its own output path,
    so two builds of identical sources to different paths already differ — let
    alone builds for arm64 and amd64, which the cross-machine merge flow pools
    by design. Use :func:`engine_source_sha256` to compare engine code.
    """
    digest = hashlib.sha256()
    with binary.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def engine_source_sha256() -> str:
    """SHA-256 over the engine's Go sources — a PORTABLE identity for its code.

    Hashes every non-test ``.go`` file plus ``go.mod`` (name + content, in a
    fixed order), so it is identical on every machine at a given commit and
    changes whenever the engine's behaviour can change. This is what makes
    "same heuristic?" answerable across an arm64 Mac and an amd64 Windows box,
    where the binaries can never match byte-for-byte.

    It describes the CHECKOUT. The runner rebuilds any stale binary before a
    run (see :func:`resolve_engine_binary`), which is what keeps the checkout
    and the artefact that ran in agreement.
    """
    digest = hashlib.sha256()
    tools = _tools_dir()
    sources = sorted(
        (p for p in tools.glob("*.go") if not p.name.endswith("_test.go")),
        key=lambda p: p.name,
    )
    for path in [*sources, tools / "go.mod"]:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def engine_version(binary: Path | None = None) -> dict[str, str]:
    """Ask the engine what it is and which farm weights it ACTUALLY applied.

    Returns the parsed ``version`` reply plus ``build_sha256`` and ``binary``.
    The weights come back from the engine *after* it has read the
    ``BOT2048_FARM_*`` overrides, so they include every weight the config left
    implicit — call this in the same environment the engine will run in.
    """
    target = resolve_engine_binary(binary)
    proc = subprocess.run(
        [str(target)],
        input="version\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60.0,
    )
    reply = (proc.stdout or "").strip()
    fields = reply.split()
    if proc.returncode != 0 or not fields or fields[0] != "version":
        raise GoEngineError(f"engine version failed (exit {proc.returncode}): {reply!r}")
    info: dict[str, str] = {
        "binary": str(target),
        "build_sha256": engine_build_sha256(target),
        "source_sha256": engine_source_sha256(),
    }
    for field in fields[1:]:
        key, _, value = field.partition("=")
        info[key] = value
    return info


def board_to_hex(board: Board) -> str:
    """Encode a row-major board as 16 hex digits (leftmost = cell 0)."""
    if len(board) != 16:
        raise ValueError(f"board must have 16 cells, got {len(board)}")
    try:
        return "".join(_HEX_DIGITS[e] for e in board)
    except (IndexError, TypeError) as exc:
        raise ValueError(f"cell exponents must be ints in 0..15: {board!r}") from exc


def hex_to_board(text: str) -> Board:
    """Decode 16 hex digits back into a row-major board."""
    if len(text) != 16:
        raise GoEngineError(f"engine returned malformed board {text!r}")
    return tuple(int(c, 16) for c in text)


class GoExpectimaxStrategy:
    """Expectimax via the Go engine subprocess.

    Satisfies the :class:`~bot2048.ai.strategies.Strategy` protocol. The
    subprocess is started lazily on first use and restarted automatically if
    it dies; call :meth:`close` (or use as a context manager) to shut it
    down. A single process serves one request at a time.
    """

    def __init__(
        self,
        depth: int = 6,
        p4: float = 0.1,
        adaptive: bool = True,
        binary: Path | None = None,
        farm: bool = False,
        budget_ms: float | None = None,
        prefetch: bool = False,
    ) -> None:
        if depth < 1:
            raise ValueError("depth must be >= 1")
        if budget_ms is not None and not budget_ms > 0.0:
            raise ValueError("budget_ms must be > 0")
        self._depth = depth
        self._p4 = float(p4)
        self._adaptive = adaptive
        self._farm = farm
        self._budget_ms = float(budget_ms) if budget_ms is not None else None
        self._prefetch = prefetch
        # Adaptive speculation gate (see _maybe_speculate): EMA of the
        # caller's think-time between consecutive choose() calls, the
        # engine-side cost of the last real search (a proxy for one
        # spawn-state speculation), and whether the previous choose()
        # actually started a speculation (else lookup is a guaranteed miss).
        self._last_return: float | None = None
        self._gap_ema_ms: float | None = None
        self._last_search_ms: float | None = None
        self._spec_started = False
        self._binary = self._resolve_binary(binary)
        self._proc: subprocess.Popen[str] | None = None
        # Reply watchdog plumbing: a per-process daemon thread drains stdout
        # into ``_reply_q`` so ``_request`` can wait for a reply with a TIMEOUT
        # (``queue.get``) instead of a bare, unbounded ``readline()``. Both are
        # (re)created together with the subprocess in ``_ensure_proc``.
        self._reply_q: queue.Queue[str] | None = None
        self._reader: threading.Thread | None = None
        # Close the subprocess at interpreter exit without keeping this
        # object alive (so __del__ still runs on earlier collection).
        _self = weakref.ref(self)

        def _atexit_close() -> None:
            obj = _self()
            if obj is not None:
                obj.close()

        atexit.register(_atexit_close)

    @staticmethod
    def _resolve_binary(binary: Path | None) -> Path:
        # explicit arg -> env var -> default path; auto-build if missing OR
        # stale (a source edit must never be silently ignored).
        return resolve_engine_binary(binary)

    @property
    def name(self) -> str:
        suffix = ",farm" if self._farm else ""
        if self._budget_ms is not None:
            return f"go-expectimax(budget={self._budget_ms:g}ms{suffix})"
        return f"go-expectimax(depth={self._depth}{suffix})"

    @property
    def depth(self) -> int:
        return self._depth

    @property
    def farm(self) -> bool:
        return self._farm

    @property
    def budget_ms(self) -> float | None:
        return self._budget_ms

    def set_budget_ms(self, value: float | None) -> None:
        """Retune the per-move search budget between ``choose()`` calls.

        Used by the live runner's mid-game budget escalation (the
        median/late-game trigger, docs/live-record-attempt-brief.md §5.2):
        once a game survives deep, the loop raises the budget on the SAME
        warm subprocess instead of rebuilding the strategy. ``None`` returns
        to fixed-depth mode; the constructor value stays the default.
        """
        if value is not None and not value > 0.0:
            raise ValueError("budget_ms must be > 0")
        self._budget_ms = float(value) if value is not None else None

    # --- strategy API --------------------------------------------------

    def choose(self, board: Board) -> Decision:
        start = time.perf_counter()
        if self._last_return is not None:
            gap_ms = (start - self._last_return) * 1000.0
            self._gap_ema_ms = (
                gap_ms
                if self._gap_ema_ms is None
                else 0.5 * (gap_ms + self._gap_ema_ms)
            )
        hex_board = board_to_hex(board)
        if self._prefetch and self._spec_started:
            hit = self._try_lookup(hex_board)
            if hit is not None:
                direction, value, achieved_depth = hit
                if not engine.move(board, direction).moved:
                    raise GoEngineError(
                        f"engine returned illegal move {direction.name} for board {board}"
                    )
                self._maybe_speculate(board, direction)
                decision = Decision(
                    direction=direction,
                    eval_ms=(time.perf_counter() - start) * 1000.0,
                    search_depth=achieved_depth,
                    details={
                        "engine": "go",
                        "eval": value,
                        "engine_ms": 0.0,
                        "p4": self._p4,
                        "farm": self._farm,
                        "prefetch_hit": True,
                    },
                )
                self._last_return = time.perf_counter()
                return decision
        # Optional trailing fields are appended only when needed, so the
        # protocol stays backward compatible for any caller that never
        # asked for farm/budget: 6th = mode (1 = farm), 7th = budget_ms
        # (positional, so a budget always sends an explicit mode too).
        suffix = ""
        if self._farm or self._budget_ms is not None:
            suffix = " 1" if self._farm else " 0"
        if self._budget_ms is not None:
            suffix += f" {self._budget_ms!r}"
        reply = self._request(
            f"search {hex_board} {self._depth} {self._p4!r} {1 if self._adaptive else 0}{suffix}"
        )
        fields = reply.split()
        if len(fields) != 4:
            raise GoEngineError(f"malformed search reply {reply!r}")
        direction_int = int(fields[0])
        if direction_int == -1:
            raise NoLegalMoveError("no legal moves available")
        direction = Direction(direction_int)
        if not engine.move(board, direction).moved:
            raise GoEngineError(f"engine returned illegal move {direction.name} for board {board}")
        self._last_search_ms = float(fields[2])
        if self._prefetch:
            self._maybe_speculate(board, direction)
        decision = Decision(
            direction=direction,
            eval_ms=(time.perf_counter() - start) * 1000.0,
            search_depth=int(fields[3]),
            details={
                "engine": "go",
                "eval": float(fields[1]),
                "engine_ms": self._last_search_ms,
                "p4": self._p4,
                "farm": self._farm,
            },
        )
        self._last_return = time.perf_counter()
        return decision

    # --- speculative prefetch -------------------------------------------

    def _try_lookup(self, hex_board: str) -> tuple[Direction, float, int] | None:
        """Probe the engine prefetch cache; ``None`` on miss."""
        reply = self._request(f"lookup {hex_board}")
        fields = reply.split()
        if fields[0] == "miss":
            return None
        if len(fields) != 4 or fields[0] != "hit":
            raise GoEngineError(f"malformed lookup reply {reply!r}")
        if fields[1] == "-1":
            # A cached decision for a dead spawn-state (search returned
            # bestDir == -1). Mirror the direct search path (see ``choose``) so
            # callers see ``NoLegalMoveError`` rather than a raw ``ValueError``
            # out of ``Direction(-1)``.
            raise NoLegalMoveError("no legal moves available")
        return Direction(int(fields[1])), float(fields[2]), int(fields[3])

    def _maybe_speculate(self, board: Board, direction: Direction) -> None:
        """Speculate only when the caller's pace leaves room for it to help.

        A prefetch hit requires the background worker to finish at least one
        spawn-state search — each costs about one real search (the last
        observed ``engine_ms``) — before the next real command aborts it.
        Measured on Windows: in back-to-back loops (sim benches) the gap
        between choose() calls is far below that, so the worker never
        completes a state (~0% hits) while its search goroutines saturate
        the CPU; the lookup and prefetch round-trips then cost ~7-8 ms each
        instead of ~15 us, inflating IPC by ~16 ms/move. Live drivers pause
        ~200 ms per move to animate/perceive, so speculation stays enabled
        there. Gate: skip when the EMA of inter-call gaps is below the last
        real search's engine-side cost.
        """
        gap = self._gap_ema_ms
        cost = self._last_search_ms
        if gap is not None and cost is not None and gap < cost:
            self._spec_started = False
            return
        self._speculate(board, direction)
        self._spec_started = True

    def _speculate(self, board: Board, direction: Direction) -> None:
        """Kick off background prefetch for every spawn of the next board.

        Fire-and-forget: the engine replies ``ok <n>`` immediately and
        searches concurrently; the next real command aborts it, so this
        never blocks or delays the caller beyond one stdio round-trip.
        """
        post_swipe = engine.move(board, direction).board
        # Unlike ``search``, prefetch takes all six fields positionally
        # (budget_ms 0 = fixed-depth speculation).
        mode = 1 if self._farm else 0
        budget = self._budget_ms if self._budget_ms is not None else 0.0
        reply = self._request(
            f"prefetch {board_to_hex(post_swipe)} {self._depth} {self._p4!r} "
            f"{1 if self._adaptive else 0} {mode} {budget!r}"
        )
        if not reply.startswith("ok"):
            raise GoEngineError(f"malformed prefetch reply {reply!r}")

    def move_raw(self, board: Board, direction: Direction) -> MoveResult:
        """Apply one move inside the engine (no spawn). For parity testing."""
        reply = self._request(f"move {board_to_hex(board)} {int(direction)}")
        fields = reply.split()
        if len(fields) != 3:
            raise GoEngineError(f"malformed move reply {reply!r}")
        moved = fields[2] == "1"
        return MoveResult(
            board=hex_to_board(fields[0]),
            score_gained=int(fields[1]),
            moved=moved,
        )

    # --- process management ---------------------------------------------

    def close(self) -> None:
        """Terminate the engine subprocess (a later call restarts it)."""
        # A restarted process starts with an empty prefetch cache, so the
        # next choose() must not probe it.
        self._spec_started = False
        proc = self._proc
        self._proc = None
        # Drop the reader thread's queue reference; closing stdin below EOFs the
        # daemon reader, which then exits on its own (it is not join()ed so a
        # wedged pipe can never make close() itself hang).
        self._reply_q = None
        self._reader = None
        if proc is None or proc.poll() is not None:
            return
        if proc.stdin is not None:
            proc.stdin.close()  # EOF -> clean exit 0
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def __enter__(self) -> GoExpectimaxStrategy:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self.close()  # best-effort cleanup in finalizer

    def _reply_timeout_s(self) -> float:
        """Watchdog bound for one engine reply -- a HANG guard, not a deadline.

        Sized to sit far above the worst LEGITIMATE reply so a slow-but-honest
        search is never false-aborted (which would spuriously kill+respawn the
        engine and lose the deep game just as surely as the hang would):

        * fixed-depth mode has no server-side clock -> the
          :data:`_REPLY_TIMEOUT_FLOOR_S` (15 s) floor, comfortably above a
          deep-6 search and the live endgame budget;
        * budget mode -> ``_REPLY_TIMEOUT_BUDGET_FACTOR`` x the budget (never
          below the floor); the engine's own hard time abort keeps a real reply
          far under this, so only a genuine stall ever waits it out.
        """
        if self._budget_ms is not None:
            return max(
                _REPLY_TIMEOUT_FLOOR_S,
                _REPLY_TIMEOUT_BUDGET_FACTOR * self._budget_ms / 1000.0,
            )
        return _REPLY_TIMEOUT_FLOOR_S

    @staticmethod
    def _reader_loop(stdout: IO[str], q: queue.Queue[str]) -> None:
        """Drain the engine's stdout line-by-line into ``q`` (daemon thread).

        One reply line per request (the shipped engine is single-writer,
        one-flush-per-reply -- see the engine-desync note), so the queue stays
        in request order. An empty line is EOF (engine died); it is enqueued too
        so a waiter sees the death immediately instead of blocking.
        """
        try:
            while True:
                line = stdout.readline()
                q.put(line)
                if line == "":  # EOF -> process gone; stop the thread
                    return
        except Exception:  # pragma: no cover - pipe torn down under us
            with contextlib.suppress(Exception):
                q.put("")

    def _ensure_proc(self) -> subprocess.Popen[str]:
        if self._proc is None or self._proc.poll() is not None:
            proc = subprocess.Popen(
                [str(self._binary)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
            self._proc = proc
            # Fresh queue + daemon reader bound to THIS process. A respawn makes
            # a new pair; any old reader drains a now-closed pipe and exits, so
            # a stale reply can never leak into the new process's queue.
            assert proc.stdout is not None
            q: queue.Queue[str] = queue.Queue()
            self._reply_q = q
            self._reader = threading.Thread(
                target=self._reader_loop, args=(proc.stdout, q), daemon=True
            )
            self._reader.start()
        return self._proc

    def _kill_stalled_proc(self) -> None:
        """Kill a hung engine so the NEXT ``choose()`` respawns a clean one."""
        proc = self._proc
        self._proc = None
        self._reply_q = None
        self._reader = None
        if proc is not None and proc.poll() is None:
            with contextlib.suppress(Exception):
                proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=2.0)

    def _request(self, line: str) -> str:
        proc = self._ensure_proc()
        assert proc.stdin is not None
        reply_q = self._reply_q
        assert reply_q is not None
        try:
            proc.stdin.write(line + "\n")
            proc.stdin.flush()
        except OSError as exc:
            self._proc = None
            raise GoEngineError(f"engine process I/O failed: {exc}") from exc
        try:
            reply: str = reply_q.get(timeout=self._reply_timeout_s())
        except queue.Empty:
            # STALL: the request was accepted but no reply arrived within a
            # bound far above the worst legit search. This is the silent-hang
            # path the never-crash loop cannot otherwise see. Kill + respawn and
            # raise the SAME GoEngineError the engine-death path already raises,
            # which the live loop recovers from (session_recovery.py).
            timeout_s = self._reply_timeout_s()
            self._kill_stalled_proc()
            raise GoEngineError(
                f"engine stalled: no reply within {timeout_s:g}s for "
                f"{line.split()[0]!r} -- killed the process, will respawn"
            ) from None
        if not reply:
            self._proc = None
            raise GoEngineError(f"engine process exited unexpectedly (code {proc.poll()})")
        reply = reply.strip()
        if reply.startswith("err"):
            raise GoEngineError(f"engine error: {reply[3:].strip()}")
        return reply
