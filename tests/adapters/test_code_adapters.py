"""CodeRepresentation adapters: Python unit extraction + byte-exact splice; C via clang."""

import shutil

import pytest

from colloid.adapters.code.python_ast import PythonAstCode

PY = '''import os


def helper(a, b):
    return a + b


class Service:
    async def handle(self, db, ids):
        """docstring."""
        out = []
        for i in ids:
            out.append(i)
        return out
'''


def test_python_units_and_sql_and_calls():
    code = PythonAstCode()
    units = code.units_from_text("def q(db):\n    return db.fetch('SELECT * FROM products WHERE id = %s')\n", "h.py")
    assert units[0].sql == ("SELECT * FROM products WHERE id = %s",)
    assert "db.fetch" in units[0].calls


def test_python_method_qualname_and_async():
    code = PythonAstCode()
    units = {u.name: u for u in code.units_from_text(PY, "svc.py")}
    assert "Service.handle" in units and units["Service.handle"].is_async
    assert "helper" in units


def test_python_replace_is_byte_exact_and_reindents():
    code = PythonAstCode()
    unit = code.find(PY, "svc.py", "Service.handle")
    new_body = "async def handle(self, db, ids):\n    return list(ids)\n"
    result = code.replace(PY, unit, new_body)
    assert "        return list(ids)" in result  # reindented to method level
    assert "def helper(a, b):" in result  # other code untouched
    import ast

    ast.parse(result)


def test_python_replace_relocates_after_drift():
    code = PythonAstCode()
    # replace helper first (shifts line numbers), then handle using a stale unit object
    unit_helper = code.find(PY, "svc.py", "helper")
    step1 = code.replace(PY, unit_helper, "def helper(a, b):\n    return b + a\n")
    unit_handle_stale = code.find(PY, "svc.py", "Service.handle")  # from original text
    step2 = code.replace(step1, unit_handle_stale, "async def handle(self, db, ids):\n    return ids\n")
    import ast

    ast.parse(step2)
    assert "return b + a" in step2 and "return ids" in step2


@pytest.mark.skipif(not shutil.which("clang"), reason="clang not installed")
def test_c_units(tmp_path):
    from colloid.adapters.code.c_clang import ClangCCode

    (tmp_path / "m.c").write_text("#include <string.h>\nint add(int a, int b) {\n    return a + b;\n}\nint twice(int x) {\n    return add(x, x);\n}\n")
    code = ClangCCode()
    units = {u.name: u for u in code.units(tmp_path, "m.c")}
    assert "add" in units and "twice" in units
    assert "add" in units["twice"].calls
    spliced = code.replace((tmp_path / "m.c").read_text(), units["add"], "int add(int a, int b) {\n    return b + a;\n}\n")
    assert "return b + a" in spliced
