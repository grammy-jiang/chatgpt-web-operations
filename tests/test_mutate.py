"""T0 tests for tests/mutate.py, the mutation runner of P10: which sites it
finds, what each mutant looks like, and which tests it would run."""

from __future__ import annotations

import ast
from pathlib import Path

import mutate

SAMPLE = '''"""Module docstring: never mutated."""


def f(x: int = 3, items: list[str] | None = None) -> bool:
    """Function docstring: never mutated."""
    log(f"value {x}")
    if not items:
        return x < 2 and x != 0
    return items[0] == "a"


if __name__ == "__main__":
    f(1 + 1)
'''


def test_the_sites_skip_docstrings_annotations_fstrings_and_main() -> None:
    kinds = [s.kind for s in mutate.sites_of(SAMPLE)]
    assert kinds == [
        "3 -> 4",  # the default value
        "call -> pass",
        "drop not",
        "return None",
        "And -> Or",
        "Lt -> LtE",
        "2 -> 3",
        "NotEq -> Eq",
        "0 -> 1",
        "return None",
        "Eq -> NotEq",
        "0 -> 1",  # the index
        "'a' -> +'XX'",
    ]


def test_every_mutant_compiles_and_changes_exactly_its_site() -> None:
    sites = mutate.sites_of(SAMPLE)
    unchanged = ast.unparse(ast.parse(SAMPLE))
    for site in sites:
        text = mutate.mutant(SAMPLE, site.index)
        compile(text, "mutant", "exec")
        assert text.strip() != unchanged.strip(), site
    dropped = mutate.mutant(SAMPLE, [s.kind for s in sites].index("drop not"))
    assert "if not not items:" in dropped
    passed = mutate.mutant(SAMPLE, [s.kind for s in sites].index("call -> pass"))
    assert "log(" not in passed and "..." in passed


def test_the_tests_run_name_the_module_or_a_script_that_imports_it(
    tmp_path: Path,
) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "round_state.py").write_text("X = 1\n")
    (tmp_path / "scripts" / "round_status.py").write_text("import round_state\n")
    (tmp_path / "scripts" / "other.py").write_text("import json\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("import round_state\n")
    (tmp_path / "tests" / "test_b.py").write_text("import round_status\n")
    (tmp_path / "tests" / "test_c.py").write_text("import other\n")
    (tmp_path / "tests" / "helper.py").write_text("import round_state\n")
    assert mutate.importers("round_state.py", tmp_path) == ["round_status"]
    assert mutate.tests_for("round_state.py", tmp_path) == [
        "tests/test_a.py",
        "tests/test_b.py",
    ]
