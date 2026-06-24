"""Runnable test for is_fizzbuzz. Exits 0 on pass, 1 on fail. No pytest needed.

This test will FAIL against the buggy version of is_fizzbuzz.py
(`return ... or ...`) and PASS after the fix (`return ... and ...`).
"""
from __future__ import annotations

import sys

from is_fizzbuzz import is_fizzbuzz


def main() -> int:
    cases = [
        # divisible by BOTH 3 and 5 -> True
        (15, True),
        (30, True),
        (45, True),
        # divisible by 3 only -> False (the buggy `or` version returns True here)
        (3, False),
        (9, False),
        # divisible by 5 only -> False (the buggy `or` version returns True here)
        (5, False),
        (25, False),
        # not divisible by either -> False
        (1, False),
        (7, False),
    ]
    failures = []
    for n, expected in cases:
        got = is_fizzbuzz(n)
        if got != expected:
            failures.append((n, expected, got))
    if failures:
        for n, exp, got in failures:
            print("FAIL: is_fizzbuzz({}) returned {}, expected {}".format(n, got, exp))
        return 1
    print("OK: all {} cases pass".format(len(cases)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
