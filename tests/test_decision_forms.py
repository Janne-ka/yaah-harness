"""The decision-forms catalog: shape + each form's example validates against
its own schema (so the engine ships internally-consistent generic shapes).

What it proves: every built-in form has a non-null schema; its `example`
matches that schema under yaah's own subset checker; `lookup` returns
`{form, schema, example}`; `lookup("json_schema")` requires an inline schema
and otherwise raises; unknown form names raise. Defends the cross-cutting
vocabulary `yaah baton-schema` surfaces to driver skills.

Run: cd yaah && PYTHONPATH=src python3 tests/test_decision_forms.py

Targets Python 3.9+.
"""
from __future__ import annotations

from yaah.harness.decision_forms import FORMS, lookup
from yaah.validators import _check_schema


def main() -> None:
    # the catalog is non-trivial — guard against accidental empty/typo'd entry
    assert "approve" in FORMS and "approve_or_revise" in FORMS \
        and "free_text" in FORMS and "json_schema" in FORMS

    # every built-in form's example passes its own schema (under the same
    # subset checker yaah uses elsewhere). json_schema has no fixed schema —
    # its example is intentionally empty.
    for name, entry in FORMS.items():
        if name == "json_schema":
            assert entry["schema"] is None, name
            continue
        errors = _check_schema(entry["example"], entry["schema"], "$")
        assert not errors, "form {!r} example fails own schema: {}".format(name, errors)

    # lookup returns the documented shape for built-ins
    got = lookup("approve_or_revise")
    assert set(got) == {"form", "schema", "example"}, got
    assert got["form"] == "approve_or_revise"
    assert got["schema"]["properties"]["decision"]["enum"] == ["approve", "revise"]
    assert got["example"] == {"decision": "approve"}

    # json_schema requires an inline schema; without it -> ValueError
    try:
        lookup("json_schema")
    except ValueError as e:
        assert "inline `decision_schema`" in str(e), str(e)
    else:
        raise AssertionError("lookup('json_schema') with no inline schema must raise")

    inline = {"type": "object", "properties": {"verdict": {"type": "string"}}}
    got = lookup("json_schema", inline_schema=inline)
    assert got == {"form": "json_schema", "schema": inline, "example": {}}, got

    # unknown form name raises with the catalog listed
    try:
        lookup("bogus")
    except ValueError as e:
        assert "bogus" in str(e) and "approve" in str(e), str(e)
    else:
        raise AssertionError("lookup('bogus') must raise")

    # catalog membership is a plain `in` check — the public surface is FORMS itself
    assert "approve" in FORMS and "json_schema" in FORMS and "bogus" not in FORMS

    print("PASS decision-forms catalog: 4 forms, examples self-consistent, lookup contract")


if __name__ == "__main__":
    main()
