"""The state the model decides on.

Two rules from the model's own documentation shape this file: the state is capped (32k tokens per
request, the questions inside a 64k total), and irrelevant state degrades accuracy. So the encoder
sends the goal, the observation filtered to what the questions read, a bounded history, and any
memory the run carries, and it measures itself. When the budget is exceeded it drops history
first, then observation detail, then memory, and records what it dropped.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .trace import Step


def estimate_tokens(obj) -> int:
    """A ceiling estimate: four characters per token is generous for English JSON."""
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return len(text) // 4 + 1


@dataclass
class Encoded:
    state: dict
    tokens: int
    truncations: list[str] = field(default_factory=list)


class StateEncoder:
    def __init__(self, budget_tokens: int = 24000, history_steps: int = 8, instructions: str = ""):
        self.budget = budget_tokens
        self.history_steps = history_steps
        self.instructions = instructions

    def encode(self, goal: str, observation, steps: list[Step], memory: dict | None = None,
               task_instructions: str = "") -> Encoded:
        history = [s.line() for s in steps][-self.history_steps:]
        state: dict = {"goal": goal}
        guidance = " ".join(x for x in (self.instructions, task_instructions) if x)
        if guidance:
            state["instructions"] = guidance
        state["observation"] = {"summary": observation.text, **(observation.fields or {})}
        if history:
            state["history"] = history
        if memory:
            state["memory"] = memory
        truncations: list[str] = []
        tokens = estimate_tokens(state)
        for reduce in self._reductions(state):
            if tokens <= self.budget:
                break
            note = reduce()
            if note:
                truncations.append(note)
                tokens = estimate_tokens(state)
        return Encoded(state=state, tokens=tokens, truncations=truncations)

    @staticmethod
    def _reductions(state: dict):
        """What to give up, in order, each step once: history lines, summary detail, memory, history,
        then the observation's own fields largest first."""
        history = state.get("history") or []
        for _ in range(max(len(history) - 1, 0)):
            def drop_oldest():
                state["history"] = state["history"][1:]
                return "dropped the oldest history line"
            yield drop_oldest

        def clip_summary():
            summary = state["observation"].get("summary", "")
            if len(summary) <= 2000:
                return None
            state["observation"]["summary"] = summary[:2000] + " …"
            return "clipped the observation summary to 2000 characters"
        yield clip_summary

        def drop_memory():
            if "memory" not in state:
                return None
            del state["memory"]
            return "dropped memory"
        yield drop_memory

        def drop_history():
            if "history" not in state:
                return None
            del state["history"]
            return "dropped history"
        yield drop_history

        def drop_biggest_field():
            fields = [k for k in state["observation"] if k != "summary"]
            if not fields:
                return None
            biggest = max(fields, key=lambda k: estimate_tokens(state["observation"][k]))
            del state["observation"][biggest]
            return f"dropped observation field {biggest!r}"
        for _ in range(len([k for k in state["observation"] if k != "summary"])):
            yield drop_biggest_field
