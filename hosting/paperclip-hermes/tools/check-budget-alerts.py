#!/usr/bin/env python3
"""Evaluate the deallocator's WDL condition against offline budget payload fixtures.

This deliberately supports only the expression functions used by this workflow;
unknown syntax fails the mock-plan check instead of silently weakening coverage.
"""

from datetime import datetime
import json
from pathlib import Path
import re


def evaluate(expression, payload, tags):
    tokens = re.findall(r"'[^']*'|[A-Za-z_][A-Za-z_0-9]*|[0-9]+(?:\.[0-9]+)?|[^\s]", expression.removeprefix("@"))
    position = 0

    def take(expected=None):
        nonlocal position
        token = tokens[position]
        position += 1
        if expected is not None and token != expected:
            raise ValueError(f"expected {expected}, got {token}")
        return token

    def ticks(value):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()

    functions = {
        "and": lambda *args: all(args), "or": lambda *args: any(args),
        "equals": lambda a, b: a == b, "greaterOrEquals": lambda a, b: a >= b,
        "lessOrEquals": lambda a, b: a <= b, "float": float, "div": lambda a, b: a / b,
        "empty": lambda value: value in (None, "", [], {}),
        "coalesce": lambda *args: next((arg for arg in args if arg is not None), None),
        "ticks": ticks, "utcNow": lambda: "2026-11-26T12:00:00Z",
        "triggerBody": lambda: payload,
        "body": lambda name: {"tags": tags} if name == "Read_VM" else None,
    }

    def value():
        token = take()
        if token.startswith("'"):
            result = token[1:-1]
        elif token in ("true", "false"):
            result = token == "true"
        elif token[0].isdigit():
            result = float(token)
        else:
            take("(")
            args = []
            if tokens[position] != ")":
                args.append(value())
                while tokens[position] == ",":
                    take(",")
                    args.append(value())
            take(")")
            result = functions[token](*args)
        while position < len(tokens) and tokens[position] == "?":
            take("?")
            take("[")
            key = take()[1:-1]
            take("]")
            result = result.get(key) if isinstance(result, dict) else None
        return result

    result = value()
    if position != len(tokens):
        raise ValueError("unconsumed workflow expression")
    return result


def findings(expression, billing_month_verified):
    errors = []
    fixtures = json.loads((Path(__file__).parent / "tests/fixtures/budget-alerts.json").read_text())
    for fixture in fixtures:
        expected = fixture["deallocate"] and (billing_month_verified or fixture["payload"]["data"]["BudgetName"] != "fixture-subscription")
        try:
            actual = evaluate(expression, fixture["payload"], fixture["tags"])
        except (ValueError, TypeError, KeyError, IndexError, ZeroDivisionError):
            # An invalid payload must fail the workflow before its deallocation action.
            actual = False
        if actual != expected:
            errors.append(f"budget payload {fixture['name']}: expected deallocate={expected}, got {actual}")
    return errors
