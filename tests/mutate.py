#!/usr/bin/env python3
"""Mutation testing for one module of scripts/ (TESTING.md section 6, P10).

    .venv/bin/python tests/mutate.py MODULE.py [--limit N] [--json PATH] [--force]
    make mutate MODULE=round_state.py

By hand only, never from cron: it costs minutes to hours of CPU. It runs
at the lowest priority (nice 19), one test process at a time, and it
refuses to start while a send is in flight unless ``--force``.

How: a throwaway git worktree of HEAD (the live ``scripts/`` that other
processes import is never touched); every mutation site of the module,
found in its AST, is applied one at a time (the module is re-parsed and
written back with ``ast.unparse``); each mutant runs the offline test files
that name the module or a script importing it (``-x``, file order, no
cache) and is *killed* when a
test fails or times out or the suite cannot run at all (the mutant broke
an import), *survived* when all pass. A baseline run on the
unchanged module must pass first. The report names every survivor by line,
with the original line and the change.

Why not mutmut: mutmut 3 runs the tests against instrumented copies it
imports by package path (``scripts.round_state``), and this skill's tests
import each command as a top-level module from ``scripts/`` on
``sys.path``; its statistics run failed on that layout (2026-09-29).

Operators: comparisons (``==``/``!=``, ``<``/``<=``, ``>``/``>=``,
``in``/``not in``, ``is``/``is not``), ``and``/``or``, dropping a ``not``,
``+``/``-``, ``+=``/``-=``, ``True``/``False``, an integer ``n`` to ``n+1``,
a string compared, indexed or tested for membership to ``s+"XX"``, a
returned value to ``None``, and a bare call statement to ``pass``.
Docstrings, annotations, f-strings and the ``__main__`` block are left
alone.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
T0 = (
    "not live_read and not live_write and not live_browser and not live_send "
    "and not live_local and not replay and not loopback"
)
SEND_PATTERN = (
    r"chatgpt_researc[h].py|scripts/send_promp[t].py|deep_researc[h].py start"
)

SWAP = {
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.LtE, ast.LtE: ast.Lt,
    ast.Gt: ast.GtE, ast.GtE: ast.Gt, ast.In: ast.NotIn, ast.NotIn: ast.In,
    ast.Is: ast.IsNot, ast.IsNot: ast.Is,
}  # fmt: skip
SKIPPED_FIELDS = {"annotation", "returns", "type_comment", "type_params"}


@dataclass
class Site:
    index: int
    line: int
    kind: str


@dataclass
class Outcome:
    index: int
    line: int
    kind: str
    status: str  # killed, survived, timeout, error
    source_line: str
    seconds: float


def _is_docstring(node: ast.AST, parent: ast.AST | None) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        and isinstance(
            parent, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        )
        and parent.body
        and parent.body[0] is node
    )


def _is_main_block(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
    )


class _Walker:
    """Visits mutation sites in a fixed order; with ``apply`` set, changes
    the site with that index and stops counting."""

    def __init__(self, apply: int | None = None) -> None:
        self.apply = apply
        self.sites: list[Site] = []

    def _site(self, node: ast.AST, kind: str) -> bool:
        index = len(self.sites)
        self.sites.append(Site(index, getattr(node, "lineno", 0), kind))
        return index == self.apply

    def walk(
        self, node: ast.AST, parent: ast.AST | None = None, in_test: bool = False
    ) -> None:
        if _is_docstring(node, parent) or _is_main_block(node):
            return
        if isinstance(node, ast.JoinedStr):
            return
        self._visit(node, parent, in_test)
        for field, value in ast.iter_fields(node):
            if field in SKIPPED_FIELDS:
                continue
            children = value if isinstance(value, list) else [value]
            for child in children:
                if isinstance(child, ast.AST):
                    compared = isinstance(node, (ast.Compare, ast.Subscript))
                    self.walk(child, node, compared)

    def _visit(self, node: ast.AST, parent: ast.AST | None, in_test: bool) -> None:
        if isinstance(node, ast.Compare):
            for i, op in enumerate(node.ops):
                other = SWAP.get(type(op))
                if other and self._site(
                    node, f"{type(op).__name__} -> {other.__name__}"
                ):
                    node.ops[i] = other()
        elif isinstance(node, ast.BoolOp):
            other = ast.Or if isinstance(node.op, ast.And) else ast.And
            if self._site(node, f"{type(node.op).__name__} -> {other.__name__}"):
                node.op = other()
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            if self._site(node, "drop not"):
                # "not not x": x's own truth value, whatever x's type ("+x"
                # would crash on a list and count as a kill it is not).
                node.operand = ast.UnaryOp(op=ast.Not(), operand=node.operand)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub)):
            other = ast.Sub if isinstance(node.op, ast.Add) else ast.Add
            if self._site(node, f"{type(node.op).__name__} -> {other.__name__}"):
                node.op = other()
        elif isinstance(node, ast.AugAssign) and isinstance(
            node.op, (ast.Add, ast.Sub)
        ):
            other = ast.Sub if isinstance(node.op, ast.Add) else ast.Add
            if self._site(node, f"{type(node.op).__name__}= -> {other.__name__}="):
                node.op = other()
        elif isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, bool):
                if self._site(node, f"{value} -> {not value}"):
                    node.value = not value
            elif isinstance(value, int):
                if self._site(node, f"{value} -> {value + 1}"):
                    node.value = value + 1
            elif (
                isinstance(value, str)
                and in_test
                and self._site(node, f"{value[:20]!r} -> +'XX'")
            ):
                node.value = value + "XX"
        elif (
            isinstance(node, ast.Return)
            and node.value is not None
            and not (isinstance(node.value, ast.Constant) and node.value.value is None)
            and self._site(node, "return None")
        ):
            node.value = ast.Constant(None)
        elif (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and self._site(node, "call -> pass")
        ):
            node.value = ast.Constant(Ellipsis)  # a bare "..." does nothing


def sites_of(source: str) -> list[Site]:
    walker = _Walker()
    walker.walk(ast.parse(source))
    return walker.sites


def mutant(source: str, index: int) -> str:
    tree = ast.parse(source)
    walker = _Walker(apply=index)
    walker.walk(tree)
    return ast.unparse(ast.fix_missing_locations(tree)) + "\n"


def importers(module: str, root: Path) -> list[str]:
    """The scripts that import ``module``: their tests exercise it too."""
    name = Path(module).stem
    pattern = re.compile(rf"^\s*(?:import {name}\b|from {name} import)", re.M)
    return sorted(
        p.stem
        for p in (root / "scripts").glob("*.py")
        if p.stem != name and pattern.search(p.read_text(encoding="utf-8"))
    )


def tests_for(module: str, root: Path) -> list[str]:
    """The offline test files that name ``module`` or a script importing it."""
    names = [Path(module).stem, *importers(module, root)]
    pattern = re.compile(r"\b(?:" + "|".join(map(re.escape, names)) + r")\b")
    return sorted(
        str(p.relative_to(root))
        for p in (root / "tests").glob("test_*.py")
        if pattern.search(p.read_text(encoding="utf-8"))
    )


def run_tests(root: Path, tests: list[str], timeout: float) -> tuple[str, float]:
    started = time.monotonic()
    command = [
        "nice", "-n", "19", sys.executable, "-m", "pytest", "-x", "-q",
        "-p", "no:randomly", "-p", "no:cacheprovider", "-m", T0, *tests,
    ]  # fmt: skip
    try:
        done = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "timeout", time.monotonic() - started
    status = {0: "survived", 1: "killed"}.get(done.returncode, "error")
    return status, time.monotonic() - started


def send_in_flight() -> bool:
    found = subprocess.run(
        ["pgrep", "-f", SEND_PATTERN], capture_output=True, check=False
    )
    return found.returncode == 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("module", help="a file in scripts/, e.g. round_state.py")
    ap.add_argument("--limit", type=int, default=0, help="stop after N mutants")
    ap.add_argument("--json", default="", help="also write the outcomes here")
    ap.add_argument(
        "--force", action="store_true", help="run while a send is in flight"
    )
    args = ap.parse_args(argv)
    module = Path(args.module).name
    if not (SKILL / "scripts" / module).is_file():
        print(f"no scripts/{module}")
        return 2
    if send_in_flight() and not args.force:
        print(
            "a send is in flight, and mutation testing competes for the CPU; "
            "try later, or pass --force"
        )
        return 2

    with tempfile.TemporaryDirectory(prefix="rp-mutate-") as tmp:
        root = Path(tmp) / "tree"
        subprocess.run(
            [
                "git",
                "-C",
                str(SKILL),
                "worktree",
                "add",
                "-q",
                "--detach",
                str(root),
                "HEAD",
            ],
            check=True,
        )
        try:
            return _mutate(root, module, args)
        finally:
            subprocess.run(
                ["git", "-C", str(SKILL), "worktree", "remove", "--force", str(root)],
                check=False,
            )
            subprocess.run(["git", "-C", str(SKILL), "worktree", "prune"], check=False)


def _mutate(root: Path, module: str, args: argparse.Namespace) -> int:
    target = root / "scripts" / module
    original = target.read_text(encoding="utf-8")
    lines = original.splitlines()
    tests = tests_for(module, root)
    if not tests:
        print(f"no test file names {module}")
        return 2
    status, baseline = run_tests(root, tests, timeout=900)
    if status != "survived":
        print(
            f"the unchanged module does not pass its tests ({status}); nothing measured"
        )
        return 1
    timeout = max(60.0, baseline * 10)
    sites = sites_of(original)
    if args.limit:
        sites = sites[: args.limit]
    print(
        f"{module}: {len(sites)} mutants, {len(tests)} test files, "
        f"baseline {baseline:.1f} s"
    )
    outcomes: list[Outcome] = []
    try:
        for site in sites:
            target.write_text(mutant(original, site.index), encoding="utf-8")
            status, seconds = run_tests(root, tests, timeout)
            text = lines[site.line - 1].strip() if 0 < site.line <= len(lines) else ""
            outcomes.append(
                Outcome(site.index, site.line, site.kind, status, text, seconds)
            )
            print(
                f"  {len(outcomes)}/{len(sites)} line {site.line}: "
                f"{site.kind}: {status}",
                flush=True,
            )
    finally:
        target.write_text(original, encoding="utf-8")
    # A mutant that makes the suite time out or unable to run (a collection
    # error: the module no longer imports) was detected, like a failed test.
    killed = sum(o.status in ("killed", "timeout", "error") for o in outcomes)
    rate = 100.0 * killed / len(outcomes) if outcomes else 0.0
    print(f"\n{module}: {killed}/{len(outcomes)} killed ({rate:.1f} %)")
    survivors = [o for o in outcomes if o.status == "survived"]
    errors = [o for o in outcomes if o.status == "error"]
    for o in survivors:
        print(f"  survived: line {o.line}: {o.kind}    {o.source_line}")
    for o in errors:
        print(f"  killed by an error (no test could run): line {o.line}: {o.kind}")
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "module": module,
                    "tests": tests,
                    "killed": killed,
                    "total": len(outcomes),
                    "outcomes": [asdict(o) for o in outcomes],
                },
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )  # fmt: skip
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    raise SystemExit(main(sys.argv[1:]))
