"""Dependency contract: mcp (Model Context Protocol SDK), what no request can reach.

Open WebUI talks to external MCP tool servers through this SDK. Connecting, listing and calling
tools, the OAuth models of an MCP connection and the httpx client under the transport run end to
end in integration/deps/test_outbound_stack.py and integration/tools/test_mcp_oauth_*.py. Two
things stay here:

- Two sweeps over the backend with `ast`: every name it imports from the SDK still resolves, and
  every call into such a name still binds its arguments. They catch an import or keyword
  upstream adds the day it lands, which no request made today can. The session calls behind
  `list_resources()` and `read_resource()` run through this sweep: the calls themselves were
  never pinned on a retired high-level `Client`, only on what the backend's client.py actually
  imports and binds.
- The `resources` field the client reads stays on the result's model_dump.

Everything is offline: no server is spawned and no transport is opened. Uses the `depcheck`
fixture from unit/deps/conftest.py; skips when mcp is not importable.

Discriminates: in a backend copy importing a name the installed SDK lacks, the import inventory
fails; calling `streamablehttp_client` with a keyword it does not take fails the call check.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

import pytest

pytestmark = pytest.mark.depcheck

IMPORT_NAME = "mcp"

def _is_mcp(module: str | None) -> bool:
    return module == IMPORT_NAME or (module or "").startswith(f"{IMPORT_NAME}.")


def _mcp_imports(backend: Path) -> dict[Path, tuple[ast.Module, dict[str, tuple[str, str]]]]:
    """Per backend file that imports from mcp: its tree and each bound name's (module, name)."""
    found = {}
    for path in sorted((backend / "open_webui").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if IMPORT_NAME not in source:
            continue
        tree = ast.parse(source)
        bound = {
            alias.asname or alias.name: (node.module, alias.name)
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level == 0 and _is_mcp(node.module)
            for alias in node.names
        }
        if bound:
            found[path] = (tree, bound)
    assert found, f"no module under {backend} imports from mcp; retarget this contract"
    return found


def _resolve(module: str, name: str):
    return getattr(importlib.import_module(module), name)


def test_every_name_the_backend_imports_resolves(depcheck, open_webui_backend):
    depcheck.load(IMPORT_NAME)
    missing = [
        f"{path.relative_to(open_webui_backend)}: from {module} import {name}"
        for path, (_, bound) in _mcp_imports(open_webui_backend).items()
        for module, name in bound.values()
        if not depcheck.has(importlib.import_module(module), name)
    ]
    assert not missing, f"the installed mcp lacks names the backend imports: {missing}"


def _call_problem(target, call: ast.Call) -> str | None:
    """Why the call's arguments would not bind to `target`, or None when they do."""
    positional = [None for argument in call.args if not isinstance(argument, ast.Starred)]
    keywords = {keyword.arg: None for keyword in call.keywords if keyword.arg}
    try:
        inspect.signature(target).bind_partial(*positional, **keywords)
    except TypeError as error:
        return str(error)
    return None


def test_every_call_into_an_imported_name_binds(depcheck, open_webui_backend):
    """`streamablehttp_client(url, headers=..., httpx_client_factory=...)`, `ClientSession(read,
    write)`, `OAuthMetadata.model_validate(...)` and every other call the backend makes into a
    name it imported from mcp still accepts the arguments as passed."""
    depcheck.load(IMPORT_NAME)
    problems = []
    for path, (tree, bound) in _mcp_imports(open_webui_backend).items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if isinstance(function, ast.Name) and function.id in bound:
                target = _resolve(*bound[function.id])
            elif (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id in bound
            ):
                target = getattr(_resolve(*bound[function.value.id]), function.attr)
            else:
                continue
            problem = _call_problem(target, node)
            if problem:
                where = f"{path.relative_to(open_webui_backend)}:{node.lineno}"
                problems.append(f"{where} {ast.unparse(node.func)}(...): {problem}")
    assert not problems, f"backend calls the installed mcp no longer accepts: {problems}"


def test_list_resources_result_shape(depcheck):
    """list_resources() does result.model_dump() then `['resources']`."""
    mod = depcheck.load(IMPORT_NAME)
    types_mod = depcheck.resolve(mod, "types")
    assert types_mod.ListResourcesResult(resources=[]).model_dump().get("resources") == []
