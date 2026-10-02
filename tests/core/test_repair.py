"""Pure parts of the SWE-bench repair operators (ADR 0011)."""

import ast

import pytest

from colloid.core import repair

VALIDATORS = '''import re

from django.core import validators


class ASCIIUsernameValidator(validators.RegexValidator):
    regex = r'^[\\w.@+-]+$'
    message = "Enter a valid username."
    flags = re.ASCII


def helper(x):
    return x + 1
'''

ISSUE = """UsernameValidator allows trailing newline in usernames
ASCIIUsernameValidator and UnicodeUsernameValidator use the regex r'^[\\w.@+-]+$'. Because `$` matches before a
trailing newline, usernames ending with a newline are accepted. See django/contrib/auth/validators.py.
Traceback (most recent call last):
  File "/testbed/django/contrib/auth/validators.py", line 10, in helper
"""


def test_snippets_cover_functions_and_class_level_blocks():
    snips = {s.name: s for s in repair.snippets(VALIDATORS, "django/contrib/auth/validators.py")}
    assert "helper" in snips and snips["helper"].kind == "function"
    block = next(s for s in snips.values() if s.name.startswith("ASCIIUsernameValidator.<L"))
    assert block.kind == "block" and "regex = " in block.text and block.indent == "    "


def test_splicing_a_class_attribute_fix_keeps_the_file_valid():
    block = next(s for s in repair.snippets(VALIDATORS, "v.py") if s.name.startswith("ASCIIUsernameValidator.<L"))
    fixed = block.text.replace("r'^[\\w.@+-]+$'", "r'\\A[\\w.@+-]+\\Z'")
    new = repair.splice(VALIDATORS, block, fixed)
    assert "    regex = r'\\A[\\w.@+-]+\\Z'" in new and new.count("class ASCIIUsernameValidator") == 1
    ast.parse(new)
    with pytest.raises(SyntaxError):
        repair.splice(VALIDATORS, block, "regex = (")


def test_mentions_and_ranking_follow_what_the_issue_names():
    m = repair.mentions(ISSUE)
    assert "django/contrib/auth/validators.py" in m.paths and ("/testbed/django/contrib/auth/validators.py", "helper") in m.frames
    assert {"ASCIIUsernameValidator", "UnicodeUsernameValidator"} <= m.names
    files = {"django/contrib/auth/validators.py": VALIDATORS, "django/utils/text.py": "def slugify(value):\n    return value\n"}
    ranked = repair.rank_files(ISSUE, files)
    assert ranked[0][0] == "django/contrib/auth/validators.py"
    snips = repair.rank_snippets(ISSUE, ranked, files)
    assert snips[0][0].file == "django/contrib/auth/validators.py"


def test_parse_fix_rejects_what_cannot_be_a_fix():
    fn = next(s for s in repair.snippets(VALIDATORS, "v.py") if s.name == "helper")
    assert not repair.parse_fix("no code here", fn, VALIDATORS).ok
    assert repair.parse_fix(f"```python\n{fn.text}```", fn, VALIDATORS).reason == "identical to the original"
    assert "does not define helper" in repair.parse_fix("```python\ndef other(x):\n    return x\n```", fn, VALIDATORS).reason
    ok = repair.parse_fix("Off by one.\n```python\ndef helper(x):\n    return x + 2\n```", fn, VALIDATORS)
    assert ok.ok and "return x + 2" in ok.file_text and "class ASCIIUsernameValidator" in ok.file_text


def test_reproduction_scripts_and_their_outcomes():
    script = repair.parse_repro("```python\ntry:\n    print('ISSUE REPRODUCED')\nexcept Exception:\n    print('OTHER')\n# ISSUE RESOLVED\n```")
    assert script is not None and repair.parse_repro("```python\nprint('ISSUE REPRODUCED')\n```") is None
    assert repair.repro_outcome("noise\nISSUE REPRODUCED\n") == "ISSUE REPRODUCED"
    assert repair.repro_outcome("ANOTHER thing\n") == "NONE" and repair.repro_outcome("ISSUE RESOLVED\nOTHER\n") == "OTHER"
