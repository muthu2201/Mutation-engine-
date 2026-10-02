package main

// Unit tests for pure helpers. Tests are frozen loci: genes can never modify them, and the
// evaluator always runs the baseline copy of these files against the candidate.

import (
	"encoding/json"
	"reflect"
	"testing"
)

func TestTokenizeOrderAndDedupe(t *testing.T) {
	got := tokenize("The Waterproof waterproof Leather BACKPACK, for camping!")
	want := []string{"waterproof", "leather", "backpack", "camping"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("tokenize = %v, want %v", got, want)
	}
}

func TestTokenizeDropsStopwordsAndShortTokens(t *testing.T) {
	if got := tokenize("a b and of x2 to"); !reflect.DeepEqual(got, []string{"x2"}) {
		t.Fatalf("tokenize = %v", got)
	}
	if got := tokenize(""); len(got) != 0 || got == nil {
		t.Fatalf("tokenize(\"\") = %#v, want an empty non-nil slice", got)
	}
}

func TestFormatMoney(t *testing.T) {
	cases := map[int64]string{0: "0.00", 5: "0.05", 123456: "1234.56", -250: "-2.50"}
	for cents, want := range cases {
		if got := formatMoney(cents); got != want {
			t.Errorf("formatMoney(%d) = %q, want %q", cents, got, want)
		}
	}
}

func TestParseIntBounds(t *testing.T) {
	if n, err := parseInt("7", "n", 1, 10); err != nil || n != 7 {
		t.Fatalf("parseInt(7) = %d, %v", n, err)
	}
	for _, bad := range []any{"11", "x", nil, "1__0", json.Number("123456789012345678901234567890")} {
		if _, err := parseInt(bad, "n", 1, 10); err == nil {
			t.Errorf("parseInt(%#v) accepted", bad)
		}
	}
	if n, err := parseInt(" +3 ", "n", 1, 10); err != nil || n != 3 {
		t.Fatalf("parseInt(' +3 ') = %d, %v (Python int() accepts it)", n, err)
	}
	if n, err := parseInt(json.Number("4.9"), "n", 1, 10); err != nil || n != 4 {
		t.Fatalf("parseInt(4.9) = %d, %v (Python int() truncates)", n, err)
	}
}

func TestParseTimestampRequiresTimezone(t *testing.T) {
	ts, err := parseTimestamp("2030-01-01T10:00:00+00:00", "t")
	if err != nil || ts.Year() != 2030 {
		t.Fatalf("parseTimestamp = %v, %v", ts, err)
	}
	if _, err := parseTimestamp("2030-01-01T10:00:00", "t"); err == nil {
		t.Fatal("a timestamp without an offset was accepted")
	}
}

func TestScoreExactAndFuzzyMatches(t *testing.T) {
	docs := []string{"Rugged Leather Backpack 120", "Steel Kettle Glass 300", "Rugged Lether Bakpack 120"}
	scores := scoreBatch([]string{"leather", "backpack"}, docs)
	// equal-length documents: exact matches outrank typo matches, unrelated text scores 0
	if !(scores[0] > scores[2] && scores[2] > 0.0) || scores[1] != 0.0 {
		t.Fatalf("scores = %v", scores)
	}
}

func TestScoreEmpty(t *testing.T) {
	if got := scoreBatch([]string{"x"}, nil); len(got) != 0 {
		t.Fatalf("scoreBatch(no docs) = %v", got)
	}
	if got := scoreBatch(nil, []string{"anything here"}); !reflect.DeepEqual(got, []float64{0.0}) {
		t.Fatalf("scoreBatch(no terms) = %v", got)
	}
}

func TestPyRoundMatchesPython(t *testing.T) {
	// round(2.675, 2) == 2.67 in Python: the double is slightly below 2.675.
	if got := pyRound(2.675, 2); got != 2.67 {
		t.Fatalf("pyRound(2.675, 2) = %v", got)
	}
	if got := pyRound(0.0005, 3); got != 0.001 {
		t.Fatalf("pyRound(0.0005, 3) = %v", got)
	}
}
