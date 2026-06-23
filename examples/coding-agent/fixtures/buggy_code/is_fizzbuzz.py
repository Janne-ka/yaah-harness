"""is_fizzbuzz — return True if n is divisible by BOTH 3 and 5.

Used by the coding-agent example as a small bug-fixture: the function has
the wrong boolean operator. A coding agent reads this file, identifies
the bug, edits it, and runs test_is_fizzbuzz.py to verify the fix.
"""


def is_fizzbuzz(n: int) -> bool:
    return n % 3 == 0 or n % 5 == 0
