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
* ``version`` -> ``version protocol=<n> go=<ver> engine=<kind> <KEY>=<value> ...``
  (the farm weights the engine actually applied at startup — see
  :func:`engine_version`; ``engine`` is ``engine2048`` or ``ntuple``).
* ``ping`` -> ``pong``; any error -> ``err <message>``

Protocol 2 (additive; detected per spawn from the ``version`` handshake):

* ``search``/``prefetch`` take all six positional fields plus a ``k=v`` tail
  ``maxdepth=<D|0> nodes=<N|0> [cprob=<C>]`` — two INDEPENDENT optional
  limits (:class:`SearchLimits`): the engine stops at whichever of the depth
  cap and the wall-clock budget hits first, ``nodes`` is the hardware-
  independent guard, ``0`` means "none". ``maxdepth=D`` with ``budget_ms=0`` is
  the fixed-depth path; with a budget it is *direct-first*: the fixed-depth-D
  answer when it fits (``stop=depth``, bit-identical to fixed depth on any
  machine), else the ladder capped at D. The reply (and a ``lookup`` hit for a
  tailed prefetch) appends ``stop=<depth|budget|nodes|ladder_end|record>
  nodes=<N> cprob=<C>``. A protocol-1 binary never sees the tail: the wrapper
  sends the old wire and infers ``stop`` from the mode.

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
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from bot2048.ai.strategies import Decision, NoLegalMoveError
from bot2048.core import engine
from bot2048.core.types import Board, Direction, MoveResult

__all__ = [
    "ENGINE_KINDS",
    "GoEngineError",
    "GoExpectimaxStrategy",
    "SearchLimits",
    "board_to_hex",
    "engine_build_sha256",
    "engine_kind",
    "engine_source_sha256",
    "engine_version",
    "find_engine_binary",
    "hex_to_board",
    "resolve_engine_binary",
]

ENGINE_ENV_VAR = "BOT2048_ENGINE2048_BIN"
_HEX_DIGITS = "0123456789abcdef"

#: The Go engines this wrapper can drive, by the ``engine=`` key of their
#: ``version`` reply; each lives in ``tools/<kind>/``.
ENGINE_KINDS: tuple[str, ...] = ("engine2048", "ntuple")
_DEFAULT_ENGINE_KIND = "engine2048"

#: Process-environment keys that configure an engine (read once at its
#: startup). When a caller passes an explicit ``env`` these are STRIPPED from
#: the inherited ``os.environ`` first, so the shell is never a hidden input to
#: a run: the spec's env is the only source of ``BOT2048_*`` / ``GOMAXPROCS``.
_ENGINE_ENV_PREFIX = "BOT2048_"
_ENGINE_ENV_KEYS = frozenset({"GOMAXPROCS"})

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


def _tools_dir(kind: str = _DEFAULT_ENGINE_KIND) -> Path:
    """``tools/<kind>/`` of the source checkout (``engine2048`` by default)."""
    if kind not in ENGINE_KINDS:
        raise ValueError(f"unknown engine kind {kind!r}; expected one of {ENGINE_KINDS}")
    return Path(__file__).resolve().parents[3] / "tools" / kind


def _default_binary_path() -> Path:
    name = "engine2048.exe" if os.name == "nt" else "engine2048"
    return _tools_dir() / name


def _engine_env(env: Mapping[str, str] | None) -> dict[str, str] | None:
    """The process environment for an engine subprocess.

    ``None`` -> ``None`` (inherit ``os.environ`` unchanged, today's behaviour).
    Otherwise: ``os.environ`` minus every ``BOT2048_*`` key and ``GOMAXPROCS``,
    plus ``env`` -- so a stale shell export can never re-tune an engine whose
    configuration was given explicitly, while PATH/TMP/etc. still reach it.
    """
    if env is None:
        return None
    clean = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_ENGINE_ENV_PREFIX) and key not in _ENGINE_ENV_KEYS
    }
    clean.update(env)
    return clean


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


