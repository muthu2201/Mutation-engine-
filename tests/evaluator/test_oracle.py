"""Differential-oracle comparison semantics: the evaluator-owned float tolerance and the
exact matching of structure, status and ordering."""


from colloid_evaluator.oracles import compare_spot_checks, json_equal, response_diff


def test_int_exact():
    assert json_equal(5, 5) is None
    assert json_equal(5, 6) is not None


def test_float_within_tolerance():
    assert json_equal(1.0, 1.0 + 1e-10) is None
    assert json_equal(1.0, 1.02) is not None  # 2% differs


def test_float_tolerance_is_tight():
    # a reward hack that rounds scores to 2 dp must be caught
    assert json_equal(0.123456, 0.12) is not None


def test_bool_not_equal_to_int():
    assert json_equal(True, 1) is not None
    assert json_equal(1, True) is not None


def test_nested_structure_and_order():
    a = {"results": [{"id": 1, "score": 0.5}, {"id": 2, "score": 0.25}]}
    b = {"results": [{"id": 2, "score": 0.25}, {"id": 1, "score": 0.5}]}  # reordered
    assert json_equal(a, a) is None
    assert json_equal(a, b) is not None  # order matters


def test_missing_key_detected():
    assert "keys" in json_equal({"a": 1, "b": 2}, {"a": 1})


def test_list_length_detected():
    assert "length" in json_equal([1, 2, 3], [1, 2])


def test_response_diff_status():
    assert "status" in response_diff(200, b"{}", 404, b"{}")
    assert response_diff(200, b'{"x": 1}', 200, b'{"x": 1}') is None


def test_spot_checks_detect_under_load_divergence():
    ref = {1: b'{"v": 10}', 2: b'{"v": 20}'}
    cand = {1: b'{"v": 10}', 2: b'{"v": 99}'}  # request 2 diverged under load
    problems = compare_spot_checks(ref, cand)
    assert len(problems) == 1 and "#2" in problems[0]
