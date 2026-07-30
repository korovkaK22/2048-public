"""Terminal outcomes + board-diff math for the live loop (refactor plan R3).

Extracted VERBATIM from :mod:`bot2048.runner.live`:

* :func:`diff_spawn` -- the single-new-tile board diff that is BOTH the swipe
  verification of the live loop and the measurement instrument of research
  mode (re-exported from ``bot2048.runner.live`` -- it is in that module's
  ``__all__`` and used by research/external callers).
* :func:`_merge_targets` -- the auto-learn gate's merge cross-check.
* the terminal-popup exceptions :class:`_WinPopup2048` / :class:`_GameOverPopup`
  / :class:`_GameReset` and :data:`WIN_EXPONENT`.
* the FATAL-code -> ``end_reason`` map :data:`_ABORT_REASONS` and
  :func:`_abort_reason`.

All pure/free-standing (no ``self``); :mod:`bot2048.runner.live` re-imports what
it still uses.
"""

from __future__ import annotations

from bot2048.core.types import BOARD_SIZE, Board, Direction, Spawn

# NOTE: module import, not a from-import: device/supervisor.py itself imports
# bot2048.runner.incidents, so binding FatalIncident at import time crashes on
# the half-initialized module whenever the device layer happens to load first.
from bot2048.device import supervisor as _supervisor_mod
from bot2048.runner.alerts import AlertCode


def diff_spawn(predicted: Board, parsed: Board) -> Spawn | None:
    """The single new tile that turns ``predicted`` into ``parsed``, if any.

    Returns the :class:`Spawn` when ``parsed`` equals ``predicted`` plus
    exactly one tile on a previously empty cell; ``None`` for every other
    diff (including identical boards). This one function is both the swipe
    verification of the live loop and the measurement instrument of research
    mode (docs/rules-research.md).
    """
    spawn: Spawn | None = None
    for cell, (p, q) in enumerate(zip(predicted, parsed, strict=True)):
        if p == q:
            continue
        if p == 0 and q > 0 and spawn is None:
            spawn = Spawn(cell=cell, exponent=q)
            continue
        return None
    return spawn


def _merge_targets(board: Board, direction: Direction) -> dict[int, int]:
    """Cells that ``engine.move(board, direction)`` fills by MERGING a pair.

    Returns ``{cell: merged exponent}`` -- the merge TARGETS of this very
    move, each worth exactly 2x the merged pair. Mirrors the engine rules
    (slide toward the leading edge, each pair merges at most once, merges
    resolve from the leading edge, exponent-15 tiles never merge). Used only
    as a cross-check by the auto-learn gate, never to move tiles: if it ever
    disagreed with the engine, the gate simply refuses to learn (safe).
    """
    n = BOARD_SIZE
    if direction is Direction.LEFT:
        lines = [[r * n + c for c in range(n)] for r in range(n)]
    elif direction is Direction.RIGHT:
        lines = [[r * n + c for c in range(n - 1, -1, -1)] for r in range(n)]
    elif direction is Direction.UP:
        lines = [[r * n + c for r in range(n)] for c in range(n)]
    else:  # Direction.DOWN
        lines = [[r * n + c for r in range(n - 1, -1, -1)] for c in range(n)]
    targets: dict[int, int] = {}
    for line in lines:
        placed: list[int] = []  # exponents already slid to the leading edge
        last_was_merge = False
        for idx in line:
            exp = board[idx]
            if exp == 0:
                continue
            if placed and placed[-1] == exp and exp < 15 and not last_was_merge:
                placed[-1] = exp + 1
                last_was_merge = True
                targets[line[len(placed) - 1]] = exp + 1
            else:
                placed.append(exp)
                last_was_merge = False
    return targets


#: The winning tile: 2**11 = 2048. When the app shows the win popup the
#: winning merge is worth exactly this many points.
WIN_EXPONENT = 11

#: ``end_reason`` recorded when ``--stop-before-win`` deliberately holds the
#: board one swipe from 2048. NOT in :data:`_ABORT_REASONS` (that map is only for
#: operational FATAL aborts): this is a NORMAL, intended terminal state -- the
#: bot chose the winning move and, on purpose, did not send it, leaving the human
#: to make the final merge and read the score.
HELD_BEFORE_WIN_REASON = "held_before_win"


