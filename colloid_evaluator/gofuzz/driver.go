// Differential fuzz driver for the Go implementation's pure functions (evaluator-owned,
// never visible to candidates): the Go counterpart of native_fuzz.c.
//
// The evaluator copies the baseline package into ./base and the candidate package into
// ./cand (package clause rewritten, plus an exported wrapper file), builds this driver with
// both, and for N random inputs requires identical results:
//
//	levenshtein, shopTokenize, tokenize, formatMoney, pyRound   exact equality
//	fuzzySimilarity                                             |a-b| <= 1e-12 * max(1,|a|)
//	scoreBatch                                                  same length, rel 1e-9 / abs 1e-12
//	parseInt (strings)                                          same value and same error message
//
// Inputs mix ASCII letters, digits, punctuation, whitespace runs, empty and long strings and
// bytes >= 0x80 (UTF-8 fragments): where tokenisers and edit-distance code typically break.
// A panic in the candidate is a failure; a panic in the baseline is a driver bug.
//
// Usage: driver <seed> <iterations>      exit 0 = equivalent, 1 = mismatch, 2 = usage
package main

import (
	"fmt"
	"math"
	"math/rand"
	"os"
	"reflect"
	"strconv"

	base "stackzero/shop/base"
	cand "stackzero/shop/cand"
)

var vocab = []string{"leather", "backpack", "steel", "kettle", "rugged", "waterproof", "the", "and", "for", "lamp", "oak",
	"titanium", "wireless", "headphones", "a", "x2", "bamboo", "mug", "thermos", "of"}

func randString(r *rand.Rand, maxLen int) string {
	n := r.Intn(maxLen + 1)
	b := make([]byte, n)
	for i := range b {
		switch k := r.Intn(10); {
		case k < 5:
			b[i] = byte('a' + r.Intn(26))
		case k < 6:
			b[i] = byte('A' + r.Intn(26))
		case k < 7:
			b[i] = byte('0' + r.Intn(10))
		case k < 8:
			b[i] = " \t\n,.-_!&"[r.Intn(9)]
		default:
			b[i] = byte(0x80 + r.Intn(0x80))
		}
	}
	return string(b)
}

func word(r *rand.Rand) string {
	if r.Intn(4) == 0 {
		return randString(r, 40)
	}
	w := vocab[r.Intn(len(vocab))]
	if len(w) >= 4 && r.Intn(3) == 0 { // a typo
		i := 1 + r.Intn(len(w)-2)
		w = w[:i] + w[i+1:]
	}
	if r.Intn(5) == 0 {
		w = string([]byte{byte('A' + r.Intn(26))}) + w
	}
	return w
}

func sentence(r *rand.Rand, maxWords int) string {
	n := r.Intn(maxWords + 1)
	s := ""
	for i := 0; i < n; i++ {
		if i > 0 {
			s += []string{" ", "  ", ", ", "-", " & "}[r.Intn(5)]
		}
		s += word(r)
	}
	return s
}

func closeFloat(a, b, rel, abs float64) bool {
	if math.IsNaN(a) || math.IsNaN(b) {
		return math.IsNaN(a) && math.IsNaN(b)
	}
	return a == b || math.Abs(a-b) <= math.Max(abs, rel*math.Max(math.Abs(a), math.Abs(b)))
}

func fail(format string, args ...any) {
	fmt.Printf("MISMATCH "+format+"\n", args...)
	os.Exit(1)
}

// call runs f, turning a candidate panic into a reported failure.
func call[T any](what string, f func() T) (out T) {
	defer func() {
		if p := recover(); p != nil {
			fail("%s: candidate panicked: %v", what, p)
		}
	}()
	return f()
}

func main() {
	if len(os.Args) != 3 {
		fmt.Println("usage: driver <seed> <iterations>")
		os.Exit(2)
	}
	seed, _ := strconv.ParseInt(os.Args[1], 10, 64)
	iterations, _ := strconv.Atoi(os.Args[2])
	r := rand.New(rand.NewSource(seed))
	for it := 0; it < iterations; it++ {
		a, b := word(r), word(r)
		if x, y := base.Levenshtein(a, b), call("levenshtein", func() int { return cand.Levenshtein(a, b) }); x != y {
			fail("levenshtein(%q, %q) = %d, baseline %d", a, b, y, x)
		}
		if x, y := base.FuzzySimilarity(a, b), call("fuzzySimilarity", func() float64 { return cand.FuzzySimilarity(a, b) }); !closeFloat(x, y, 1e-12, 1e-12) {
			fail("fuzzySimilarity(%q, %q) = %v, baseline %v", a, b, y, x)
		}
		text := sentence(r, 30)
		if x, y := base.ShopTokenize(text), call("shopTokenize", func() []string { return cand.ShopTokenize(text) }); !reflect.DeepEqual(x, y) {
			fail("shopTokenize(%q) = %q, baseline %q", text, y, x)
		}
		if x, y := base.Tokenize(text), call("tokenize", func() []string { return cand.Tokenize(text) }); !reflect.DeepEqual(x, y) {
			fail("tokenize(%q) = %q, baseline %q", text, y, x)
		}
		terms := make([]string, r.Intn(5))
		for i := range terms {
			terms[i] = word(r)
		}
		docs := make([]string, r.Intn(40))
		for i := range docs {
			docs[i] = sentence(r, 25)
		}
		x, y := base.ScoreBatch(terms, docs), call("scoreBatch", func() []float64 { return cand.ScoreBatch(terms, docs) })
		if len(x) != len(y) {
			fail("scoreBatch(%q, %d docs) returned %d scores, baseline %d", terms, len(docs), len(y), len(x))
		}
		for i := range x {
			if !closeFloat(x[i], y[i], 1e-9, 1e-12) {
				fail("scoreBatch(%q, docs) doc %d (%q) = %v, baseline %v", terms, i, docs[i], y[i], x[i])
			}
		}
		cents := r.Int63n(2_000_000_000) - 1_000_000_000
		if x, y := base.FormatMoney(cents), call("formatMoney", func() string { return cand.FormatMoney(cents) }); x != y {
			fail("formatMoney(%d) = %q, baseline %q", cents, y, x)
		}
		f := (r.Float64() - 0.5) * math.Pow(10, float64(r.Intn(8)))
		n := r.Intn(8)
		if x, y := base.PyRound(f, n), call("pyRound", func() float64 { return cand.PyRound(f, n) }); math.Float64bits(x) != math.Float64bits(y) {
			fail("pyRound(%v, %d) = %v, baseline %v", f, n, y, x)
		}
		s := []string{randString(r, 6), strconv.Itoa(r.Intn(200) - 50), " " + strconv.Itoa(r.Intn(100)) + " ", "1_0", "+7", ""}[r.Intn(6)]
		xv, xe := base.ParseInt(s, "n", 1, 100)
		res := call("parseInt", func() [2]any { v, e := cand.ParseInt(s, "n", 1, 100); return [2]any{v, e} })
		yv, ye := res[0].(int64), res[1].(string)
		if xv != yv || xe != ye {
			fail("parseInt(%q) = %d %q, baseline %d %q", s, yv, ye, xv, xe)
		}
	}
	fmt.Printf("OK %d iterations equivalent\n", iterations)
}
