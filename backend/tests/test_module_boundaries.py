"""The abstraction rule, enforced structurally (SPEC.md §3.1).

    "Adding a new issuer = one new adapter file + registry entry. Zero changes to
     funding, ledger, webhook dispatch, or mobile code."

That promise is only worth something if it cannot quietly stop being true. These
tests read the import graph and fail if it does — which is the difference between a
design rule and a design aspiration. Phase 4 exists to demonstrate the rule with a
second real provider; this is what keeps it honest in between.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[1] / "app"
ISSUERS_ROOT = APP_ROOT / "issuers"

#: The entire issuer surface the rest of the service may import.
PUBLIC_ISSUER_MODULES = frozenset({"app.issuers", "app.issuers.base", "app.issuers.registry"})

#: Adapters translate; they must not know what happens to what they return.
FORBIDDEN_TO_ADAPTERS = ("app.funding", "app.ledger", "app.webhooks", "app.api")


def python_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py"))


def imported_modules(path: Path) -> set[str]:
    """Every module named by an import in one file, as a dotted string.

    `from x.y import z` contributes both `x.y` and `x.y.z`, since `z` may be a
    submodule — callers narrow that down against the modules that actually exist.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def module_name(path: Path) -> str:
    parts = path.relative_to(APP_ROOT.parent).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def issuer_modules() -> frozenset[str]:
    """Real module names under `app/issuers/`, so `base.Card` is not mistaken for one."""
    return frozenset(module_name(path) for path in python_files(ISSUERS_ROOT))


def outside_issuers() -> list[Path]:
    return [path for path in python_files(APP_ROOT) if ISSUERS_ROOT not in path.parents]


def test_there_is_something_to_check() -> None:
    # A path typo would make every test below vacuously pass.
    assert len(outside_issuers()) > 5
    assert PUBLIC_ISSUER_MODULES <= issuer_modules()
    assert "app.issuers.evm_deposit_mock" in issuer_modules()


@pytest.mark.parametrize("path", outside_issuers(), ids=lambda p: str(p.name))
def test_no_module_outside_issuers_imports_an_adapter(path: Path) -> None:
    leaked = (imported_modules(path) & issuer_modules()) - PUBLIC_ISSUER_MODULES
    assert not leaked, (
        f"{path.relative_to(APP_ROOT.parent)} imports {sorted(leaked)}. "
        f"Only {sorted(PUBLIC_ISSUER_MODULES)} may be imported from outside issuers/ — "
        f"if an adapter's specifics are needed, the abstraction is wrong, not the caller."
    )


@pytest.mark.parametrize("path", python_files(ISSUERS_ROOT), ids=lambda p: str(p.name))
def test_no_adapter_imports_the_pipeline(path: Path) -> None:
    # The rule in the other direction. An adapter reaching into `funding/` or
    # `ledger/` would mean the next adapter has to as well, and the one after that
    # would have to be written to match.
    leaked = {name for name in imported_modules(path) if name.startswith(FORBIDDEN_TO_ADAPTERS)}
    assert not leaked, (
        f"{path.relative_to(APP_ROOT.parent)} imports {sorted(leaked)}. "
        f"Adapters translate provider payloads and nothing else; what happens to a "
        f"CardEvent afterwards is not theirs to know."
    )


def adapter_packages() -> list[str]:
    """One entry per adapter: the package (or module) directly under `issuers/`."""
    reserved = {"app.issuers", "app.issuers.base", "app.issuers.registry"}
    return sorted(
        {
            ".".join(name.split(".")[:3])
            for name in issuer_modules()
            if name not in reserved and name.startswith("app.issuers.")
        }
    )


def test_there_are_at_least_two_adapters_to_compare() -> None:
    # From phase 3 on, the rule below has something to say. Before it, it passed
    # for want of a second adapter rather than for want of a bad import.
    assert len(adapter_packages()) >= 2, adapter_packages()


@pytest.mark.parametrize("path", python_files(ISSUERS_ROOT), ids=lambda p: str(p.name))
def test_no_adapter_imports_another_adapter(path: Path) -> None:
    # `issuers/__init__.py` is the registry entry and names every adapter; that is
    # its job. Every other file may not, because a helper shared between two
    # adapters is how "adding an issuer is one adapter file" turns into "one file,
    # plus edits to whatever they share" — and the third adapter inherits both.
    own = ".".join(module_name(path).split(".")[:3])
    if module_name(path) == "app.issuers":
        pytest.skip("the registry entry is allowed to know the adapters")
    leaked = {
        name
        for name in imported_modules(path) & issuer_modules()
        if name.startswith("app.issuers.") and not name.startswith(own)
    } - PUBLIC_ISSUER_MODULES
    assert not leaked, (
        f"{path.relative_to(APP_ROOT.parent)} imports {sorted(leaked)} from another "
        f"adapter. Adapters share `base.py` and `app/core/`, and nothing else."
    )


def test_the_funding_machine_depends_only_on_core_and_itself() -> None:
    # Phase 1's invariant, still true: the state machine has no idea providers
    # exist. Phase 5 will make it call adapters through the registry — and nothing
    # more than the registry.
    machine = imported_modules(APP_ROOT / "funding" / "machine.py")
    assert not {name for name in machine if name.startswith("app.issuers")}
    assert not {name for name in machine if name.startswith("app.webhooks")}


def test_the_ledger_knows_nothing_about_providers() -> None:
    # `ledger/` is written to by everything, so a dependency here would propagate
    # everywhere. Event types are strings for exactly this reason.
    forbidden = ("app.issuers", "app.webhooks")
    for path in python_files(APP_ROOT / "ledger"):
        leaked = {name for name in imported_modules(path) if name.startswith(forbidden)}
        assert not leaked, f"{path.name} imports {sorted(leaked)}"


def test_webhook_code_depends_on_the_interface_not_on_adapters() -> None:
    # The receiver resolves providers through the registry and receives normalized
    # events, which is what makes "a new issuer changes no webhook code" true.
    imports: set[str] = set()
    for path in python_files(APP_ROOT / "webhooks"):
        imports |= imported_modules(path)
    assert (imports & issuer_modules()) <= PUBLIC_ISSUER_MODULES
