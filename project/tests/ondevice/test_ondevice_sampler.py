"""Reference sampler: color-only read must match the host parser on real frames.

The load-bearing feasibility test is :func:`test_color_only_matches_digit_parser`
-- if the cheap on-device color read did not reproduce the authoritative
digits+color parser on real device screenshots, the whole on-device-offload
design would be unsound. It does (every settled PNG fixture, exactly).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import numpy as np
import numpy.typing as npt
import pytest

from bot2048.ondevice.packet import PacketFlag
from bot2048.ondevice.sampler import (
    UNCERTAIN_MAX_DIST,
    build_packet,
    is_black_frame,
    sample_board_reference,
    sample_cell_exponents,
)
from bot2048.vision.calibration import Calibration
from bot2048.vision.parser import parse_board

Image = npt.NDArray[np.uint8]

#: Lossless PNG game frames shared with the vision suite (see conftest).
GAME_PNG_FIXTURES: tuple[str, ...] = (
    "board_256.png",
    "board_a.png",
    "board_b.png",
    "board_c.png",
    "board_d_labeled.png",
    "board_fresh.png",
    "live_1.png",
    "live_2.png",
    "live_3.png",
    "state_e.png",
)


@pytest.mark.parametrize("name", GAME_PNG_FIXTURES)
def test_color_only_matches_digit_parser(
    name: str, load_image: Callable[[str], Image], calib: Calibration
) -> None:
    """On every real settled board the color-only read == the digit parser."""
    image = load_image(name)
    reference = sample_board_reference(image, calib)
    authoritative = parse_board(image, calib, lenient=True)
    assert reference == authoritative


@pytest.mark.parametrize("name", GAME_PNG_FIXTURES)
def test_real_frames_are_confident(
    name: str, load_image: Callable[[str], Image], calib: Calibration
) -> None:
    """Settled tiles sit far inside the confidence band -> no uncertain flags."""
    image = load_image(name)
    _, dists = sample_cell_exponents(image, calib)
    assert max(dists) <= UNCERTAIN_MAX_DIST
    packet = build_packet(image, calib, seq=1)
    assert packet.uncertain == 0
    assert PacketFlag.HAS_UNCERTAIN not in packet.flags


def _empty_colors(calib: Calibration) -> list[tuple[int, int, int]]:
    empty_rgb = next(rgb for rgb, exp in calib.palette if exp == 0)
    return [empty_rgb] * 16


def test_off_palette_cell_flagged_uncertain(
    calib: Calibration,
    paint_board: Callable[[Sequence[tuple[int, int, int]]], Image],
) -> None:
    """A cell whose color is far from every palette entry trips the uncertain bit."""
    colors = _empty_colors(calib)
    colors[6] = (255, 255, 255)  # white: far from every calibrated tile color
    image = paint_board(colors)
    packet = build_packet(image, calib, seq=3)
    assert 6 in packet.uncertain_cells()
    assert PacketFlag.HAS_UNCERTAIN in packet.flags


def test_board_changed_flag_tracks_prev_board(
    calib: Calibration, load_image: Callable[[str], Image]
) -> None:
    image = load_image("board_a.png")
    board = sample_board_reference(image, calib)
    unchanged = build_packet(image, calib, seq=2, prev_board=board)
    assert PacketFlag.BOARD_CHANGED not in unchanged.flags
    shifted = (board[1:] + board[:1])  # any different arrangement
    changed = build_packet(image, calib, seq=3, prev_board=shifted)
    assert PacketFlag.BOARD_CHANGED in changed.flags


def test_first_packet_without_prev_has_no_changed_flag(
    calib: Calibration, load_image: Callable[[str], Image]
) -> None:
    image = load_image("board_a.png")
    packet = build_packet(image, calib, seq=0)
    assert PacketFlag.BOARD_CHANGED not in packet.flags


def test_black_frame_sets_secure_flag_and_zero_board(calib: Calibration) -> None:
    width, height = calib.screen_size
    black = np.zeros((height, width, 3), dtype=np.uint8)
    assert is_black_frame(black)
    packet = build_packet(black, calib, seq=9)
    assert PacketFlag.SECURE_SUSPECTED in packet.flags
    assert set(packet.board) == {0}
    assert packet.uncertain == 0


def test_seq_and_timestamp_wrap_into_range(
    calib: Calibration, load_image: Callable[[str], Image]
) -> None:
    image = load_image("board_a.png")
    packet = build_packet(image, calib, seq=70_000, device_ts_ms=1 << 33)
    assert packet.seq == 70_000 & 0xFFFF
    assert 0 <= packet.device_ts_ms <= 0xFFFFFFFF


def test_packet_round_trips_from_real_frame(
    calib: Calibration, load_image: Callable[[str], Image]
) -> None:
    from bot2048.ondevice.packet import BoardPacket

    image = load_image("board_256.png")
    packet = build_packet(image, calib, seq=11, device_ts_ms=555)
    assert BoardPacket.decode(packet.encode()) == packet
