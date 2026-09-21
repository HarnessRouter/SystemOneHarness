"""The configuration: everything that shapes the reflex's decisions except the model and the world.

One YAML, the action-space file extended, kept beside the environment and versioned with it:

    version: 3                    # every change is a new version; the trace carries it
    instructions: "..."           # what the model is told
    gate: {read: 0.5, write: 0.7, destructive: 0.9}
    encoder: {history_steps: 3}   # how much of the past it is shown
    tunables: {horizon: 24}       # numbers the environment's rendering reads (through the package)
    actions: {...}                # optional: declared actions, guards, escalate, two_request
    objective:                    # what the environment counts as success, failure and place
      pass: "cleared == true"
      failure: "lives decreased"
      metrics: [{field: cleared, better: true}, {field: deaths, better: lower}]
      locus: [level_x]
      evidence: observations/

Skills (a directory of SKILL.md files, or several) are compiled into the instructions once per
load: a reflex gets one model call per step and cannot open a file inside it, so what a skill
says has to be in what the model is told before the run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .actions import ActionSpace, DEFAULT_GATE

SKILL_BUDGET_CHARS = 12000


@dataclass
class Config:
    version: int = 0
    instructions: str = ""
    gate: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_GATE))
    encoder: dict = field(default_factory=dict)
    tunables: dict = field(default_factory=dict)
    objective: dict | None = None
    raw: dict = field(default_factory=dict)
    path: str | None = None
    skills: list[str] = field(default_factory=list)   # the skill files compiled in, for the trace

    @classmethod
    def from_dict(cls, d: dict, path: str | None = None) -> "Config":
        gate = dict(DEFAULT_GATE)
        for k, v in (d.get("gate") or {}).items():
            gate[k] = float(v)
        return cls(version=int(d.get("version") or 0), instructions=str(d.get("instructions") or ""), gate=gate,
                   encoder=dict(d.get("encoder") or {}), tunables=dict(d.get("tunables") or {}),
                   objective=dict(d["objective"]) if d.get("objective") else None, raw=dict(d), path=path)

    @classmethod
    def load(cls, path: str | Path, skill_dirs: list[str] | None = None) -> "Config":
        p = Path(path)
        c = cls.from_dict(yaml.safe_load(p.read_text()) or {}, path=str(p))
        for d in skill_dirs or []:
            c.compile_skills(d)
        return c

    def compile_skills(self, root: str | Path) -> None:
        """Append every SKILL.md under `root` to the instructions, within a budget."""
        files = sorted(Path(root).rglob("SKILL.md"))
        for f in files:
            text = f.read_text().strip()
            if not text:
                continue
            room = SKILL_BUDGET_CHARS - sum(len(s) for s in self.skills)
            if room <= 0:
                break
            self.instructions = (self.instructions.rstrip() + "\n\n" + text[:room]).strip()
            self.skills.append(str(f))

    def overlay(self) -> dict:
        """What an MCP-backed action space takes from the file: the tools are the actions."""
        d = {"instructions": self.instructions, "gate": self.gate}
        if "escalate" in self.raw:
            d["escalate"] = bool(self.raw["escalate"])
        return d

    def space(self) -> ActionSpace | None:
        """The declared action space, when the file declares actions."""
        if not self.raw.get("actions"):
            return None
        d = dict(self.raw)
        d["instructions"] = self.instructions
        d["gate"] = self.gate
        return ActionSpace.from_dict(d)

    def to_dict(self) -> dict:
        return {"version": self.version, "instructions": self.instructions, "gate": self.gate, "encoder": self.encoder,
                "tunables": self.tunables, "objective": self.objective, "skills": self.skills, "path": self.path}
