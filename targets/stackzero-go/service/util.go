package main

import (
	"encoding/json"
	"fmt"
	"math"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode"
)

var stopwords = []string{"a", "an", "and", "the", "for", "with", "of", "to", "in", "on", "by", "at", "or"}

// HTTPError is a client-visible error: the status code and the message of the JSON body.
type HTTPError struct {
	Status  int
	Message string
}

func (e *HTTPError) Error() string { return e.Message }

func httpError(status int, format string, args ...any) *HTTPError {
	return &HTTPError{Status: status, Message: fmt.Sprintf(format, args...)}
}

// errOverflow mirrors the Python reference, where int(float('inf')) raises OverflowError,
// which is not a client error: the request fails with 500.
var errOverflow = fmt.Errorf("cannot convert float infinity to integer")

// tokenize returns lower-case alphanumeric words, without stopwords or duplicates, in
// first-seen order.
func tokenize(text string) []string {
	lower := strings.ToLower(text)
	terms := []string{}
	start := -1
	for i := 0; i <= len(lower); i++ {
		if i < len(lower) && isWordByte(lower[i]) {
			if start < 0 {
				start = i
			}
			continue
		}
		if start < 0 {
			continue
		}
		word := lower[start:i]
		start = -1
		if contains(stopwords, word) || len(word) < 2 {
			continue
		}
		if !contains(terms, word) {
			terms = append(terms, word)
		}
	}
	return terms
}

func isWordByte(c byte) bool {
	return (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9')
}

func contains(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

func formatMoney(cents int64) string {
	sign := ""
	if cents < 0 {
		sign = "-"
		cents = -cents
	}
	return fmt.Sprintf("%s%d.%02d", sign, cents/100, cents%100)
}

// pyRound rounds to ndigits decimal places exactly like Python's round(x, ndigits): the
// correctly rounded decimal value of the binary double, ties to even.
func pyRound(x float64, ndigits int) float64 {
	if math.IsNaN(x) || math.IsInf(x, 0) {
		return x
	}
	v, _ := strconv.ParseFloat(strconv.FormatFloat(x, 'f', ndigits, 64), 64)
	return v
}

// parseInt converts a query-string or JSON value to an integer with Python int()
// semantics (strings: surrounding whitespace, sign and digit-group underscores allowed;
// floats truncate; booleans are 0/1) and checks the inclusive range.
func parseInt(value any, name string, low, high int64) (int64, error) {
	notInt := httpError(400, "%s must be an integer", name)
	outOfRange := httpError(400, "%s must be between %d and %d", name, low, high)
	var n int64
	switch v := value.(type) {
	case string:
		parsed, big, ok := pyIntString(v)
		if !ok {
			return 0, notInt
		}
		if big {
			return 0, outOfRange
		}
		n = parsed
	case json.Number:
		s := string(v)
		if !strings.ContainsAny(s, ".eE") {
			parsed, big, ok := pyIntString(s)
			if !ok {
				return 0, notInt
			}
			if big {
				return 0, outOfRange
			}
			n = parsed
			break
		}
		f, err := strconv.ParseFloat(s, 64)
		if err != nil && !math.IsInf(f, 0) {
			return 0, notInt
		}
		if math.IsInf(f, 0) {
			return 0, errOverflow
		}
		f = math.Trunc(f)
		if f < float64(math.MinInt64) || f >= float64(math.MaxInt64) {
			return 0, outOfRange
		}
		n = int64(f)
	case bool:
		if v {
			n = 1
		}
	default:
		return 0, notInt
	}
	if n < low || n > high {
		return 0, outOfRange
	}
	return n, nil
}

// pyIntString parses a base-10 integer literal the way Python's int(str) does. big reports a
// value too large for int64 (always out of any range this service checks).
func pyIntString(s string) (n int64, big bool, ok bool) {
	s = strings.TrimFunc(s, pyIsSpace)
	neg := false
	if s != "" && (s[0] == '+' || s[0] == '-') {
		neg = s[0] == '-'
		s = s[1:]
	}
	if s == "" || s[0] == '_' || s[len(s)-1] == '_' {
		return 0, false, false
	}
	digits := make([]byte, 0, len(s))
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case c >= '0' && c <= '9':
			digits = append(digits, c)
		case c == '_' && s[i-1] != '_':
		default:
			return 0, false, false
		}
	}
	trimmed := strings.TrimLeft(string(digits), "0")
	if len(trimmed) > 18 {
		return 0, true, true
	}
	if trimmed == "" {
		return 0, false, true
	}
	v, err := strconv.ParseInt(trimmed, 10, 64)
	if err != nil {
		return 0, true, true
	}
	if neg {
		v = -v
	}
	return v, false, true
}

