"""
backend/execution/swing.py
Compatibility shim while swing-promotion logic is still hosted in agent.py.

The exit module imports this lazily at runtime. Keeping the shim tiny avoids a
larger move while the agent refactor is in progress, and gives a stable import
target for the next extraction pass.
"""
from __future__ import annotations


def _try_promote_to_swing(*args, **kwargs) -> bool:
    from backend.agent import _try_promote_to_swing as _impl
    return _impl(*args, **kwargs)
