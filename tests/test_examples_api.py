"""Offline API-drift smoke test for every script under ``examples/``.

The examples drive live Virtuoso/Spectre hosts, so they are never imported or
executed here.  Each file is parsed with :mod:`ast` and checked against the
real, imported ``virtuoso_bridge`` package:

* every ``virtuoso_bridge`` import must resolve to an existing module/name;
* calls to those imported functions/classes (and their class attributes such
  as ``VirtuosoClient.from_env``) must bind to ``inspect.signature``;
* attribute and method use on values whose type is statically known must name
  attributes that exist and, for methods, bind to their signatures.  Known
  values are instances built from an imported bridge class (including
  ``VirtuosoClient.from_env(...)``-style factories), parameters annotated with
  one, the client's sub-APIs (``client.schematic``, ``client.maestro`` ...)
  and the SSH runner behind ``<expr>.ssh_runner``.

The analysis is deliberately conservative: anything it cannot type precisely
(re-bound names, ``*args``/``**kwargs`` calls, return values, dynamic
``__getattr__`` classes) is skipped instead of guessed.
"""

from __future__ import annotations

import ast
import dataclasses
import functools
import importlib
import inspect
import textwrap
import types
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pytest

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.transport.ssh import SSHRunner
from virtuoso_bridge.transport.tunnel import SSHClient

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = REPO_ROOT / "examples"
EXAMPLE_FILES = sorted(EXAMPLES_DIR.rglob("*.py"))

# ``VirtuosoClient.ssh_runner`` returns ``SSHClient._ssh_runner``, an
# ``SSHRunner``; the tunnel's other role runners are the same class.
RUNNER_ATTRS = frozenset(
    {"ssh_runner", "_ssh_runner", "gui_runner", "docs_runner", "deployment_runner"}
)

_MISSING = object()
_PLACEHOLDER = object()


# ---------------------------------------------------------------------------
# Introspection of the real package
# ---------------------------------------------------------------------------


def _discover_client_subapis() -> Dict[str, type]:
    """Map ``client.<attr>`` to its class from ``self.<attr> = Cls(self)``."""
    init = VirtuosoClient.__init__
    module = importlib.import_module(VirtuosoClient.__module__)
    tree = ast.parse(textwrap.dedent(inspect.getsource(init)))
    local_imports: Dict[str, Tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                local_imports[alias.asname or alias.name] = (node.module, alias.name)
    found: Dict[str, type] = {}
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
        ):
            continue
        name = node.value.func.id
        if name in local_imports:
            mod_name, attr = local_imports[name]
            cls = getattr(importlib.import_module(mod_name), attr, None)
        else:
            cls = getattr(module, name, None)
        if not inspect.isclass(cls):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                found[target.attr] = cls
    return found


CLIENT_SUBAPIS = _discover_client_subapis()


def _is_bridge_module_name(name: str) -> bool:
    return name == "virtuoso_bridge" or name.startswith("virtuoso_bridge.")


def _is_bridge_class(obj: Any) -> bool:
    return inspect.isclass(obj) and _is_bridge_module_name(
        getattr(obj, "__module__", "") or ""
    )


def _qualname(obj: Any) -> str:
    module = getattr(obj, "__module__", None)
    name = getattr(obj, "__qualname__", None) or getattr(obj, "__name__", None)
    if module and name:
        return f"{module}.{name}"
    return repr(obj)


def _module_member(module: types.ModuleType, name: str) -> Any:
    """``module.name``, importing it as a submodule when necessary."""
    if hasattr(module, name):
        return getattr(module, name)
    full = f"{module.__name__}.{name}"
    try:
        return importlib.import_module(full)
    except ModuleNotFoundError as exc:
        if exc.name == full:
            return _MISSING
        raise


def _lookup_static(cls: type, name: str) -> Any:
    try:
        return inspect.getattr_static(cls, name)
    except AttributeError:
        return _MISSING


def _signature(obj: Any) -> Optional[inspect.Signature]:
    try:
        return inspect.signature(obj)
    except (TypeError, ValueError):
        return None
    except Exception:  # noqa: BLE001 - lazily evaluated annotations (3.14+)
        try:
            import annotationlib  # type: ignore[import-not-found]

            return inspect.signature(
                obj, annotation_format=annotationlib.Format.FORWARDREF
            )
        except Exception:  # noqa: BLE001
            return None


