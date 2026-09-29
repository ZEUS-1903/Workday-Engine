"""Safe evaluator for condition rules.

Workday lets administrators attach *condition rules* to business process
steps (entry conditions, validation conditions, while-running conditions on
approval chains). This module is our stand-in: conditions are written as small
Python-like expressions, for example::

    data.salary >= 150000
    pay_change_pct > 10 or job_profile.management_level < current_job_profile.management_level
    approver.management_level >= 3

Expressions are parsed with :mod:`ast` and only a whitelist of node types is
allowed. There are no function calls, no subscripts, no imports and no access
to attributes that start with an underscore, so a definition file cannot be
used to run arbitrary code. ``eval`` is never called.

Missing values evaluate to ``None``. Ordering comparisons (``<``, ``>`` ...)
involving ``None`` are ``False`` rather than raising, which mirrors how a rule
on an empty field simply does not match.
"""

from __future__ import annotations

import ast
import operator
from typing import Any

MAX_EXPRESSION_LENGTH = 500

# Top-level names a condition may reference. Anything else is almost
# certainly a typo, so we reject it when the definition is loaded.
ALLOWED_ROOT_NAMES = frozenset(
    {
        "data",  # proposed values on the event (salary, job_profile_id, effective_date ...)
        "worker",  # current snapshot of the event subject (None for a hire)
        "job_profile",  # the proposed job profile (pay range, management level)
        "current_job_profile",  # the subject's current job profile
        "pay_change_pct",  # computed % change between current and proposed salary
        "initiator",  # snapshot of the person who initiated the event
        "approver",  # candidate approver (only inside while-running conditions)
    }
)

_ALLOWED_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.USub,
    ast.UAdd,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Mod,
    ast.Compare,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.Is,
    ast.IsNot,
    ast.Name,
    ast.Load,
    ast.Attribute,
    ast.Constant,
    ast.List,
    ast.Tuple,
)

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
}

_ORDERING_OPS = {
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}


class ConditionError(ValueError):
    """Raised when a condition cannot be parsed or evaluated safely."""


def compile_condition(expression: str) -> ast.Expression:
    """Parse and validate a condition. Raises :class:`ConditionError`."""
    if not isinstance(expression, str) or not expression.strip():
        raise ConditionError("Condition must be a non-empty string")
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise ConditionError(f"Condition is longer than {MAX_EXPRESSION_LENGTH} characters")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        raise ConditionError(f"Invalid condition syntax in {expression!r}: {exc.msg}") from None

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ConditionError(
                f"{type(node).__name__} is not allowed in condition {expression!r}"
            )
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ConditionError(f"Private attribute {node.attr!r} is not allowed")
        if isinstance(node, ast.Constant) and not isinstance(
            node.value, (str, int, float, bool, type(None))
        ):
            raise ConditionError(f"Constant {node.value!r} is not allowed")

    _check_root_names(tree.body, expression)
    return tree


def _check_root_names(node: ast.AST, expression: str) -> None:
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id not in ALLOWED_ROOT_NAMES:
            allowed = ", ".join(sorted(ALLOWED_ROOT_NAMES))
            raise ConditionError(
                f"Unknown name {child.id!r} in condition {expression!r}. Allowed: {allowed}"
            )


def evaluate(expression: str, context: dict[str, Any]) -> Any:
    """Evaluate ``expression`` against ``context`` and return the raw value."""
    tree = compile_condition(expression)
    return _eval(tree.body, context)


def is_true(expression: str | None, context: dict[str, Any]) -> bool:
    """Evaluate a condition as a boolean. An empty condition counts as true."""
    if expression is None:
        return True
    return bool(evaluate(expression, context))


def _eval(node: ast.AST, ctx: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Name):
        return ctx.get(node.id)

    if isinstance(node, ast.Attribute):
        base = _eval(node.value, ctx)
        if base is None:
            return None
        if isinstance(base, dict):
            return base.get(node.attr)
        raise ConditionError(f"Cannot read attribute {node.attr!r} of a {type(base).__name__}")

    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(element, ctx) for element in node.elts]

    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            result: Any = True
            for value in node.values:
                result = _eval(value, ctx)
                if not result:
                    return result
            return result
        result = False
        for value in node.values:
            result = _eval(value, ctx)
            if result:
                return result
        return result

    if isinstance(node, ast.UnaryOp):
        operand = _eval(node.operand, ctx)
        if isinstance(node.op, ast.Not):
            return not operand
        if operand is None:
            return None
        _require_number(operand)
        return -operand if isinstance(node.op, ast.USub) else +operand

    if isinstance(node, ast.BinOp):
        left = _eval(node.left, ctx)
        right = _eval(node.right, ctx)
        if left is None or right is None:
            return None
        # Numbers only: this blocks string/list repetition ("x" * 10**9) and
        # %-formatting, which could otherwise exhaust memory.
        _require_number(left)
        _require_number(right)
        try:
            return _BIN_OPS[type(node.op)](left, right)
        except ZeroDivisionError:
            return None
        except OverflowError:
            raise ConditionError("Number too large in condition") from None

    if isinstance(node, ast.Compare):
        left = _eval(node.left, ctx)
        for op, comparator in zip(node.ops, node.comparators):
            right = _eval(comparator, ctx)
            if not _compare(op, left, right):
                return False
            left = right
        return True

    raise ConditionError(f"Unsupported expression element {type(node).__name__}")


def _require_number(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConditionError(f"Arithmetic is only allowed on numbers, got {type(value).__name__}")


def _compare(op: ast.cmpop, left: Any, right: Any) -> bool:
    if isinstance(op, ast.Eq):
        return left == right
    if isinstance(op, ast.NotEq):
        return left != right
    if isinstance(op, ast.Is):
        return left is right
    if isinstance(op, ast.IsNot):
        return left is not right
    if isinstance(op, (ast.In, ast.NotIn)):
        if right is None:
            return isinstance(op, ast.NotIn)
        contained = left in right
        return contained if isinstance(op, ast.In) else not contained
    # Ordering comparison
    if left is None or right is None:
        return False
    try:
        return _ORDERING_OPS[type(op)](left, right)
    except TypeError as exc:
        raise ConditionError(f"Cannot compare {left!r} and {right!r}: {exc}") from None
