"""Board <-> hex codec round-trip and validation."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from bot2048.storage import StorageError, board_from_hex, board_to_hex

boards = st.lists(st.integers(min_value=0, max_value=15), min_size=16, max_size=16).map(tuple)


@given(board=boards)
def test_roundtrip(board: tuple[int, ...]) -> None:
    encoded = board_to_hex(board)
    assert len(encoded) == 16
    assert board_from_hex(encoded) == board


def test_known_encodings() -> None:
    assert board_to_hex((0,) * 16) == "0000000000000000"
    assert board_to_hex((0,) * 15 + (10,)) == "000000000000000a"
    assert board_to_hex((1, 2, 3, 4) + (0,) * 12) == "1234" + "0" * 12
    assert board_from_hex("000000000000000A") == (0,) * 15 + (10,)  # uppercase tolerated


def test_board_wrong_length_rejected() -> None:
    with pytest.raises(StorageError):
        board_to_hex((0,) * 15)


def test_exponent_out_of_range_rejected() -> None:
    with pytest.raises(StorageError):
        board_to_hex((16,) + (0,) * 15)
    with pytest.raises(StorageError):
        board_to_hex((-1,) + (0,) * 15)


def test_hex_wrong_length_rejected() -> None:
    with pytest.raises(StorageError):
        board_from_hex("0" * 15)


def test_hex_invalid_char_rejected() -> None:
    with pytest.raises(StorageError):
        board_from_hex("g" + "0" * 15)
