"""The Go policy scanner (cascade L0 for Go genes), on the real Go implementation's sources."""

import shutil
from pathlib import Path

import pytest

from colloid_evaluator.policy import scan_go

pytestmark = pytest.mark.skipif(shutil.which("go") is None, reason="needs the Go toolchain")

SERVICE = Path(__file__).resolve().parents[2] / "targets" / "stackzero-go" / "service"
RATING = '''func ratingSummary(ctx context.Context, db Querier, productID int64) (int64, *float64, error) {
	row, _, err := fetchRow[ratingRow](ctx, db, "SELECT count(*), avg(rating)::float8 FROM reviews WHERE product_id = $1", productID)
	return row.Count, row.Average, err
}
'''


def scan(src: str, base: str = RATING, name: str = "ratingSummary", file: str = "search.go") -> list[str]:
    return scan_go(src, base, name, SERVICE / file)


def with_body(extra: str) -> str:
    return RATING.replace("\trow, _, err :=", extra + "\trow, _, err :=")


def test_baseline_is_clean():
    assert scan(RATING) == []


@pytest.mark.parametrize("extra,why", [
    ("\tgo func() {}()\n", "goroutines"),
    ("\tstopwords = append(stopwords, \"x\")\n", "package-level state"),
    ("\tvar ch chan int\n\t_ = ch\n", "channels"),
    ("\tif p, ok := db.(interface{ Stat() int }); ok {\n\t\t_ = p.Stat()\n\t}\n", "introspection"),
    ("\t_, _ = db.Exec(ctx, \"SELECT set_config('a.b', $1::text, false)\", productID)\n", "set_config"),
    ("\t_ = \"/opt/colloid/state\"\n", "evaluator or system paths"),
])
def test_state_concurrency_and_introspection_are_rejected(extra, why):
    reasons = scan(with_body(extra))
    assert any(why in r for r in reasons), reasons


def test_clock_access_is_rejected_where_time_is_imported():
    base = '''func categoryTop(ctx context.Context, db Querier, categoryID int64, limit int64) (any, error) {
	return nil, nil
}
'''
    src = base.replace("\treturn nil, nil", "\tif time.Now().Unix() > 0 {\n\t\treturn nil, nil\n\t}\n\treturn nil, nil")
    reasons = scan(src, base, "categoryTop", "handlers.go")
    assert any("time.Now" in r for r in reasons), reasons


def test_confinement_and_signature():
    assert any("confinement" in r for r in scan(RATING + "\nfunc extra() {}\n"))
    assert any("signature" in r for r in scan(RATING.replace("productID int64)", "pid int64)").replace("productID)", "pid)")))
    assert any("directives" in r for r in scan("//go:noinline\n" + RATING))


def test_mutating_an_argument_is_rejected_but_local_state_is_fine():
    base = '''func tokenize(text string) []string {
	return nil
}
'''
    local = base.replace("\treturn nil", "\tterms := map[string]int{}\n\tterms[text] = 1\n\tout := []string{}\n\tfor k := range terms {\n\t\tout = append(out, k)\n\t}\n\treturn out")
    assert scan(local, base, "tokenize", "util.go") == []
    mutate = '''func rank(terms []string, candidates []candidate, limit int) []scoredCandidate {
	candidates[0].Name = "x"
	return nil
}
'''
    base_rank = mutate.replace('\tcandidates[0].Name = "x"\n', "")
    reasons = scan(mutate, base_rank, "rank", "search.go")
    assert any("mutates an argument" in r for r in reasons), reasons
