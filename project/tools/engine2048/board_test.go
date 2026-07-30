package main

import (
	"math/rand"
	"os"
	"testing"
)

func TestMain(m *testing.M) {
	initTables()
	os.Exit(m.Run())
}

func randomBoard(rng *rand.Rand) uint64 {
	var b uint64
	for i := 0; i < 16; i++ {
		var v uint64
		if rng.Float64() >= 0.4 { // ~40% empty cells
			v = uint64(rng.Intn(15) + 1)
		}
		b |= v << (4 * i)
	}
	return b
}

// Vertical moves must equal the transpose(horizontal(transpose)) identity —
// the col tables are a fused version of exactly that.
func TestVerticalMovesMatchTransposedHorizontal(t *testing.T) {
	rng := rand.New(rand.NewSource(42))
	for n := 0; n < 5000; n++ {
		b := randomBoard(rng)
		if got, want := executeMove(b, dirUp), transpose(executeMove(transpose(b), dirLeft)); got != want {
			t.Fatalf("up mismatch on %016x: got %016x want %016x", b, got, want)
		}
		if got, want := executeMove(b, dirDown), transpose(executeMove(transpose(b), dirRight)); got != want {
			t.Fatalf("down mismatch on %016x: got %016x want %016x", b, got, want)
		}
	}
}

func TestTransposeIsInvolution(t *testing.T) {
	rng := rand.New(rand.NewSource(7))
	for n := 0; n < 1000; n++ {
		b := randomBoard(rng)
		if transpose(transpose(b)) != b {
			t.Fatalf("transpose(transpose(%016x)) != identity", b)
		}
	}
}

func rowOf(line [4]uint16) uint16 {
	return line[0] | line[1]<<4 | line[2]<<8 | line[3]<<12
}

func TestFixedRowMerges(t *testing.T) {
	cases := []struct {
		in    [4]uint16
		want  [4]uint16
		score float64
	}{
		{[4]uint16{0, 0, 0, 0}, [4]uint16{0, 0, 0, 0}, 0},
		{[4]uint16{1, 1, 0, 0}, [4]uint16{2, 0, 0, 0}, 4},
		{[4]uint16{0, 1, 0, 1}, [4]uint16{2, 0, 0, 0}, 4},
		// each pair merges at most once; merges resolve from the leading edge
		{[4]uint16{2, 2, 2, 0}, [4]uint16{3, 2, 0, 0}, 8},
		{[4]uint16{2, 2, 2, 2}, [4]uint16{3, 3, 0, 0}, 16},
		{[4]uint16{2, 3, 3, 2}, [4]uint16{2, 4, 2, 0}, 16},
		// rank-15 tiles never merge
		{[4]uint16{15, 15, 0, 0}, [4]uint16{15, 15, 0, 0}, 0},
		{[4]uint16{0, 15, 0, 15}, [4]uint16{15, 15, 0, 0}, 0},
		{[4]uint16{14, 14, 14, 14}, [4]uint16{15, 15, 0, 0}, 65536},
	}
	for _, c := range cases {
		row := rowOf(c.in)
		got := row ^ rowLeftTable[row]
		if want := rowOf(c.want); got != want {
			t.Errorf("left(%v): got %04x want %04x", c.in, got, want)
		}
		if scoreTable[row] != c.score {
			t.Errorf("score(%v): got %v want %v", c.in, scoreTable[row], c.score)
		}
	}
}

func TestIllegalMoveLeavesBoardUnchanged(t *testing.T) {
	// Two rank-15 tiles flush left: LEFT changes nothing.
	board, err := parseBoardHex("ff00000000000000")
	if err != nil {
		t.Fatal(err)
	}
	if executeMove(board, dirLeft) != board {
		t.Fatalf("left move on %016x should be illegal", board)
	}
	if executeMove(board, dirRight) == board {
		t.Fatalf("right move on %016x should be legal", board)
	}
}

func TestBoardHexRoundTrip(t *testing.T) {
	board, err := parseBoardHex("0123456789abcdef")
	if err != nil {
		t.Fatal(err)
	}
	// digit j (leftmost = j=0) = exponent of cell j -> cell 0 is the low nibble
	if board&0xF != 0 || (board>>60)&0xF != 15 {
		t.Fatalf("hex digit order wrong: %016x", board)
	}
	if formatBoardHex(board) != "0123456789abcdef" {
		t.Fatalf("round trip failed: %q", formatBoardHex(board))
	}
	if _, err := parseBoardHex("012"); err == nil {
		t.Fatal("short input must be rejected")
	}
	if _, err := parseBoardHex("0123456789abcdeg"); err == nil {
		t.Fatal("non-hex input must be rejected")
	}
}
