"""The environment: where the world is.

An environment observes and executes. It never decides. Its one obligation to the model is to
enumerate candidates: the visible buttons, the open slots, the records on screen. Candidates are
how a value the model could never write becomes a choice it can make.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field


@dataclass
class Observation:
    text: str = ""
    fields: dict = field(default_factory=dict)
    candidates: dict = field(default_factory=dict)   # name -> list[str] or {value: description}
    terminal: bool = False
    artifacts: list[str] = field(default_factory=list)
    # A real-time environment: the world moves whether or not a decision is made, so a refused
    # or repeated decision is a tick with the last action standing, never a reason to stop, and
    # the history the model reads is short (the state is what matters, and tokens are latency).
    realtime: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Result:
    ok: bool = True
    text: str = ""
    fields: dict = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    terminal: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class Environment(ABC):
    """Implement three methods. `observe` and `execute` are the whole contract."""

    def reset(self, goal: str) -> None:
        """Start a fresh episode for `goal`. Optional; a continued session does not call it."""

    @abstractmethod
    def observe(self) -> Observation: ...

    @abstractmethod
    def execute(self, action: str, params: dict) -> Result: ...

    def close(self) -> None:
        """Release what the environment holds. Optional."""
