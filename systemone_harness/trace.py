"""The record of a run: every step whole, distributions included.

A System One model reports a distribution over alternatives on every decision. The trace keeps it,
because "the model chose X at 0.73 with Y at 0.27" is the fact, and "the model chose X" is a
rendering of it. Anything that summarises the distribution away belongs in a view, not here.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field


@dataclass
class Step:
    index: int
    started_at: float
    state: dict
    state_tokens: int
    questions: dict
    answers: dict
    action: str | None
    params: dict
    action_confidence: float | None
    weakest: float | None
    threshold: float | None
    verdict: str                 # run | refused | finish | escalate
    result: dict | None
    latency_ms: int
    usage: dict
    served_model: str
    request_id: str
    note: str = ""

    def line(self) -> str:
        """One line for a terminal or a history entry; the trace keeps the rest."""
        if self.verdict == "run":
            out = (self.result or {}).get("text", "")
            return f"{self.action}({_params_text(self.params)}) -> {out}"
        if self.verdict == "refused":
            return (f"refused {self.action}({_params_text(self.params)}): weakest judgment "
                    f"{self.weakest:.2f} below {self.threshold:.2f}")
        if self.verdict == "finish":
            return f"finish proposed ({self.note})" if self.note else "finish"
        return self.verdict


@dataclass
class Run:
    goal: str
    status: str = "in_progress"   # in_progress | completed | incomplete | failed | cancelled
    reason: str = ""
    steps: list[Step] = field(default_factory=list)
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    served_model: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    artifacts: list[str] = field(default_factory=list)
    error: str | None = None
    config_version: int = 0        # the configuration the run was made under
    handoff: dict | None = None    # on no_confident_action or escalation_requested: the refused step, for whoever takes over

    @property
    def executed(self) -> int:
        return sum(1 for s in self.steps if s.verdict == "run")

    def summary(self) -> str:
        """The terminal sentence: what happened, in the harness's words, never the model's."""
        n = self.executed
        acts = "action" if n == 1 else "actions"
        if self.status == "completed":
            if self.reason == "goal_reached":
                return f"Goal reached after {n} {acts}."
            if self.reason == "finish_insisted":
                return (f"Finished after {n} {acts}; the model chose to stop twice while its own "
                        "goal check stayed low.")
            if self.reason == "environment_terminal":
                return f"The environment reached a terminal state after {n} {acts}."
            return f"Completed after {n} {acts}."
        if self.status == "incomplete":
            return {
                "max_steps": f"Stopped at the step budget after {n} {acts}.",
                "timeout": f"Stopped at the time budget after {n} {acts}.",
                "no_confident_action": (f"Stopped after {n} {acts}: no action cleared the confidence "
                                        "threshold on the last attempts."),
                "repeated_action": (f"Stopped after {n} {acts}: the same action on the same state was "
                                    "chosen twice in a row."),
                "escalation_requested": f"Stopped after {n} {acts}: the model asked for help.",
            }.get(self.reason, f"Stopped after {n} {acts} ({self.reason}).")
        if self.status == "cancelled":
            return f"Cancelled after {n} {acts}."
        if self.status == "failed":
            return f"Failed after {n} {acts}: {self.error or self.reason}"
        return f"Running, {n} {acts} so far."

    def to_dict(self) -> dict:
        d = asdict(self)
        d["summary"] = self.summary() + (" A handoff is attached." if self.handoff else "")
        d["executed"] = self.executed
        return d


def _params_text(params: dict) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in (params or {}).items())


def reasoning_text(step: "Step") -> str:
    """One sentence on a step that executed nothing: what the model wanted, how sure it was, and why
    the harness did not act. Harness prose for the trace and for a host's reasoning item."""
    na = step.answers.get("next_action") or {}
    probs = na.get("probabilities") or {}
    ranked = sorted(probs.items(), key=lambda kv: -float(kv[1] or 0))[:3]
    dist = ", ".join(f"{k} {float(v):.2f}" for k, v in ranked)
    if step.verdict == "refused":
        return (f"Refused {step.action}: the weakest judgment was {step.weakest:.2f}, below the {step.threshold:.2f} "
                f"the action's risk requires. Distribution: {dist}.")
    if step.verdict == "finish":
        return f"The model proposed finishing ({step.note}). Distribution: {dist}."
    if step.verdict == "escalate":
        return f"The model asked for help; no offered action fit. Distribution: {dist}."
    return f"Distribution: {dist}."