@functools.lru_cache(maxsize=None)
def _instance_attrs(cls: type) -> frozenset:
    """Instance attribute names: ``self.X = ...`` in class sources + fields."""
    names: Set[str] = set()
    for klass in cls.__mro__:
        if klass is object:
            continue
        if dataclasses.is_dataclass(klass):
            names.update(field.name for field in dataclasses.fields(klass))
        for fields_attr in ("model_fields", "__fields__"):
            fields = klass.__dict__.get(fields_attr)
            if isinstance(fields, dict):
                names.update(fields)
        try:
            source = textwrap.dedent(inspect.getsource(klass))
        except (OSError, TypeError):
            continue
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                targets = [node.target]
            else:
                continue
            while targets:
                target = targets.pop()
                if isinstance(target, (ast.Tuple, ast.List)):
                    targets.extend(target.elts)
                elif (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    names.add(target.attr)
    return frozenset(names)


def _has_dynamic_getattr(cls: type) -> bool:
    return any(
        "__getattr__" in vars(klass) for klass in cls.__mro__ if klass is not object
    )


def _instance_has_attr(cls: type, name: str) -> bool:
    return (
        _lookup_static(cls, name) is not _MISSING
        or hasattr(cls, name)
        or name in _instance_attrs(cls)
        or _has_dynamic_getattr(cls)
    )


def _method_signature(cls: type, name: str) -> Optional[inspect.Signature]:
    """Signature of ``instance.name(...)`` for plain/static/class methods."""
    raw = _lookup_static(cls, name)
    if isinstance(raw, staticmethod):
        return _signature(raw.__func__)
    if isinstance(raw, classmethod):
        return _signature(getattr(cls, name))
    if inspect.isfunction(raw):
        sig = _signature(raw)
        if sig is None:
            return None
        params = list(sig.parameters.values())
        positional = (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
        if not params or params[0].kind not in positional:
            return None
        return sig.replace(parameters=params[1:])
    return None


def _factory_returns_instance(cls: type, name: str) -> bool:
    """True for ``@classmethod`` factories annotated ``-> <cls>``."""
    raw = _lookup_static(cls, name)
    if not isinstance(raw, classmethod):
        return False
    try:
        annotation = raw.__func__.__annotations__.get("return", None)
    except Exception:  # noqa: BLE001 - lazily evaluated annotations (3.14+)
        return False
    if annotation is cls:
        return True
    return isinstance(annotation, str) and annotation.strip("'\" ") == cls.__name__


def _unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001
        return type(node).__name__


# ---------------------------------------------------------------------------
# Example analysis
# ---------------------------------------------------------------------------


class _Scope:
    def __init__(self, parent: Optional["_Scope"], is_class: bool = False) -> None:
        self.parent = parent
        self.is_class = is_class
        # name -> [(kind, payload)], kind in {"expr", "type", "with", "none", "unknown"}
        self.bindings: Dict[str, List[Tuple[str, Any]]] = {}
        self.declared_outer: Set[str] = set()

    def bind(self, name: str, kind: str, payload: Any = None) -> None:
        self.bindings.setdefault(name, []).append((kind, payload))


def _target_names(target: ast.AST) -> List[str]:
    return [
        node.id
        for node in ast.walk(target)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    ]


class ExampleChecker:
    """Collects API-drift problems for one example's source text."""

    def __init__(self, source: str, label: str) -> None:
        self.label = label
        self.tree = ast.parse(source, filename=label)
        self.problems: List[str] = []
        self.imported: Dict[str, Any] = {}
        self.attr_uses: List[Tuple[ast.Attribute, _Scope]] = []
        self.calls: List[Tuple[ast.Call, _Scope]] = []

    # -- reporting ---------------------------------------------------------

    def _report(self, node: ast.AST, message: str) -> None:
        self.problems.append(f"{self.label}:{getattr(node, 'lineno', '?')}: {message}")

    # -- driver ------------------------------------------------------------

    def run(self) -> List[str]:
        self._collect_imports()
        self._visit(self.tree, _Scope(None))
        self._check_attributes()
        self._check_calls()
        return self.problems

    # -- imports -----------------------------------------------------------

    def _collect_imports(self) -> None:
        bound: Dict[str, List[Any]] = {}
        other_bindings: Set[str] = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[0]
                    if not _is_bridge_module_name(alias.name):
                        other_bindings.add(local)
                        continue
                    try:
                        module = importlib.import_module(alias.name)
                    except Exception as exc:  # noqa: BLE001
                        self._report(node, f"`import {alias.name}` failed: {exc!r}")
                        continue
                    if alias.asname is None:
                        module = importlib.import_module(local)
                    bound.setdefault(local, []).append(module)
            elif isinstance(node, ast.ImportFrom):
                is_bridge = (
                    not node.level
                    and node.module is not None
                    and _is_bridge_module_name(node.module)
                )
                if not is_bridge:
                    other_bindings.update(
                        alias.asname or alias.name
                        for alias in node.names
                        if alias.name != "*"
                    )
                    continue
                try:
                    module = importlib.import_module(node.module)
                except Exception as exc:  # noqa: BLE001
                    self._report(node, f"`from {node.module} import ...` failed: {exc!r}")
                    continue
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    try:
                        obj = _module_member(module, alias.name)
                    except Exception as exc:  # noqa: BLE001
                        self._report(
                            node,
                            f"`from {node.module} import {alias.name}` failed: {exc!r}",
                        )
                        continue
                    if obj is _MISSING:
                        self._report(
                            node,
                            f"`from {node.module} import {alias.name}`: "
                            f"{node.module} has no attribute {alias.name!r}",
                        )
                        continue
                    bound.setdefault(alias.asname or alias.name, []).append(obj)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                other_bindings.add(node.name)
            elif isinstance(node, ast.arg):
                other_bindings.add(node.arg)
            elif isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
                other_bindings.add(node.id)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                other_bindings.add(node.name)
        # Only names bound to one bridge object and never re-bound otherwise
        # are resolved through; the rest is skipped rather than guessed.
        for name, objs in bound.items():
            if name in other_bindings or any(obj is not objs[0] for obj in objs):
                continue
            self.imported[name] = objs[0]

    def _resolve(self, expr: ast.AST) -> Any:
        """Imported bridge object named by a Name/Attribute chain, or None."""
        if isinstance(expr, ast.Name):
            return self.imported.get(expr.id)
        if isinstance(expr, ast.Attribute):
            base = self._resolve(expr.value)
            if isinstance(base, types.ModuleType):
                try:
                    member = _module_member(base, expr.attr)
                except Exception:  # noqa: BLE001
                    return None
                return None if member is _MISSING else member
            if inspect.isclass(base):
                return getattr(base, expr.attr, None)
        return None

    # -- scope / binding collection ------------------------------------------

    def _annotation_type(self, ann: Optional[ast.AST]) -> Optional[type]:
        if ann is None:
            return None
        if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
            try:
                ann = ast.parse(ann.value, mode="eval").body
            except SyntaxError:
                return None
        if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
            parts = [ann.left, ann.right]
        elif isinstance(ann, ast.Subscript) and _unparse(ann.value).split(".")[-1] in (
            "Optional",
            "Union",
        ):
            inner = ann.slice
            parts = list(inner.elts) if isinstance(inner, ast.Tuple) else [inner]
        else:
            obj = self._resolve(ann)
            return obj if _is_bridge_class(obj) else None
        non_none = [
            part
            for part in parts
            if not (isinstance(part, ast.Constant) and part.value is None)
        ]
        if len(non_none) != 1:
            return None
        return self._annotation_type(non_none[0])

    def _bind_arguments(self, args: ast.arguments, scope: _Scope) -> None:
        for arg in list(getattr(args, "posonlyargs", [])) + args.args + args.kwonlyargs:
            arg_type = self._annotation_type(arg.annotation)
            if arg_type is None:
                scope.bind(arg.arg, "unknown")
            else:
                scope.bind(arg.arg, "type", arg_type)
        for arg in (args.vararg, args.kwarg):
            if arg is not None:
                scope.bind(arg.arg, "unknown")

    def _visit_all(self, nodes: List[Optional[ast.AST]], scope: _Scope) -> None:
        for node in nodes:
            if node is not None:
                self._visit(node, scope)

    def _visit(self, node: ast.AST, scope: _Scope) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            args = node.args
            outer: List[Optional[ast.AST]] = list(args.defaults) + list(args.kw_defaults)
            if not isinstance(node, ast.Lambda):
                outer += list(node.decorator_list) + [node.returns]
                all_args = list(getattr(args, "posonlyargs", [])) + args.args + args.kwonlyargs
                all_args += [a for a in (args.vararg, args.kwarg) if a is not None]
                outer += [a.annotation for a in all_args]
                scope.bind(node.name, "unknown")
            self._visit_all(outer, scope)
            child = _Scope(scope)
            self._bind_arguments(args, child)
            body = [node.body] if isinstance(node, ast.Lambda) else node.body
            self._visit_all(body, child)
            return
        if isinstance(node, ast.ClassDef):
            self._visit_all(
                list(node.decorator_list) + list(node.bases) + [k.value for k in node.keywords],
                scope,
            )
            scope.bind(node.name, "unknown")
            child = _Scope(scope, is_class=True)
            self._visit_all(node.body, child)
            return
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            scope.declared_outer.update(node.names)
            return

        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    if isinstance(node.value, ast.Constant) and node.value.value is None:
                        scope.bind(target.id, "none")
                    else:
                        scope.bind(target.id, "expr", node.value)
                else:
                    for name in _target_names(target):
                        scope.bind(name, "unknown")
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            ann_type = self._annotation_type(node.annotation)
            if ann_type is not None:
                scope.bind(node.target.id, "type", ann_type)
            elif node.value is not None:
                scope.bind(node.target.id, "expr", node.value)
        elif isinstance(node, ast.NamedExpr):
            scope.bind(node.target.id, "expr", node.value)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if isinstance(item.optional_vars, ast.Name):
                    scope.bind(item.optional_vars.id, "with", item.context_expr)
                elif item.optional_vars is not None:
                    for name in _target_names(item.optional_vars):
                        scope.bind(name, "unknown")
        elif isinstance(node, (ast.AugAssign, ast.For, ast.AsyncFor, ast.comprehension)):
            for name in _target_names(node.target):
                scope.bind(name, "unknown")
        elif isinstance(node, ast.ExceptHandler) and node.name:
            scope.bind(node.name, "unknown")
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name != "*":
                    scope.bind(alias.asname or alias.name.split(".")[0], "unknown")
        elif type(node).__name__ in ("MatchAs", "MatchStar", "MatchMapping"):
            for name in (getattr(node, "name", None), getattr(node, "rest", None)):
                if name:
                    scope.bind(name, "unknown")

        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            self.attr_uses.append((node, scope))
        if isinstance(node, ast.Call):
            self.calls.append((node, scope))
        for child_node in ast.iter_child_nodes(node):
            self._visit(child_node, scope)

    # -- type inference ----------------------------------------------------

    def _name_type(self, name: str, scope: _Scope, seen: frozenset) -> Optional[type]:
        current: Optional[_Scope] = scope
        while current is not None:
            visible = current is scope or not current.is_class
            if visible and name in current.bindings and name not in current.declared_outer:
                break
            current = current.parent
        if current is None:
            return None
        key = (id(current), name)
        if key in seen:
            return None
        seen = seen | {key}
        result: Optional[type] = None
        for kind, payload in current.bindings[name]:
            if kind == "none":
                continue
            if kind == "type":
                found = payload
            elif kind == "expr":
                found = self._infer(payload, current, seen)
            elif kind == "with":
                # Only the client's ``__enter__`` is known to return itself.
                found = self._infer(payload, current, seen)
                if found is not VirtuosoClient:
                    found = None
            else:
                found = None
            if found is None or (result is not None and found is not result):
                return None
            result = found
        return result

    def _infer(self, expr: ast.AST, scope: _Scope, seen: frozenset = frozenset()) -> Optional[type]:
        """Statically known class of the value of *expr*, or None."""
        if isinstance(expr, ast.Name):
            if expr.id in self.imported:
                return None
            return self._name_type(expr.id, scope, seen)
        if isinstance(expr, ast.Call):
            func = expr.func
            target = self._resolve(func)
            if _is_bridge_class(target):
                return target
            if isinstance(func, ast.Attribute):
                owner = self._resolve(func.value)
                if _is_bridge_class(owner) and _factory_returns_instance(owner, func.attr):
                    return owner
            return None
        if isinstance(expr, ast.Attribute):
            if self._resolve(expr) is not None or self._resolve(expr.value) is not None:
                return None
            if expr.attr in RUNNER_ATTRS:
                return SSHRunner
            if self._infer(expr.value, scope, seen) is VirtuosoClient:
                return CLIENT_SUBAPIS.get(expr.attr)
        return None

    # -- checks ------------------------------------------------------------

    def _check_attributes(self) -> None:
        for node, scope in self.attr_uses:
            base = self._resolve(node.value)
            if isinstance(base, types.ModuleType):
                try:
                    member = _module_member(base, node.attr)
                except Exception as exc:  # noqa: BLE001
                    self._report(node, f"`{_unparse(node)}` failed to import: {exc!r}")
                    continue
                if member is _MISSING:
                    self._report(
                        node,
                        f"`{_unparse(node)}`: module {base.__name__} has no "
                        f"attribute {node.attr!r}",
                    )
                continue
            if inspect.isclass(base):
                if _lookup_static(base, node.attr) is _MISSING and not hasattr(base, node.attr):
                    self._report(
                        node,
                        f"`{_unparse(node)}`: {_qualname(base)} has no attribute {node.attr!r}",
                    )
                continue
            owner = self._infer(node.value, scope)
            if owner is not None and not _instance_has_attr(owner, node.attr):
                self._report(
                    node,
                    f"`{_unparse(node)}`: {_qualname(owner)} has no attribute {node.attr!r}",
                )

    def _check_calls(self) -> None:
        for call, scope in self.calls:
            if any(isinstance(arg, ast.Starred) for arg in call.args):
                continue
            if any(keyword.arg is None for keyword in call.keywords):
                continue
            func = call.func
            sig: Optional[inspect.Signature] = None
            described = ""
            target = self._resolve(func)
            if target is not None:
                if callable(target) and not isinstance(target, types.ModuleType):
                    sig = _signature(target)
                    described = _qualname(target)
            elif isinstance(func, ast.Attribute):
                owner = self._infer(func.value, scope)
                if owner is not None:
                    sig = _method_signature(owner, func.attr)
                    described = f"{_qualname(owner)}.{func.attr}"
            if sig is None:
                continue
            try:
                sig.bind(
                    *[_PLACEHOLDER] * len(call.args),
                    **{keyword.arg: _PLACEHOLDER for keyword in call.keywords},
                )
            except TypeError as exc:
                self._report(
                    call,
                    f"`{_unparse(func)}(...)` does not match {described}{sig}: {exc}",
                )


def check_source(source: str, label: str) -> List[str]:
    return ExampleChecker(source, label).run()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_examples_are_discovered() -> None:
    assert len(EXAMPLE_FILES) > 10, EXAMPLES_DIR


def test_client_subapi_discovery_covers_well_known_attributes() -> None:
    expected = {"schematic", "symbol", "layout", "library", "maestro", "dialogs", "requests"}
    assert expected <= set(CLIENT_SUBAPIS), sorted(CLIENT_SUBAPIS)
    for name in expected:
        assert _is_bridge_class(CLIENT_SUBAPIS[name]), (name, CLIENT_SUBAPIS[name])


def test_runner_class_is_what_the_client_exposes() -> None:
    getter_source = inspect.getsource(VirtuosoClient.ssh_runner.fget)
    assert "_ssh_runner" in getter_source
    annotation = SSHClient.ssh_runner.fget.__annotations__.get("return")
    assert "SSHRunner" in str(annotation)
    assert hasattr(SSHRunner, "run_command")


def test_checker_flags_known_drift_patterns() -> None:
    drifted = textwrap.dedent(
        '''
        from virtuoso_bridge import VirtuosoClient
        from virtuoso_bridge.virtuoso.schematic.ops import schematic_create_pin
        from virtuoso_bridge.virtuoso.schematic.ops import no_such_helper

        def build(client: VirtuosoClient) -> None:
            with client.schematic.create("L", "C") as sch:
                sch.add(schematic_create_pin("A", 0.0, 0.0, direction="input"))
            client.schematic.no_such_method()

        def main() -> None:
            client = VirtuosoClient.from_env(no_such_kwarg=1)
            client.ssh_runner.run("ls")
            client.execute_skill()
            runner = client.ssh_runner
            runner.run_command("ls", 1, 2)
        '''
    )
    problems = check_source(drifted, "drifted.py")
    expected_fragments = [
        "no attribute 'no_such_helper'",
        "`schematic_create_pin(...)` does not match",
        "no attribute 'no_such_method'",
        "`VirtuosoClient.from_env(...)` does not match",
        "`client.ssh_runner.run`",
        "`client.execute_skill(...)` does not match",
        "`runner.run_command(...)` does not match",
    ]
    for fragment in expected_fragments:
        assert any(fragment in problem for problem in problems), (fragment, problems)
    assert len(problems) == len(expected_fragments), problems

    clean = textwrap.dedent(
        '''
        from virtuoso_bridge import VirtuosoClient
        from virtuoso_bridge.virtuoso.schematic.ops import schematic_create_pin

        def main(*args, **kwargs) -> None:
            client = VirtuosoClient.from_env()
            with client.schematic.create("L", "C") as sch:
                sch.add(schematic_create_pin("A", 0.0, 0.0, "R0", direction="input"))
            client.ssh_runner.run_command("ls")
            schematic_create_pin(*args, **kwargs)
            client = object()
            client.anything_goes()
        '''
    )
    assert check_source(clean, "clean.py") == []


@pytest.mark.parametrize(
    "path",
    EXAMPLE_FILES,
    ids=[path.relative_to(EXAMPLES_DIR).as_posix() for path in EXAMPLE_FILES],
)
def test_example_matches_current_api(path: Path) -> None:
    label = path.relative_to(REPO_ROOT).as_posix()
    source = path.read_text(encoding="utf-8")
    problems = check_source(source, label)
    assert not problems, "API drift in example:\n" + "\n".join(problems)
