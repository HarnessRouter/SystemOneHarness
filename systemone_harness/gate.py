"""The gate: the harness's judgment about the model's judgment.

The model reports a distribution; the gate decides whether it is enough for the action's risk. A
read-only action may run at 0.5, a destructive one waits for 0.9. The measure is the weakest
judgment the action depends on (the action's own confidence and each parameter's probability),
not their product: the question is "is there one shaky judgment", which is TypeSafe's own rule.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .actions import ESCALATE, FINISH, NEXT_ACTION, ActionSpace, Compiled


@dataclass
class Verdict:
    action: str
    params: dict = field(default_factory=dict)
    kind: str = "run"            # run | refused | finish | escalate
    threshold: float | None = None
    confidence: float | None = None
    weakest: float | None = None
    judgments: dict = field(default_factory=dict)   # what each probability was, by name

    @property
    def run(self) -> bool:
        return self.kind == "run"


class Gate:
    def __init__(self, thresholds: dict[str, float]):
        self.thresholds = dict(thresholds)

    def judge(self, answers: dict, compiled: Compiled, space: ActionSpace) -> Verdict:
        na = answers.get(NEXT_ACTION) or {}
        action = str(na.get("choice") or "")
        confidence = _num(na.get("confidence"))
        if action == FINISH:
            return Verdict(action=FINISH, kind="finish", confidence=confidence,
                           judgments={NEXT_ACTION: confidence})
        if action == ESCALATE:
            return Verdict(action=ESCALATE, kind="escalate", confidence=confidence,
                           judgments={NEXT_ACTION: confidence})
        if action not in space.actions or action not in compiled.feasible:
            # the model named something it was not offered; the provider guarantees this cannot
            # happen, and the gate refuses rather than trusting that guarantee blindly
            return Verdict(action=action or "(none)", kind="refused", threshold=1.0, confidence=confidence,
                           weakest=0.0, judgments={NEXT_ACTION: confidence})
        spec = space.actions[action]
        params: dict = {}
        judgments: dict = {NEXT_ACTION: confidence}
        for pname, p in spec.params.items():
            key = compiled.param_keys[action][pname]
            a = answers.get(key) or {}
            stated_key = compiled.stated_keys[action].get(pname)
            if stated_key:
                stated = _num((answers.get(stated_key) or {}).get("noul"))
                judgments[stated_key] = max(stated, 1 - stated)
                if stated < 0.5:
                    params[pname] = p.default
                    continue
            value, prob = _read(a, p.kind)
            params[pname] = value
            judgments[key] = prob
        weakest = min((v for v in judgments.values() if v is not None), default=0.0)
        threshold = float(self.thresholds.get(spec.risk, 0.7))
        kind = "run" if weakest >= threshold else "refused"
        return Verdict(action=action, params=params, kind=kind, threshold=threshold,
                       confidence=confidence, weakest=weakest, judgments=judgments)


def _read(answer: dict, kind: str) -> tuple[object, float]:
    t = answer.get("type")
    if t == "choice":
        choice = answer.get("choice")
        probs = answer.get("probabilities") or {}
        p = _num(probs.get(choice, answer.get("confidence")))
        return choice, p
    if t == "noul":
        p = _num(answer.get("noul"))
        return p >= 0.5, max(p, 1 - p)
    if t == "score":
        probs = answer.get("probabilities") or {}
        legend = answer.get("legend") or {}
        if probs:
            top = max(probs, key=lambda k: probs[k])
            return legend.get(top, top), _num(probs[top])
        return answer.get("score"), _num(answer.get("confidence"))
    return None, 0.0


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0