def engine_source_sha256(tools_dir: Path | None = None) -> str:
    """SHA-256 over an engine's Go sources — a PORTABLE identity for its code.

    Hashes every non-test ``.go`` file plus ``go.mod`` (name + content, in a
    fixed order), so it is identical on every machine at a given commit and
    changes whenever the engine's behaviour can change. This is what makes
    "same heuristic?" answerable across an arm64 Mac and an amd64 Windows box,
    where the binaries can never match byte-for-byte.

    ``tools_dir`` is the engine's source directory (``tools/engine2048`` by
    default; ``tools/ntuple`` for the n-tuple engine -- pick it from the
    ``engine=`` key of the ``version`` reply, see :func:`engine_kind`, so an
    ntuple run never records engine2048's hash).

    It describes the CHECKOUT. The runner rebuilds any stale binary before a
    run (see :func:`resolve_engine_binary`), which is what keeps the checkout
    and the artefact that ran in agreement.
    """
    digest = hashlib.sha256()
    tools = _tools_dir() if tools_dir is None else Path(tools_dir)
    sources = sorted(
        (p for p in tools.glob("*.go") if not p.name.endswith("_test.go")),
        key=lambda p: p.name,
    )
    for path in [*sources, tools / "go.mod"]:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _parse_version_reply(reply: str) -> dict[str, str]:
    """Split a ``version k=v k=v ...`` line into a dict (raises on anything else)."""
    fields = reply.split()
    if not fields or fields[0] != "version":
        raise GoEngineError(f"malformed version reply {reply!r}")
    info: dict[str, str] = {}
    for field in fields[1:]:
        key, _, value = field.partition("=")
        info[key] = value
    return info


def engine_kind(info: Mapping[str, str], binary: Path | None = None) -> str:
    """Which engine a ``version`` reply came from: ``engine2048`` or ``ntuple``.

    Taken from the reply's ``engine=`` key (both engines report it). For a
    binary that predates the key, fall back to the name of the binary's parent
    directory (``tools/<kind>/``), then to ``engine2048``.
    """
    kind = info.get("engine")
    if kind in ENGINE_KINDS:
        return kind
    path = binary if binary is not None else Path(info.get("binary", ""))
    parent = path.resolve().parent.name if str(path) else ""
    return parent if parent in ENGINE_KINDS else _DEFAULT_ENGINE_KIND


