"""
Runtime compatibility helpers for the backend package.

Currently used to keep pydantic 1.x working on Python 3.13+.
"""

from __future__ import annotations

import sys
import typing
from typing import ForwardRef


def _patch_forward_ref_for_py313():
    """
    Python 3.12+ changed ``typing.ForwardRef._evaluate`` signature and made the
    ``recursive_guard`` argument mandatory. Pydantic 1.x still calls the method
    without that keyword which breaks FastAPI import.

    To keep existing schemas working we wrap the original method with a shim
    that provides a default value when FastAPI / pydantic call it.
    """

    if sys.version_info < (3, 12):
        return

    original_evaluate = getattr(ForwardRef, "_evaluate", None)
    if not callable(original_evaluate):
        return

    # Avoid double patching
    if getattr(original_evaluate, "__miniapp_forward_ref_patch__", False):
        return

    sentinel = getattr(typing, "_sentinel", object())

    def _patched_evaluate(
        self: ForwardRef,
        globalns,
        localns,
        type_params=sentinel,
        /,
        **kwargs,
    ):
        recursive_guard = kwargs.pop("recursive_guard", None)
        if recursive_guard is None:
            recursive_guard = set()
        if type_params is sentinel:
            type_params = getattr(typing, "_sentinel", ())
        return original_evaluate(
            self,
            globalns,
            localns,
            type_params,
            recursive_guard=recursive_guard,
            **kwargs,
        )

    setattr(_patched_evaluate, "__miniapp_forward_ref_patch__", True)
    ForwardRef._evaluate = _patched_evaluate  # type: ignore[attr-defined]


_patch_forward_ref_for_py313()