class _HeldBeforeWin(Exception):
    """Internal: ``--stop-before-win`` chose a move that WOULD merge to 2048.

    Raised by :meth:`~bot2048.runner.swipe_controller.SwipeController.play_move`
    BEFORE the swipe is issued, when the already-computed predicted post-move
    board contains a 2048 tile (max exponent >= :data:`WIN_EXPONENT`) and
    :attr:`~bot2048.runner.live_config.LiveConfig.stop_before_win` is set. Caught
    by :meth:`LiveRunner._play_game`, which finalizes the game as
    :data:`HELD_BEFORE_WIN_REASON` and STOPS the session (the human makes the
    final swipe). Like :class:`_WinPopup2048` this is a HAPPY terminal state, not
    a FatalIncident and not an error path -- it must never log WARN/FATAL.
    """

    def __init__(self, direction: Direction) -> None:
        super().__init__(f"held one swipe before 2048 (direction {direction.name})")
        self.direction = direction


class _WinPopup2048(Exception):
    """Internal: the «Ви зібрали 2048!» win popup is on screen.

    Raised by :meth:`LiveRunner._capture_game_frame` the moment the state
    detector reports :data:`ScreenState.WIN_POPUP_2048` and caught by
    :meth:`LiveRunner._play_game` (mid-game -> ``end_reason='win_2048'``) or
    :meth:`LiveRunner._acquire_initial_game` (leftover popup at session
    start -> tap through it). Unlike a FatalIncident this is a HAPPY
    terminal state: the game is finalized normally and the session either
    restarts (autorestart: more games budgeted) or ends cleanly.
    """


class _GameOverPopup(Exception):
    """Internal: the «Гру завершено» game-over popup is on screen.

    Raised by :meth:`LiveRunner._capture_game_frame` the moment the state
    detector reports :data:`ScreenState.GAME_OVER_POPUP` and caught by
    :meth:`LiveRunner._play_game` (mid-game -> ``end_reason='game_over'``) or
    :meth:`LiveRunner._acquire_initial_game` (leftover popup at session start
    -> tap through it). Like :class:`_WinPopup2048` this is a TERMINAL state,
    not a FatalIncident: the board filled up, the game ends naturally and the
    session either autorestarts (more games budgeted -> tap «Нова гра») or
    ends cleanly. It is finalized exactly like an engine-decided game over
    (the popup only confirms what ``core.engine.is_game_over`` would decide),
    so the tile histogram + end screenshot are written the same way.
    """


class _GameReset(Exception):
    """Internal: the app regenerated the game (post-auth cold start)."""

    def __init__(self, board: Board, t0: float) -> None:
        super().__init__("game reset by the app")
        self.board = board
        self.t0 = t0


#: FatalIncident alert code -> the ``end_reason`` recorded when it finalizes an
#: open game. Every entry is an OPERATIONAL abort (the game did not truly end
#: and no code bug fired), kept distinct from a generic ``'error'`` so the DB
#: shows what actually happened -- e.g. the bank auth wall (AUTH_REQUIRED) or a
#: human touching the phone mid-run (which surfaces as VISION_MISMATCH /
#: MOVE_REJECTED / SCREEN_UNEXPECTED). Codes are not conflated further because
#: "user tapped the phone" is not distinguishable from a genuine vision glitch.
_ABORT_REASONS: dict[AlertCode, str] = {
    AlertCode.AUTH_REQUIRED: "auth_required",
    AlertCode.VISION_MISMATCH: "vision_mismatch",
    AlertCode.MOVE_REJECTED: "move_rejected",
    AlertCode.SCREEN_UNEXPECTED: "screen_unexpected",
    AlertCode.GAME_OVER_UNEXPECTED: "game_over_unexpected",
    AlertCode.DEVICE_LOST: "device_lost",
}


def _abort_reason(cause: BaseException) -> str:
    """Map a mid-game exception to the ``end_reason`` recorded for the game.

    ``KeyboardInterrupt`` -> ``'interrupted'`` (a deliberate stop); an
    operational :class:`~bot2048.device.supervisor.FatalIncident` -> its
    specific reason (:data:`_ABORT_REASONS`, defaulting to ``'aborted'`` for an
    unmapped code); anything else -> ``'error'`` (a genuine internal crash,
    which must stay visible as such).
    """
    if isinstance(cause, KeyboardInterrupt):
        return "interrupted"
    if isinstance(cause, _supervisor_mod.FatalIncident):
        return _ABORT_REASONS.get(cause.alert.code, "aborted")
    return "error"