def engine_version(
    binary: Path | None = None, *, env: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Ask the engine what it is and which farm weights it ACTUALLY applied.

    Returns the parsed ``version`` reply plus ``binary``, ``build_sha256``,
    ``source_sha256`` (of the RIGHT engine's sources, chosen by the reply's
    ``engine=`` key) and ``engine`` (filled in for a binary that predates the
    key). The weights come back from the engine *after* it has read the
    ``BOT2048_*`` overrides, so they include every value the config left
    implicit — pass the same ``env`` the engine will run with (``None`` =
    inherit the current process environment, as a plain :class:`Popen` would;
    see :func:`_engine_env`).
    """
    target = resolve_engine_binary(binary)
    proc = subprocess.run(
        [str(target)],
        input="version\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60.0,
        env=_engine_env(env),
    )
    reply = (proc.stdout or "").strip()
    if proc.returncode != 0 or not reply.startswith("version"):
        raise GoEngineError(f"engine version failed (exit {proc.returncode}): {reply!r}")
    info = _parse_version_reply(reply)
    kind = engine_kind(info, target)
    info["engine"] = kind
    info["binary"] = str(target)
    info["build_sha256"] = engine_build_sha256(target)
    info["source_sha256"] = engine_source_sha256(_tools_dir(kind))
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


@dataclass(frozen=True, slots=True)
class SearchLimits:
    """Two independent, individually optional stop conditions plus a guard.

    ``max_depth`` (hardware-independent) and ``budget_ms`` (wall clock): the
    engine stops at whichever hits first. ``None`` = no limit on that axis;
    both ``None`` is the "unbounded" mode where ``max_nodes`` is the only stop.
    ``max_nodes`` is the per-search node cap -- deterministic, so it is the
    guard that keeps the unbounded mode reproducible; ``None`` leaves today's
    time-only behaviour untouched.

    Local stand-in for the spec-level ``Limits``/``Guard`` (``ai/spec.py``);
    the strategy factory maps the spec onto this.
    """

    max_depth: int | None = None
    budget_ms: float | None = None
    max_nodes: int | None = None

    def __post_init__(self) -> None:
        if self.max_depth is not None and self.max_depth < 1:
            raise ValueError("max_depth must be >= 1 (or None)")
        if self.budget_ms is not None and not self.budget_ms > 0.0:
            raise ValueError("budget_ms must be > 0 (or None)")
        if self.max_nodes is not None and self.max_nodes < 1:
            raise ValueError("max_nodes must be >= 1 (or None)")


def _split_reply_tail(reply: str) -> tuple[list[str], dict[str, str]]:
    """Split a reply into its positional fields and the optional ``k=v`` tail.

    Protocol 2 appends ``stop=... nodes=... cprob=...`` to ``search`` replies
    and ``lookup`` hits; a protocol-1 binary sends the positional fields only.
    """
    fields = reply.split()
    for i, field in enumerate(fields):
        if "=" in field:
            tail = dict(f.partition("=")[::2] for f in fields[i:])
            return fields[:i], tail
    return fields, {}


class GoExpectimaxStrategy:
    """Expectimax via the Go engine subprocess.

    Satisfies the :class:`~bot2048.ai.strategies.Strategy` protocol. The
    subprocess is started lazily on first use and restarted automatically if
    it dies; call :meth:`close` (or use as a context manager) to shut it
    down. A single process serves one request at a time.

    Search limits (:class:`SearchLimits`): the legacy ``depth``/``budget_ms``
    arguments map onto them -- ``budget_ms`` set = time-only ladder (no depth
    cap, today's live behaviour), else fixed ``depth`` -- or pass ``limits``
    explicitly for a capped budget / node guard / unbounded search.

    ``env`` is the engine process environment (``None`` inherits the caller's,
    as before; a mapping replaces every ``BOT2048_*``/``GOMAXPROCS`` the shell
    had, see :func:`_engine_env`). ``label`` overrides :attr:`name`.
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
        *,
        limits: SearchLimits | None = None,
        cprob: float | None = None,
        env: Mapping[str, str] | None = None,
        label: str | None = None,
        hang_timeout_s: float = _REPLY_TIMEOUT_FLOOR_S,
    ) -> None:
        if depth < 1:
            raise ValueError("depth must be >= 1")
        if budget_ms is not None and not budget_ms > 0.0:
            raise ValueError("budget_ms must be > 0")
        if limits is not None and budget_ms is not None:
            raise ValueError("pass either budget_ms or limits, not both")
        if cprob is not None and not 0.0 < cprob <= 1.0:
            raise ValueError("cprob must be in (0, 1]")
        if not hang_timeout_s > 0.0:
            raise ValueError("hang_timeout_s must be > 0")
        self._depth = depth
        self._p4 = float(p4)
        self._adaptive = adaptive
        self._farm = farm
        if limits is None:
            # Legacy mapping: a budget is the uncapped ladder (time-only), no
            # budget is the fixed depth -- exactly today's two modes.
            limits = SearchLimits(
                max_depth=None if budget_ms is not None else depth,
                budget_ms=float(budget_ms) if budget_ms is not None else None,
            )
        self._limits = limits
        self._cprob = float(cprob) if cprob is not None else None
        self._env = _engine_env(env)
        self._label = label
        self._hang_timeout_s = float(hang_timeout_s)
        self._prefetch = prefetch
        # Filled by the per-spawn `version` handshake (see _ensure_proc): the
        # engine's protocol version decides the wire shape, and the parsed reply
        # is the engine's own account of its configuration (provenance).
        self._protocol = 1
        self._engine_info: dict[str, str] = {}
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
        if self._label is not None:
            return self._label
        suffix = ",farm" if self._farm else ""
        budget = self._limits.budget_ms
        if budget is not None:
            return f"go-expectimax(budget={budget:g}ms{suffix})"
        return f"go-expectimax(depth={self._depth}{suffix})"

    @property
    def depth(self) -> int:
        """The legacy fixed depth (also the fallback cap when a budget is lifted)."""
        return self._depth

    @property
    def farm(self) -> bool:
        return self._farm

    @property
    def budget_ms(self) -> float | None:
        return self._limits.budget_ms

    @property
    def limits(self) -> SearchLimits:
        """The limits the NEXT ``choose()`` will search under."""
        return self._limits

    @property
    def engine_info(self) -> Mapping[str, str]:
        """The parsed ``version`` reply of the running engine (empty before the
        first spawn): ``protocol``, ``engine``, ``BOT2048_*`` effective values."""
        return self._engine_info

    @property
    def protocol(self) -> int:
        """Wire protocol of the running engine (1 before the first spawn)."""
        return self._protocol

    def set_limits(self, limits: SearchLimits) -> None:
        """Retune the search limits between ``choose()`` calls (ladder tiers).

        Used by the live runner's mid-game escalation: once a game survives
        deep, the loop raises the limits on the SAME warm subprocess instead of
        rebuilding the strategy. Resets the prefetch gate: a speculation that
        is still running was computed under the PREVIOUS limits, and a cache
        hit would silently serve a decision made with the old budget/cap.
        """
        if not isinstance(limits, SearchLimits):
            raise TypeError(f"limits must be a SearchLimits, got {type(limits).__name__}")
        self._limits = limits
        self._spec_started = False

    def set_budget_ms(self, value: float | None) -> None:
        """Retune the per-move budget -- thin alias over :meth:`set_limits`.

        Legacy semantics kept for the budget ladder: in budget mode the depth
        cap (if any) is inherited by the new budget; stepping from fixed depth
        into a budget lifts the cap (time-only, as today); ``None`` returns to
        fixed-depth mode at the current cap or, failing that, the constructor
        depth -- never to the unbounded mode by accident.
        """
        if value is not None and not value > 0.0:
            raise ValueError("budget_ms must be > 0")
        lim = self._limits
        max_depth: int | None
        if value is None:
            max_depth = lim.max_depth if lim.max_depth is not None else self._depth
            new = SearchLimits(max_depth=max_depth, budget_ms=None, max_nodes=lim.max_nodes)
        else:
            max_depth = lim.max_depth if lim.budget_ms is not None else None
            new = SearchLimits(max_depth=max_depth, budget_ms=float(value), max_nodes=lim.max_nodes)
        self.set_limits(new)

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
        # Spawn (and handshake) first: the wire shape below depends on the
        # engine's protocol version, which only the running process knows.
        self._ensure_proc()
        if self._prefetch and self._spec_started:
            hit = self._try_lookup(hex_board)
            if hit is not None:
                direction, value, achieved_depth, tail = hit
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
                        "search": self._search_details(tail),
                    },
                )
                self._last_return = time.perf_counter()
                return decision
        reply = self._request(self._search_line(hex_board))
        fields, tail = _split_reply_tail(reply)
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
                "search": self._search_details(tail),
            },
        )
        self._last_return = time.perf_counter()
        return decision

    # --- wire shape -------------------------------------------------------

    def _wire_depth(self) -> int:
        """The positional ``<depth>`` field: the cap, else the legacy depth."""
        return self._limits.max_depth if self._limits.max_depth is not None else self._depth

    def _limits_tail(self) -> str:
        """The protocol-2 ``k=v`` request tail for the current limits."""
        lim = self._limits
        tail = f" maxdepth={lim.max_depth or 0} nodes={lim.max_nodes or 0}"
        if self._cprob is not None:
            tail += f" cprob={self._cprob!r}"
        return tail

    def _require_protocol1_expressible(self) -> None:
        """A protocol-1 engine knows two modes only: fixed depth or time-only.

        Anything else (depth cap + budget, node guard, pinned cprob, unbounded)
        would be silently degraded on the wire -- and recorded as if it ran.
        Refuse instead; the fix is to rebuild the engine.
        """
        lim = self._limits
        unexpressible = (
            lim.max_nodes is not None
            or self._cprob is not None
            or (lim.max_depth is not None and lim.budget_ms is not None)
            or (lim.max_depth is None and lim.budget_ms is None)
        )
        if unexpressible:
            raise GoEngineError(
                f"engine speaks protocol 1 and cannot honour {lim} (cprob={self._cprob}); "
                "rebuild the engine (protocol 2) or use a plain depth / budget"
            )

    def _search_line(self, hex_board: str) -> str:
        """Build the ``search`` request for the running engine's protocol."""
        head = f"search {hex_board} {self._wire_depth()} {self._p4!r} {1 if self._adaptive else 0}"
        mode = 1 if self._farm else 0
        budget = self._limits.budget_ms
        if self._protocol >= 2:
            return f"{head} {mode} {budget or 0.0!r}{self._limits_tail()}"
        self._require_protocol1_expressible()
        # Protocol 1: optional trailing fields are appended only when needed,
        # so the wire stays byte-identical for any caller that never asked for
        # farm/budget: 6th = mode (1 = farm), 7th = budget_ms (positional, so a
        # budget always sends an explicit mode too).
        suffix = ""
        if self._farm or budget is not None:
            suffix = f" {mode}"
        if budget is not None:
            suffix += f" {budget!r}"
        return head + suffix

    def _search_details(self, tail: Mapping[str, str]) -> dict[str, object]:
        """``Decision.details["search"]``: effective limits + why the search stopped.

        ``stop``/``nodes``/``cprob`` come from the protocol-2 reply tail; a
        protocol-1 binary sends none, so ``stop`` is inferred from the mode
        (a budget search stops on budget, a fixed one on depth) and the rest is
        ``None``. ``max_depth``/``budget_ms`` are the limits of THIS move (the
        ladder mutates them), so every row is self-describing.
        """
        lim = self._limits
        stop = tail.get("stop") or ("budget" if lim.budget_ms is not None else "depth")
        workers_raw = self._engine_info.get("BOT2048_SEARCH_WORKERS")
        return {
            "stop": stop,
            "nodes": int(tail["nodes"]) if "nodes" in tail else None,
            "cprob": float(tail["cprob"]) if "cprob" in tail else None,
            "max_depth": lim.max_depth,
            "budget_ms": lim.budget_ms,
            "max_nodes": lim.max_nodes,
            "workers": int(workers_raw) if workers_raw and workers_raw.isdigit() else None,
        }

    # --- speculative prefetch -------------------------------------------

    def _try_lookup(self, hex_board: str) -> tuple[Direction, float, int, dict[str, str]] | None:
        """Probe the engine prefetch cache; ``None`` on miss.

        A hit is ``(direction, value, depth, tail)`` where ``tail`` is the
        protocol-2 ``stop=/nodes=/cprob=`` dict (empty from a protocol-1 engine).
        """
        reply = self._request(f"lookup {hex_board}")
        fields, tail = _split_reply_tail(reply)
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
        return Direction(int(fields[1])), float(fields[2]), int(fields[3]), tail

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
        # (budget_ms 0 = fixed-depth speculation). The speculation runs under
        # the SAME limits as the real search (protocol 2 appends them), so a
        # cache hit is the decision a direct request would have made.
        mode = 1 if self._farm else 0
        budget = self._limits.budget_ms if self._limits.budget_ms is not None else 0.0
        tail = self._limits_tail() if self._protocol >= 2 else ""
        reply = self._request(
            f"prefetch {board_to_hex(post_swipe)} {self._wire_depth()} {self._p4!r} "
            f"{1 if self._adaptive else 0} {mode} {budget!r}{tail}"
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

        * no budget (fixed depth, or the node-capped unbounded mode) has no
          server-side clock -> ``hang_timeout_s`` alone (default
          :data:`_REPLY_TIMEOUT_FLOOR_S`, 15 s, comfortably above a deep-6
          search and the live endgame budget) -- the watchdog is the only
          wall-clock guard there;
        * budget mode -> ``max(hang_timeout_s, _REPLY_TIMEOUT_BUDGET_FACTOR x
          budget)``; the engine's own hard time abort keeps a real reply far
          under this, so only a genuine stall ever waits it out.
        """
        budget = self._limits.budget_ms
        if budget is not None:
            return max(self._hang_timeout_s, _REPLY_TIMEOUT_BUDGET_FACTOR * budget / 1000.0)
        return self._hang_timeout_s

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
                env=self._env,
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
            self._handshake(proc, q)
        return self._proc

    def _handshake(self, proc: subprocess.Popen[str], q: queue.Queue[str]) -> None:
        """One ``version`` round-trip per spawn: protocol + engine provenance.

        The protocol version decides the wire shape every later request uses
        (a protocol-1 binary must never see the ``k=v`` tail, which it would
        reject), and the parsed reply is the engine's own account of what it
        runs with -- the record a run's provenance is built from.
        """
        assert proc.stdin is not None
        try:
            proc.stdin.write("version\n")
            proc.stdin.flush()
            reply = q.get(timeout=self._hang_timeout_s)
        except (OSError, queue.Empty) as exc:
            self._kill_stalled_proc()
            raise GoEngineError(f"engine version handshake failed: {exc!r}") from exc
        if not reply:
            self._proc = None
            raise GoEngineError(f"engine exited during handshake (code {proc.poll()})")
        info = _parse_version_reply(reply.strip())
        info["engine"] = engine_kind(info, self._binary)
        self._engine_info = info
        protocol = info.get("protocol", "1")
        self._protocol = int(protocol) if protocol.isdigit() else 1

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