func pyIsSpace(r rune) bool {
	return unicode.IsSpace(r) || (r >= 0x1c && r <= 0x1f)
}

// parseDate accepts an ISO calendar date (YYYY-MM-DD or YYYYMMDD); a missing value is invalid.
func parseDate(value string, present bool, name string) (time.Time, error) {
	bad := httpError(400, "%s must be an ISO date (YYYY-MM-DD)", name)
	if !present {
		return time.Time{}, bad
	}
	for _, layout := range []string{"2006-01-02", "20060102"} {
		if len(value) != len(layout) {
			continue
		}
		if d, err := time.Parse(layout, value); err == nil {
			return d, nil
		}
	}
	return time.Time{}, bad
}

var isoTimestamp = regexp.MustCompile(
	`^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2})(?::(\d{2})(?::(\d{2})(?:[.,](\d+))?)?)?)?` +
		`(Z|[+-]\d{2}(?::?\d{2}(?::?\d{2}(?:\.\d{1,6})?)?)?)?$`)

// parseTimestamp accepts an ISO 8601 timestamp, which must carry a timezone offset.
func parseTimestamp(value any, name string) (time.Time, error) {
	bad := httpError(400, "%s must be an ISO timestamp", name)
	s, ok := value.(string)
	if !ok {
		return time.Time{}, bad
	}
	m := isoTimestamp.FindStringSubmatch(s)
	if m == nil {
		return time.Time{}, bad
	}
	num := func(x string) int {
		if x == "" {
			return 0
		}
		v, _ := strconv.Atoi(x)
		return v
	}
	year, month, day := num(m[1]), num(m[2]), num(m[3])
	hour, minute, second := num(m[4]), num(m[5]), num(m[6])
	if month < 1 || month > 12 || day < 1 || day > daysIn(year, month) || hour > 23 || minute > 59 || second > 59 || year < 1 {
		return time.Time{}, bad
	}
	frac := m[7]
	if len(frac) > 6 {
		frac = frac[:6]
	}
	micros := 0
	if frac != "" {
		micros = num(frac + strings.Repeat("0", 6-len(frac)))
	}
	if m[8] == "" {
		return time.Time{}, httpError(400, "%s must include a timezone offset", name)
	}
	offset := 0
	if m[8] != "Z" {
		sign := 1
		if m[8][0] == '-' {
			sign = -1
		}
		parts := strings.ReplaceAll(m[8][1:], ":", "")
		h := num(parts[:2])
		mins, secs := 0, 0
		if len(parts) >= 4 {
			mins = num(parts[2:4])
		}
		if len(parts) >= 6 {
			secs = num(parts[4:6])
		}
		if mins > 59 || secs > 59 || h > 23 {
			return time.Time{}, bad
		}
		offset = sign * (h*3600 + mins*60 + secs)
	}
	t := time.Date(year, time.Month(month), day, hour, minute, second, micros*1000, time.FixedZone("", offset))
	return t, nil
}

func daysIn(year, month int) int {
	return time.Date(year, time.Month(month)+1, 0, 0, 0, 0, 0, time.UTC).Day()
}

// isoformat renders a timestamp like Python's datetime.isoformat() for the UTC timestamps
// the database returns (the cluster runs with timezone = 'UTC').
func isoformat(t time.Time) string {
	t = t.UTC()
	s := t.Format("2006-01-02T15:04:05")
	if us := t.Nanosecond() / 1000; us != 0 {
		s += fmt.Sprintf(".%06d", us)
	}
	return s + "+00:00"
}
