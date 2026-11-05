"""
Static integrity checks over bot.py.

A 12k-line module can be imported successfully and still blow up at runtime the
moment a rarely-used code path runs, because Python only resolves global names
when the line executes.  During the Docker rewrite several helpers were deleted
(`webssh_tokens`, `webssh_sessions`, `_save_webssh_token`) while call sites
survived, and one new command referenced a helper (`_exc_text`) that never
existed.  `import bot` caught none of it.

These tests make that class of bug a test failure.
"""

from __future__ import annotations

import ast
import builtins
import keyword
from pathlib import Path

import pytest

BOT_PY = Path(__file__).resolve().parent.parent / "bot.py"


@pytest.fixture(scope="module")
def bot_source() -> str:
    return BOT_PY.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def bot_tree(bot_source) -> ast.Module:
    return ast.parse(bot_source, filename=str(BOT_PY))


def test_bot_py_parses(bot_tree):
    """bot.py must be syntactically valid."""
    assert isinstance(bot_tree, ast.Module)


def test_no_undefined_global_names(bot_source):
    """
    Every global name bot.py loads must be defined, imported or a builtin.

    Uses pyflakes when available (it understands scopes properly) and falls back
    to an AST walk so the check still runs in a minimal environment.
    """
    try:
        from pyflakes import api, reporter
    except ImportError:
        pytest.skip("pyflakes is not installed")

    class Collector(reporter.Reporter):
        def __init__(self) -> None:
            super().__init__(None, None)
            self.messages = []

        def unexpectedError(self, filename, msg):  # noqa: N802
            self.messages.append(f"{filename}: unexpected error: {msg}")

        def syntaxError(self, filename, msg, lineno, offset, text):  # noqa: N802
            self.messages.append(f"{filename}:{lineno}: syntax error: {msg}")

        def flake(self, message):
            self.messages.append(str(message))

    collector = Collector()
    api.check(bot_source, str(BOT_PY), collector)

    undefined = [m for m in collector.messages if "undefined name" in m]
    assert not undefined, "bot.py references names that are never defined:\n" + "\n".join(undefined)


def _enclosing_function(tree: ast.Module, lineno: int) -> str:
    """Name of the innermost function containing `lineno`, or '' at module scope."""
    best = ""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.lineno <= lineno <= (node.end_lineno or node.lineno):
                best = node.name
    return best


#: Module-level names Python provides implicitly.
IMPLICIT_MODULE_GLOBALS = {"__file__", "__name__", "__doc__", "__spec__",
                           "__package__", "__loader__", "__builtins__", "__debug__"}


def _module_level_names(tree: ast.Module) -> set:
    """Names bound at module scope: imports, assignments, defs, classes, globals."""
    names = set(dir(builtins)) | IMPLICIT_MODULE_GLOBALS
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.comprehension,)) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Global):
            names.update(node.names)
        elif isinstance(node, ast.withitem) and isinstance(node.optional_vars, ast.Name):
            names.add(node.optional_vars.id)
    return names


def test_deleted_helpers_are_gone(bot_source):
    """
    The LXC/LXD backend and the in-bot web SSH server were removed.  If a helper
    from either comes back, a call site somewhere will still assume the old one.
    """
    removed = [
        "run_lxc_command", "run_lxc_argv", "execute_lxc", "apply_lxc_config",
        "get_lxc_containers_on_node", "_local_lxc_binary",
        "WebSSHSession", "WebSSHToken", "_render_terminal", "_render_pin_page",
        "_webssh_cleanup_loop", "_ip_rate_ok", "_sessions_for_ip",
        "_cleanup_webssh", "_token_row", "_save_webssh_token", "_load_webssh_html",
        "LXC_BRIDGE", "LXC_TIMEOUT",
    ]
    revived = [name for name in removed if f"def {name}" in bot_source
               or f"class {name}" in bot_source or f"{name} =" in bot_source]
    assert not revived, f"deleted helpers have been redefined: {revived}"


def test_no_privileged_containers(bot_source):
    """Customer containers must never be created privileged."""
    assert "privileged=True" not in bot_source
    assert "privileged = True" not in bot_source


def test_docker_socket_is_never_mounted_into_a_customer_container(bot_source):
    assert "/var/run/docker.sock" not in bot_source.replace(
        'unix:///var/run/docker.sock', "")


