"""Shared helpers for the tests that assert on the shape of the code.

Two properties in this project are guarded structurally rather than
behaviourally, because behaviour cannot tell the right spelling from the wrong
one: that run() puts no deadline on waiting for a dependency, and that the
serial write does not swallow a cancellation. Both walk a syntax tree and both
need to read a call target or an exception type as a name, so that reading
lives here rather than in each of them.
"""

import ast
from typing import Optional


def dotted_name(node) -> Optional[str]:
    """Render an attribute or name node as a dotted name.

    Returns None for anything else - a call, a subscript, a tuple of exception
    types - so a caller can test for a name without first testing the shape.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else None
    return None
