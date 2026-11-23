import ast
import sys
from pathlib import Path

CORE = Path(__file__).resolve().parents[2] / "core"
ALLOWED_THIRD_PARTY = {"pydantic", "pydantic_core", "typing_extensions"}


def _imported_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def disallowed_imports(source: str) -> set[str]:
    allowed = set(sys.stdlib_module_names) | ALLOWED_THIRD_PARTY | {"core"}
    return _imported_roots(ast.parse(source)) - allowed


def test_core_imports_only_stdlib_pydantic_and_core() -> None:
    files = sorted(CORE.rglob("*.py"))
    assert files
    violations = {
        str(path.relative_to(CORE.parent)): bad
        for path in files
        if (bad := disallowed_imports(path.read_text()))
    }
    assert violations == {}


def test_checker_flags_vendor_and_app_imports() -> None:
    source = "import os\nfrom pydantic import BaseModel\nimport anthropic\nfrom apps.x import y\n"
    assert disallowed_imports(source) == {"anthropic", "apps"}
