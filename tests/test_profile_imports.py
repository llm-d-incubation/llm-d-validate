"""The profile package imports without cycles, so no module needs an import inside a function.

An import placed in a function body to dodge a cycle hides the dependency from a reader and from
tools, and the cycle stays. This reads every import in the package, at any depth, and requires
the graph between its modules to be acyclic.
"""

from __future__ import annotations

import ast
from pathlib import Path

import llmdv.profile

PACKAGE = Path(llmdv.profile.__file__).parent


def internal_imports(path: Path) -> set[str]:
    """Modules of this package that `path` imports anywhere in its body."""
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            if node.module:
                found.add(node.module.split(".")[0])
            else:
                found.update(alias.name for alias in node.names)
    return found


def test_the_profile_package_has_no_import_cycle():
    graph = {path.stem: internal_imports(path) for path in PACKAGE.glob("*.py")}
    done: set[str] = set()

    def visit(module: str, chain: tuple[str, ...]) -> None:
        assert module not in chain, f"import cycle: {' -> '.join((*chain, module))}"
        if module in done:
            return
        for imported in sorted(graph.get(module, ())):
            visit(imported, (*chain, module))
        done.add(module)

    for module in sorted(graph):
        visit(module, ())


def test_no_profile_module_imports_inside_a_function():
    for path in PACKAGE.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        nested = [
            f"{path.name}:{inner.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for inner in ast.walk(node)
            if isinstance(inner, (ast.Import, ast.ImportFrom))
        ]
        assert not nested, f"imports inside functions: {nested}"
