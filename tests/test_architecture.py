"""Architectural boundary tests enforcing modular monolith isolation rules via AST analysis."""

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = PROJECT_ROOT / "src"
TESTS_ROOT = PROJECT_ROOT / "tests"

FORBIDDEN_CORE_PREFIXES: tuple[str, ...] = (
    "aiogram",
    "src.bot",
    "bot",
    "src.db",
    "db",
    "src.config",
    "config",
)

FORBIDDEN_DB_PREFIXES: tuple[str, ...] = (
    "aiogram",
    "src.bot",
    "bot",
)


def _extract_imported_modules(py_file: Path) -> list[str]:
    """Return all module names referenced in `import` or `from ... import` statements."""
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.append(node.module)
    return modules


def test_core_package_has_zero_coupling_to_bot_db_or_config() -> None:
    """Verify `src/core/` never imports `aiogram`, `src.bot`, `src.db`, or `src.config`."""
    core_dir = SRC_ROOT / "core"
    py_files = list(core_dir.rglob("*.py"))
    assert py_files, "Expected Python modules inside src/core/"

    violations: list[str] = []
    for py_file in py_files:
        for mod in _extract_imported_modules(py_file):
            if (
                mod == "aiogram"
                or mod.startswith(tuple(f"{prefix}." for prefix in FORBIDDEN_CORE_PREFIXES))
                or mod in FORBIDDEN_CORE_PREFIXES
            ):
                rel = py_file.relative_to(PROJECT_ROOT)
                violations.append(f"{rel} imports forbidden module '{mod}'")

    assert not violations, "Architectural boundary violations in src/core:\n" + "\n".join(violations)


def test_db_package_never_imports_telegram_bot_layer() -> None:
    """Verify `src/db/` remains independent of `aiogram` and `src/bot/`."""
    db_dir = SRC_ROOT / "db"
    py_files = list(db_dir.rglob("*.py"))
    assert py_files, "Expected Python modules inside src/db/"

    violations: list[str] = []
    for py_file in py_files:
        for mod in _extract_imported_modules(py_file):
            if mod in FORBIDDEN_DB_PREFIXES or mod.startswith(
                tuple(f"{prefix}." for prefix in FORBIDDEN_DB_PREFIXES)
            ):
                rel = py_file.relative_to(PROJECT_ROOT)
                violations.append(f"{rel} imports forbidden module '{mod}'")

    assert not violations, "Architectural boundary violations in src/db:\n" + "\n".join(violations)


def test_no_wildcard_or_nested_imports_in_codebase() -> None:
    """Verify that all imports across `src/` and `tests/` are explicit top-level imports."""
    all_files = [*SRC_ROOT.rglob("*.py"), *TESTS_ROOT.rglob("*.py"), PROJECT_ROOT / "main.py"]
    violations: list[str] = []

    for py_file in all_files:
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        rel = py_file.relative_to(PROJECT_ROOT)

        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "*":
                        violations.append(f"{rel}:{node.lineno} uses wildcard import '*'")

        for child in ast.walk(tree):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for subnode in ast.walk(child):
                    if isinstance(subnode, (ast.Import, ast.ImportFrom)):
                        violations.append(
                            f"{rel}:{subnode.lineno} contains nested/inline import inside {child.name}"
                        )

    assert not violations, "Import rule violations:\n" + "\n".join(violations)
