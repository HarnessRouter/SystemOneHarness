"""The action space, and its compiler into one `questions` map per step.

A System One model cannot invent an action or a value, so everything it may choose is declared
here before the call: the actions, each parameter as a finite set, each action's risk. The compiler
turns the declaration into the model's questions and enforces the model's ceilings (255 options per
choice) and its one honest boundary: a parameter that would need free text does not compile.

Three kinds of parameter, and no fourth:

  choices:   {"value": "description", ...}     a fixed set          -> one `choice`
  from:      "<candidate list name>"            found in the state   -> one `choice` over what the
                                                                        environment enumerated
  flag: true                                    on or off            -> one `noul`
  levels:    ["low", "medium", "high"]          a bucket             -> one `score`
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml

RISKS = ("read", "write", "destructive")
FINISH = "finish"
ESCALATE = "escalate"
NEXT_ACTION = "next_action"
GOAL_REACHED = "goal_reached"
MAX_OPTIONS = 255
DEFAULT_GATE = {"read": 0.5, "write": 0.7, "destructive": 0.9, "finish": 0.5}
FINISH_DESCRIPTION = "The goal is fully achieved as far as the observation shows; stop here."
ESCALATE_DESCRIPTION = "No available action fits the situation; ask for help and stop."


class ActionSpaceError(ValueError):
    """A declaration the model could not act on. Raised at load time, never at run time."""


@dataclass
class Param:
    name: str
    instructions: str = ""
    choices: dict[str, str] | None = None
    from_field: str | None = None
    flag: bool = False
    levels: list[str] | None = None
    optional: bool = False
    default: object = None

    @property
    def kind(self) -> str:
        if self.choices is not None:
            return "choices"
        if self.from_field:
            return "candidates"
        if self.flag:
            return "flag"
        if self.levels:
            return "levels"
        return "free"

    @classmethod
    def from_dict(cls, name: str, d: dict | None) -> "Param":
        d = d or {}
        choices = d.get("choices")
        if isinstance(choices, list):
            choices = {str(c): str(c) for c in choices}
        p = cls(name=name, instructions=str(d.get("instructions") or ""),
                choices={str(k): str(v) for k, v in choices.items()} if isinstance(choices, dict) else None,
                from_field=d.get("from"), flag=bool(d.get("flag", False)),
                levels=[str(x) for x in d["levels"]] if d.get("levels") else None,
                optional=bool(d.get("optional", False)), default=d.get("default"))
        if p.kind == "free":
            raise ActionSpaceError(
                f"parameter {name!r} would need free text: declare `choices`, `from`, `flag` or "
                "`levels`. A System One model chooses; it does not write.")
        if p.choices is not None and len(p.choices) > MAX_OPTIONS:
            raise ActionSpaceError(f"parameter {name!r} declares {len(p.choices)} choices; the model "
                                   f"takes at most {MAX_OPTIONS}")
        if p.levels is not None and len(p.levels) < 2:
            raise ActionSpaceError(f"parameter {name!r} needs at least two levels")
        return p


@dataclass
class Action:
    name: str
    description: str
    risk: str = "write"
    params: dict[str, Param] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, name: str, d: dict) -> "Action":
        if name in (FINISH, ESCALATE, NEXT_ACTION, GOAL_REACHED):
            raise ActionSpaceError(f"{name!r} is a reserved action name")
        risk = str(d.get("risk") or "write")
        if risk not in RISKS:
            raise ActionSpaceError(f"action {name!r}: risk must be one of {RISKS}, not {risk!r}")
        if not d.get("description"):
            raise ActionSpaceError(f"action {name!r} has no description; the description is what "
                                   "the model reads to choose it")
        params = {pn: Param.from_dict(pn, pd) for pn, pd in (d.get("params") or {}).items()}
        return cls(name=name, description=str(d["description"]), risk=risk, params=params)


@dataclass
class Guard:
    """A question asked every step beside the action, for the harness's own use."""
    name: str
    instructions: str
    type: str = "noul"
    criteria: object = None

    def question(self) -> dict:
        q: dict = {"type": self.type, "instructions": self.instructions}
        if self.criteria is not None:
            q["criteria"] = self.criteria
        return q


@dataclass
class Compiled:
    questions: dict
    param_keys: dict[str, dict[str, str]]     # action -> param -> question key
    stated_keys: dict[str, dict[str, str]]    # action -> param -> stated-question key
    feasible: list[str]
    omitted: dict[str, str]                   # action -> why it is not offered this step
    truncated: dict[str, int]                 # question key -> options dropped


