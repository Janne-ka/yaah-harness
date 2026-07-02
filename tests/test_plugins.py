"""The plugins registration seam (yaah.plugins + root `plugins:`).

Proves the extensibility promise end-to-end: an extension module registers a new
`state` type via register_type; `plugins:` loads it; validate_root then ACCEPTS
the type it used to reject; and the factory map builds the registered instance.
Registrations are cleaned up after each scenario — the factory maps are process-
global, and leaking a test type would corrupt sibling tests.

Run: cd yaah && PYTHONPATH=src python3 tests/test_plugins.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import textwrap

from yaah import runtime_factories as rf
from yaah.plugins import load_plugins, register_type
from yaah.validate import validate_root


def _clean(kind_map: dict, name: str) -> None:
    kind_map.pop(name, None)


def test_register_and_validate_accepts_new_state_type() -> None:
    # before: unknown type is rejected
    root = {"state": {"type": "redis_t", "url": "redis://x"}, "pipeline": "p.json"}
    try:
        validate_root(root)
        raise AssertionError("unregistered type must be rejected")
    except ValueError as e:
        assert "redis_t" in str(e)
    built = []
    register_type("state", "redis_t",
                  lambda spec, base: built.append(spec.get("url")) or "REDIS",
                  spec_keys=["url"])
    try:
        validate_root(root)                               # now a known enum value
        # and the factory map builds it (the runtime path reads the same map)
        factory, keys = rf._STATE_TYPES["redis_t"]
        assert factory({"url": "redis://x"}, "/b") == "REDIS" and built == ["redis://x"]
        assert keys == frozenset({"url"})
        # spec-keys enforcement: an unknown key on the registered type errors
        bad = {"state": {"type": "redis_t", "ulr": "typo"}, "pipeline": "p.json"}
        try:
            validate_root(bad)
            raise AssertionError("unknown spec key on a registered type must be rejected")
        except ValueError as e:
            assert "ulr" in str(e)
    finally:
        _clean(rf._STATE_TYPES, "redis_t")


def test_register_rejects_collision_and_unknown_kind() -> None:
    try:
        register_type("state", "memory", lambda s, b: None)
        raise AssertionError("collision with a built-in must be rejected")
    except ValueError as e:
        assert "already registered" in str(e)
    try:
        register_type("stroe", "x", lambda s, b: None)
        raise AssertionError("unknown kind must be rejected")
    except ValueError as e:
        assert "valid kinds" in str(e)
    try:
        register_type("provider", "", lambda s, b: None)
        raise AssertionError("empty name must be rejected")
    except ValueError:
        pass
    assert "x" not in rf._PROVIDER_TYPES


def test_load_plugins_imports_module_and_surfaces_errors() -> None:
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "yaah_test_ext.py"), "w") as f:
            f.write(textwrap.dedent("""\
                from yaah.plugins import register_type
                register_type("data_sink", "null_t", lambda spec, base: "NULL")
            """))
        try:
            load_plugins(["yaah_test_ext"], d)
            assert "null_t" in rf._DATA_SINK_TYPES
            # re-load is idempotent (cached import; no duplicate-registration blowup)
            load_plugins(["yaah_test_ext"], d)
        finally:
            _clean(rf._DATA_SINK_TYPES, "null_t")
            sys.modules.pop("yaah_test_ext", None)
            if d in sys.path:
                sys.path.remove(d)
    # a listed-but-unloadable plugin is a loud error naming the module
    try:
        load_plugins(["no_such_module_xyz"], "/tmp")
        raise AssertionError("missing plugin module must be a loud error")
    except ValueError as e:
        assert "no_such_module_xyz" in str(e)
    # a non-list shape is rejected
    try:
        load_plugins("not_a_list", "/tmp")
        raise AssertionError("non-list plugins must be rejected")
    except ValueError:
        pass


def test_root_plugins_key_shape_validated() -> None:
    root = {"pipeline": "p.json", "plugins": [1, 2]}
    try:
        validate_root(root)
        raise AssertionError("non-string plugins entries must be rejected")
    except ValueError as e:
        assert "plugins" in str(e)
    validate_root({"pipeline": "p.json", "plugins": ["ok_module"]})  # shape ok


if __name__ == "__main__":
    test_register_and_validate_accepts_new_state_type()
    test_register_rejects_collision_and_unknown_kind()
    test_load_plugins_imports_module_and_surfaces_errors()
    test_root_plugins_key_shape_validated()
    print("ok")
