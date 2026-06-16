"""extract_json: tolerant JSON extraction from LLM text.

Run: cd yaah && PYTHONPATH=src python3 tests/test_jsonio.py
"""
from __future__ import annotations

import json

from yaah.jsonio import extract_json


def main() -> None:
    # plain JSON
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('[1, 2, 3]') == [1, 2, 3]

    # ```json fenced (what real claude -p emits)
    fenced = '```json\n{\n  "lens": "security",\n  "findings": [{"id": "SEC-001"}]\n}\n```'
    assert extract_json(fenced) == {"lens": "security", "findings": [{"id": "SEC-001"}]}

    # bare ``` fence, no language tag
    assert extract_json('```\n{"x": true}\n```') == {"x": True}

    # prose around the object
    prose = 'Here is the result:\n{"summary": "ok", "findings": []}\nHope that helps!'
    assert extract_json(prose) == {"summary": "ok", "findings": []}

    # braces inside strings must not fool the balancer
    tricky = 'noise {"msg": "a } b { c", "n": 1} trailing'
    assert extract_json(tricky) == {"msg": "a } b { c", "n": 1}

    # apostrophe in prose must NOT open a JSON string (assessment #4): JSON has no
    # single-quoted strings, so `'` in prose used to swallow the real {...} as
    # string content and the recovered "JSON" then failed to parse.
    apostrophe = "It's the result: {\"a\": 1}"
    assert extract_json(apostrophe) == {"a": 1}
    # multiple apostrophes around the JSON — none of them should fake-open a string
    apostrophe_multi = "Here's what I think you're looking for: {\"x\": 2} OK?"
    assert extract_json(apostrophe_multi) == {"x": 2}
    # an apostrophe INSIDE a JSON string is just data, not a quote
    inside = '{"text": "it\'s fine", "n": 3}'
    assert extract_json(inside) == {"text": "it's fine", "n": 3}

    # nothing parseable -> raises
    try:
        extract_json("no json here at all")
        raise AssertionError("expected JSONDecodeError")
    except json.JSONDecodeError:
        pass

    print("ok")


if __name__ == "__main__":
    main()