def test_infra_is_the_only_docker_entry_point(bot_source, bot_tree):
    """
    bot.py must not talk to the Docker SDK directly — that is what the
    `unixnodes` package exists for.

    The one exception is the startup probe in `startup_checks()`, which pings the
    engine to warn about a missing daemon.  Anything outside that function is a
    leak of infrastructure back into the UI layer.
    """
    probe_ok = {"startup_checks"}
    offenders = []
    for node in ast.walk(bot_tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = node.module if isinstance(node, ast.ImportFrom) else ""
            targets = [a.name for a in node.names]
            if any((module or "").split(".")[0] == "docker"
                   or t.split(".")[0] == "docker" for t in targets):
                offenders.append((node.lineno, module or ",".join(targets)))
    for lineno, what in offenders:
        enclosing = _enclosing_function(bot_tree, lineno)
        assert enclosing in probe_ok, (
            f"bot.py:{lineno} imports `{what}` inside {enclosing or '<module scope>'}; "
            "Docker belongs in the unixnodes package")


def test_the_credential_vault_is_installed(bot_source):
    """
    Without `INFRA.set_vault(...)` every root password the services generate is
    discarded: `!vps password` and the web terminal would both stop working.
    """
    assert "INFRA.set_vault(encrypt_secret)" in bot_source


def test_init_db_runs_the_migration_engine(bot_source):
    """The Docker tables only exist if init_db() delegates to the migrator."""
    assert "unix_migrations.migrate(" in bot_source


def test_every_progress_callback_is_thread_safe(bot_source):
    """
    The unixnodes services invoke `progress(...)` from a worker thread.  Passing
    a coroutine there is broken, so every call must use a PanelPump.
    """
    tree = ast.parse(bot_source)
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "progress":
            value = node.value
            if isinstance(value, ast.Attribute) and value.attr in ("emit", "emit_line"):
                continue
            if isinstance(value, ast.Constant) and value.value is None:
                continue
            offenders.append(ast.dump(value))
    assert not offenders, f"progress= must be PanelPump.emit/emit_line: {offenders}"


def test_bot_module_names_resolve(bot_tree, bot_source):
    """
    Cheap belt-and-braces check on top of pyflakes: no call site references a
    name that is neither bound somewhere in the file nor a builtin/keyword.
    """
    bound = _module_level_names(bot_tree)
    missing = set()
    for node in ast.walk(bot_tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in bound and not keyword.iskeyword(node.id):
                missing.add(node.id)
    # `self`/`cls` and comprehension targets are handled by _module_level_names;
    # anything still missing is a genuine typo.
    assert not missing, f"possibly undefined names: {sorted(missing)}"


def _unix_alias_map(tree: ast.Module) -> dict:
    """
    Map bot.py's ``unix_*`` import aliases to the real module objects.

    Only aliases that name a *module* are collected.  Aliases bound to a
    function (``run_in_thread as unix_run_in_thread``) are skipped — attribute
    access on those is not the failure mode this guard targets.
    """
    import importlib

    aliases = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if not module.startswith("unixnodes"):
            continue
        for alias in node.names:
            name = alias.asname or ""
            if not name.startswith("unix_"):
                continue
            try:
                aliases[name] = importlib.import_module(f"{module}.{alias.name}")
            except ImportError:
                pass  # a function/class, not a submodule — nothing to check
    return aliases


def test_every_unix_attribute_access_resolves(bot_tree):
    """
    Every `unix_<alias>.<ATTR>` in bot.py must exist on the real module.

    This is the bug pyflakes cannot see. `unix_networking.BRIDGE_NAME` was
    shipped to `main` and broke `!vps-network` at runtime: pyflakes reports
    **zero** undefined names for it, because `unix_networking` is a perfectly
    valid module — only the attribute is missing. Python resolves the attribute
    when the line executes, so `import bot` is clean too.

    Resolving the accesses against the imported modules turns that whole class
    of typo into a test failure.
    """
    aliases = _unix_alias_map(bot_tree)
    assert aliases, "no unixnodes aliases were found in bot.py — the check is broken"

    bad = []
    for node in ast.walk(bot_tree):
        if not isinstance(node, ast.Attribute):
            continue
        base = node.value
        if not isinstance(base, ast.Name) or base.id not in aliases:
            continue
        module = aliases[base.id]
        if not hasattr(module, node.attr):
            bad.append(f"{base.id}.{node.attr} (line {node.lineno})")

    assert not bad, (
        "bot.py reads attributes that do not exist on the unixnodes modules — "
        "these raise AttributeError at runtime, not at import:\n  "
        + "\n".join(sorted(set(bad))))