@dataclass
class ActionSpace:
    actions: dict[str, Action]
    gate: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_GATE))
    guards: list[Guard] = field(default_factory=list)
    escalate: bool = True
    instructions: str = ""
    two_request: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "ActionSpace":
        raw = d.get("actions") or {}
        if not raw:
            raise ActionSpaceError("an action space needs at least one action")
        actions = {n: Action.from_dict(n, a or {}) for n, a in raw.items()}
        gate = dict(DEFAULT_GATE)
        for k, v in (d.get("gate") or {}).items():
            if k not in gate:
                raise ActionSpaceError(f"gate: unknown level {k!r}; levels are {tuple(gate)}")
            gate[k] = float(v)
        guards = [Guard(name=n, instructions=str(g.get("instructions") or ""), type=str(g.get("type") or "noul"),
                        criteria=g.get("criteria")) for n, g in (d.get("guards") or {}).items()]
        for g in guards:
            if g.name in (NEXT_ACTION, GOAL_REACHED) or "__" in g.name:
                raise ActionSpaceError(f"guard {g.name!r}: reserved or malformed name")
        return cls(actions=actions, gate=gate, guards=guards, escalate=bool(d.get("escalate", True)),
                   instructions=str(d.get("instructions") or ""), two_request=bool(d.get("two_request", False)))

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ActionSpace":
        return cls.from_dict(yaml.safe_load(Path(path).read_text()) or {})

    @classmethod
    def from_json(cls, text: str) -> "ActionSpace":
        return cls.from_dict(json.loads(text))

    def compile(self, candidates: dict | None = None, disabled: set[str] | None = None) -> Compiled:
        """One `questions` map for this step.

        `candidates` is what the environment enumerated this step, by name; an action whose
        candidate list is empty is not feasible now and is not offered. `disabled` actions are
        never offered, which is enforcement by omission: the model cannot choose what it was not
        shown.
        """
        candidates = candidates or {}
        disabled = disabled or set()
        questions: dict = {}
        param_keys: dict[str, dict[str, str]] = {}
        stated_keys: dict[str, dict[str, str]] = {}
        omitted: dict[str, str] = {}
        truncated: dict[str, int] = {}
        feasible: list[str] = []
        for name, action in self.actions.items():
            if name in disabled:
                omitted[name] = "disabled"
                continue
            qs: dict = {}
            keys: dict[str, str] = {}
            stated: dict[str, str] = {}
            why = ""
            for pname, p in action.params.items():
                key = f"{name}__{pname}"
                q = _param_question(p, candidates, key, truncated)
                if q is None:
                    why = f"no candidates for {pname!r} this step"
                    break
                qs[key] = q
                keys[pname] = key
                if p.optional:
                    skey = f"{key}__stated"
                    qs[skey] = {"type": "noul",
                                "instructions": (f"Does the goal or the observation say anything about "
                                                 f"{pname} for the action {name!r}? Answer no when it is "
                                                 "left unspecified.")}
                    stated[pname] = skey
            if why:
                omitted[name] = why
                continue
            feasible.append(name)
            questions.update(qs)
            param_keys[name] = keys
            stated_keys[name] = stated
        criteria = {n: self.actions[n].description for n in feasible}
        criteria[FINISH] = FINISH_DESCRIPTION
        if self.escalate:
            criteria[ESCALATE] = ESCALATE_DESCRIPTION
        questions = {
            NEXT_ACTION: {"type": "choice",
                          "instructions": ("Given the goal, the observation and the history, choose the single "
                                           "best next action. Choose `finish` only when the goal is already "
                                           "achieved."), "criteria": criteria},
            GOAL_REACHED: {"type": "noul",
                           "instructions": "Is the goal already fully achieved according to the observation?"},
            **{g.name: g.question() for g in self.guards},
            **questions,
        }
        return Compiled(questions=questions, param_keys=param_keys, stated_keys=stated_keys,
                        feasible=feasible, omitted=omitted, truncated=truncated)

    def to_dict(self) -> dict:
        return {"actions": {n: {"description": a.description, "risk": a.risk,
                                "params": {pn: _param_dict(p) for pn, p in a.params.items()}}
                            for n, a in self.actions.items()},
                "gate": dict(self.gate),
                "guards": {g.name: {"type": g.type, "instructions": g.instructions, "criteria": g.criteria}
                           for g in self.guards},
                "escalate": self.escalate, "instructions": self.instructions, "two_request": self.two_request}


def _param_question(p: Param, candidates: dict, key: str, truncated: dict) -> dict | None:
    instructions = p.instructions or f"What value of {p.name}?"
    if p.kind == "choices":
        return {"type": "choice", "instructions": instructions, "criteria": dict(p.choices)}
    if p.kind == "candidates":
        raw = candidates.get(p.from_field)
        if not raw:
            return None
        options = raw if isinstance(raw, dict) else {str(v): str(v) for v in raw}
        options = {str(k): str(v) for k, v in options.items()}
        if len(options) > MAX_OPTIONS:
            truncated[key] = len(options) - MAX_OPTIONS
            options = dict(list(options.items())[:MAX_OPTIONS])
        return {"type": "choice", "instructions": instructions, "criteria": options}
    if p.kind == "flag":
        return {"type": "noul", "instructions": instructions}
    return {"type": "score", "instructions": instructions, "criteria": list(p.levels)}


def _param_dict(p: Param) -> dict:
    d: dict = {"instructions": p.instructions}
    if p.choices is not None:
        d["choices"] = dict(p.choices)
    if p.from_field:
        d["from"] = p.from_field
    if p.flag:
        d["flag"] = True
    if p.levels:
        d["levels"] = list(p.levels)
    if p.optional:
        d["optional"] = True
        d["default"] = p.default
    return d
