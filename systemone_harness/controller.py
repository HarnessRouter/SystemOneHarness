"""The loop: observe, encode, ask, gate, execute, record, until a terminal state.

One step is one request. The action, every parameter of every feasible action, the goal check and
the declared guards ride that request and are answered in parallel, so a step costs one round
trip of a few hundred milliseconds and a few hundred tokens. The controller never spends a second
call where one will do.

Terminal states and their reasons:
  completed   goal_reached | finish_insisted | environment_terminal
  incomplete  max_steps | timeout | no_confident_action | repeated_action | escalation_requested
  failed      environment_error | provider_error
  cancelled   cancelled
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Callable

from .actions import GOAL_REACHED, ActionSpace
from .encoder import StateEncoder
from .environment import Environment
from .gate import Gate
from .provider import ProviderError
from .trace import Run, Step


class Controller:
    def __init__(self, space: ActionSpace, env: Environment, provider, encoder: StateEncoder | None = None,
                 gate: Gate | None = None, max_steps: int = 100, timeout_seconds: float | None = None,
                 refusal_streak: int = 3, disabled: set[str] | None = None,
                 on_step: Callable[[Step], None] | None = None, config_version: int = 0,
                 trace_path: str | None = None):
        self.space = space
        self.config_version = config_version
        self.trace_path = trace_path or os.environ.get("SYSTEMONE_TRACE_PATH") or None
        self.env = env
        self.provider = provider
        self.encoder = encoder or StateEncoder(instructions=space.instructions)
        self.gate = gate or Gate(space.gate)
        self.max_steps = max_steps
        self.timeout_seconds = timeout_seconds
        self.refusal_streak = refusal_streak
        self.disabled = set(disabled or ())
        self.on_step = on_step
        self._cancel = threading.Event()

    def cancel(self) -> None:
        """Ends the run after the step in flight; a step is short and nothing is killed mid-act."""
        self._cancel.set()

    def run(self, goal: str, memory: dict | None = None, reset: bool = True, task_instructions: str = "",
            prior: list[Step] | None = None) -> Run:
        run = Run(goal=goal, config_version=self.config_version)
        memory = dict(memory or {})
        history: list[Step] = list(prior or [])   # a continued session carries its earlier steps
        if reset:
            self.env.reset(goal)
        refusals = 0
        finish_insist = 0
        last_sig: tuple | None = None
        try:
            while True:
                if self._cancel.is_set():
                    return self._end(run, "cancelled", "cancelled")
                if len(run.steps) >= self.max_steps:
                    return self._end(run, "incomplete", "max_steps")
                if self.timeout_seconds and time.time() - run.started_at > self.timeout_seconds:
                    return self._end(run, "incomplete", "timeout")

                obs = self.env.observe()
                realtime = bool(getattr(obs, "realtime", False))
                if realtime and self.encoder.history_steps > 3:
                    self.encoder.history_steps = 3
                if obs.artifacts:
                    run.artifacts.extend(a for a in obs.artifacts if a not in run.artifacts)
                if obs.terminal:
                    return self._end(run, "completed", "environment_terminal")
                compiled = self.space.compile(obs.candidates, disabled=self.disabled)
                enc = self.encoder.encode(goal, obs, history + run.steps, memory, task_instructions)
                try:
                    dec = self.provider.decide(enc.state, compiled.questions)
                except ProviderError as exc:
                    run.error = str(exc)
                    return self._end(run, "failed", "provider_error")
                verdict = self.gate.judge(dec.answers, compiled, self.space)
                note = "; ".join(enc.truncations)
                if realtime and verdict.kind == "refused" and verdict.action in self.space.actions \
                        and self.space.actions[verdict.action].risk == "read":
                    # In real time not deciding is a decision: the last keys stay held, the world
                    # moves. A refusal of a harmless action buys nothing a run of it would not, so
                    # the strongest choice runs and the verdict says it ran below the threshold.
                    verdict.kind = "run"
                    note = _join(note, f"strongest choice {verdict.weakest:.2f}, below the read threshold; in real time it runs")
                if compiled.omitted:
                    note = "; ".join(x for x in (note, "not offered: " + ", ".join(
                        f"{k} ({v})" for k, v in compiled.omitted.items())) if x)
                step = Step(index=len(run.steps), started_at=time.time(), state=enc.state, state_tokens=enc.tokens,
                            questions=compiled.questions, answers=dec.answers, action=verdict.action,
                            params=verdict.params, action_confidence=verdict.confidence, weakest=verdict.weakest,
                            threshold=verdict.threshold, verdict=verdict.kind, result=None,
                            latency_ms=dec.latency_ms, usage=dec.usage, served_model=dec.model,
                            request_id=dec.request_id, note=note)
                run.usage["input_tokens"] += dec.usage.get("input_tokens", 0)
                run.usage["output_tokens"] += dec.usage.get("output_tokens", 0)
                run.served_model = dec.model or run.served_model
                goal_p = _num((dec.answers.get(GOAL_REACHED) or {}).get("noul"))

                if verdict.kind == "finish":
                    finish_insist += 1
                    if goal_p >= self.space.gate.get("finish", 0.5):
                        step.note = _join(step.note, f"goal_reached {goal_p:.2f}")
                        self._record(run, step)
                        return self._end(run, "completed", "goal_reached")
                    if finish_insist >= 2:
                        step.note = _join(step.note, f"chosen twice while goal_reached stayed {goal_p:.2f}")
                        self._record(run, step)
                        return self._end(run, "completed", "finish_insisted")
                    step.note = _join(step.note, f"goal_reached only {goal_p:.2f}; one more step")
                    self._record(run, step)
                    continue
                if verdict.kind == "escalate":
                    self._record(run, step)
                    return self._end(run, "incomplete", "escalation_requested")
                if verdict.kind == "refused":
                    refusals += 1
                    finish_insist = 0
                    self._record(run, step)
                    if refusals >= self.refusal_streak and not realtime:
                        return self._end(run, "incomplete", "no_confident_action")
                    continue

                refusals = 0
                finish_insist = 0
                sig = (verdict.action, json.dumps(verdict.params, sort_keys=True, default=str),
                       _state_hash(obs))
                if sig == last_sig and not realtime:
                    step.note = _join(step.note, "same action on the same state as the previous step")
                    self._record(run, step)
                    return self._end(run, "incomplete", "repeated_action")
                last_sig = sig
                try:
                    result = self.env.execute(verdict.action, verdict.params)
                except Exception as exc:  # noqa: BLE001 - the environment's failure is the run's
                    run.error = f"{type(exc).__name__}: {exc}"
                    step.result = {"ok": False, "text": run.error}
                    self._record(run, step)
                    return self._end(run, "failed", "environment_error")
                step.result = result.to_dict()
                run.artifacts.extend(a for a in result.artifacts if a not in run.artifacts)
                self._record(run, step)
                if result.terminal:
                    return self._end(run, "completed", "environment_terminal")
        finally:
            run.finished_at = run.finished_at or time.time()

    def _record(self, run: Run, step: Step) -> None:
        run.steps.append(step)
        if self.on_step:
            self.on_step(step)

    def _end(self, run: Run, status: str, reason: str) -> Run:
        run.status = status
        run.reason = reason
        run.finished_at = time.time()
        if reason in ("no_confident_action", "escalation_requested") and run.steps:
            # the branch for whoever takes over: the state the reflex could not decide on, what it
            # was asked, what it answered, and how far short it fell
            last = run.steps[-1]
            action = self.space.actions.get(last.action) if last.action else None
            run.handoff = {"reason": reason, "state": last.state, "questions": last.questions, "answers": last.answers,
                           "weakest": last.weakest, "threshold": last.threshold,
                           "risk": action.risk if action is not None else None, "step": last.index}
        if self.trace_path:
            try:
                with open(self.trace_path, "w") as f:
                    json.dump(run.to_dict(), f, indent=1, default=str)
            except OSError:
                pass
        return run


def _state_hash(obs) -> str:
    return hashlib.sha256(json.dumps({"t": obs.text, "f": obs.fields}, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _join(a: str, b: str) -> str:
    return "; ".join(x for x in (a, b) if x)


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0
