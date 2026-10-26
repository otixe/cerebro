"""Configurable public legacy leader roster. DB rows remain authoritative.

Auth bootstrap names are independently configured by BOOTSTRAP_AGENTS in each
service. This list only covers legacy fallback checks when no DB row exists.
"""
import os
import re


def leaders():
    result = set()
    for item in os.environ.get("CEREBRO_LEADERS", "admin").split(","):
        name = item.strip().lower()
        if not name:
            continue
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
            raise ValueError("Invalid name in CEREBRO_LEADERS")
        result.add(name)
    return frozenset(result)
