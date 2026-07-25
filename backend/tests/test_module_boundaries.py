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


# ------------------------------------------------- the coupling that hid ----
# The tests above read the import graph, and for a year that looked like enough.
# It was not: adapter configuration lived in `app/core/config.py` as fields named
# after each provider (`lithic_api_key`, `evm_deposit_mock_webhook_secret`), and
# the coupling ran adapter -> `get_settings()` -> a field named after the adapter.
# No import of an adapter anywhere, so nothing here fired — while "adding an
# issuer is one adapter file plus one registry entry" was quietly false, and a
# dead field for a deleted adapter sat in `core/` read by nobody. These two tests
# close it from both ends.

CORE_CONFIG = APP_ROOT / "core" / "config.py"

#: Tokens from adapter package names that are too generic to accuse a core field
#: over. `mock` says nothing about *which* provider; `gnosis`, `pay`, `lithic`,
#: `stripe`, `issuing` all do.
GENERIC_NAME_TOKENS = frozenset({"mock"})


def settings_field_names(path: Path) -> set[str]:
    """Annotated attribute names of every class in one module."""
    tree = ast.parse(path.read_text(), filename=str(path))
    return {
        node.target.id
        for cls in tree.body
        if isinstance(cls, ast.ClassDef)
        for node in cls.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }


def adapter_name_tokens() -> set[str]:
    """`app.issuers.gnosis_pay_mock` -> {"gnosis", "pay"}; the generic bits dropped."""
    tokens: set[str] = set()
    for package in adapter_packages():
        tokens |= set(package.split(".")[-1].split("_")) - GENERIC_NAME_TOKENS
    return tokens


def test_the_config_guard_has_something_to_read() -> None:
    # Both helpers fail open — an empty field set or an empty token set would make
    # the test below pass without checking anything.
    assert len(settings_field_names(CORE_CONFIG)) > 3
    assert {"lithic", "gnosis"} <= adapter_name_tokens()


def test_core_config_declares_no_adapter_specific_fields() -> None:
    named = {
        field: token
        for field in settings_field_names(CORE_CONFIG)
        for token in adapter_name_tokens()
        if token in field.split("_")
    }
    assert not named, (
        f"app/core/config.py declares {sorted(named)}, named after "
        f"{sorted(set(named.values()))}. A provider's configuration belongs to that "
        f"provider's package (app/issuers/<name>/config.py, with its own env prefix): "
        f"a field here is a change to core/ for every new issuer, and it outlives the "
        f"adapter it was named for."
    )


@pytest.mark.parametrize("path", python_files(ISSUERS_ROOT), ids=lambda p: str(p.name))
def test_no_adapter_reads_core_settings(path: Path) -> None:
    # The structural half: an adapter that cannot see `app.core.config` cannot add
    # a field to it. `app.core.money` and the rest of `core/` stay available —
    # `Money` is shared vocabulary, whereas settings are ownership.
    leaked = {name for name in imported_modules(path) if name.startswith("app.core.config")}
    assert not leaked, (
        f"{path.relative_to(APP_ROOT.parent)} imports {sorted(leaked)}. Adapters own "
        f"their own settings; reading the app's is how a provider-specific field ends "
        f"up in core/."
    )


def test_each_adapter_that_needs_configuration_declares_its_own() -> None:
    # Not every adapter needs settings, but one that does must declare them itself.
    for package in adapter_packages():
        config = APP_ROOT.parent / Path(package.replace(".", "/")) / "config.py"
        if not config.exists():
            continue
        prefixes = [
            node.value.value
            for node in ast.walk(ast.parse(config.read_text()))
            if isinstance(node, ast.keyword)
            and node.arg == "env_prefix"
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ]
        assert prefixes, f"{config} declares settings without an env_prefix"
        expected = f"{package.split('.')[-1].upper()}_"
        assert prefixes == [expected], (
            f"{config} uses env prefix {prefixes} but the package is named "
            f"{package.split('.')[-1]}; two adapters sharing a prefix would read each "
            f"other's variables."
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
