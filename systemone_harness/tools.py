"""Tools compiled to actions: the state definition convention.

A System One model cannot call a tool. It cannot write the arguments as text and it cannot stream a
call. So the harness lists an MCP server's tools once, at configuration time, and turns each one
whose input is fully enumerable into an action the model can choose. What cannot be compiled is
listed with its reason, never dropped in silence.

The convention a server follows to be an environment:

  observe            a tool returning {"text", "fields", "candidates", "terminal"}; called every step
  reset              a tool taking {"goal"}; called when a run starts (optional)
  every other tool   an action. Its description is what the model reads. Its risk comes from the
                     MCP annotations: readOnlyHint -> read, destructiveHint -> destructive, else write.

A parameter compiles when its schema is one of:

  enum                          a choice over the values
  oneOf [{const, description}]  a choice over the values, each with its meaning
  boolean                       a yes/no flag
  integer with minimum/maximum  levels, when the range has ten values or fewer
  "x-candidates": "<list>"      a choice over a candidate list the observe tool enumerates each step

A parameter absent from `required` is optional and takes its schema default when the model does not
state it. Anything else (free strings, arrays, objects) makes the tool unsupported.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .actions import ESCALATE, FINISH, GOAL_REACHED, NEXT_ACTION, MAX_OPTIONS, ActionSpace, ActionSpaceError

OBSERVE = "observe"
RESET = "reset"
MAX_LEVELS = 10


class Unsupported(ValueError):
    pass


@dataclass
class ToolCatalogue:
    """What the server's tools became: an action space, the protocol tools, and the rest with reasons."""
    space: ActionSpace
    unsupported: dict[str, str] = field(default_factory=dict)
    observe: str | None = None
    reset: str | None = None
    schemas: dict[str, dict] = field(default_factory=dict)     # action -> input schema, for coercion

    def to_dict(self) -> dict:
        return {"actions": self.space.to_dict()["actions"], "unsupported": dict(self.unsupported),
                "observe": self.observe, "reset": self.reset}

    def table(self) -> str:
        lines = []
        for name, a in self.space.actions.items():
            params = ", ".join(f"{p.name}: {p.kind}" + (" (optional)" if p.optional else "") for p in a.params.values())
            lines.append(f"  {name:24s} {a.risk:12s} {params or '(no parameters)'}")
        lines.append(f"  protocol: observe={self.observe or 'missing'} reset={self.reset or 'none'}")
        for name, why in self.unsupported.items():
            lines.append(f"  unsupported {name}: {why}")
        return "\n".join(lines)


def _risk(annotations: dict | None) -> str:
    a = annotations or {}
    if a.get("readOnlyHint"):
        return "read"
    if a.get("destructiveHint") is True:
        return "destructive"
    return "write"


def _param(name: str, schema: dict, required: set[str]) -> dict:
    d: dict = {"instructions": str(schema.get("description") or schema.get("title") or name),
               "optional": name not in required}
    if "default" in schema:
        d["default"] = schema["default"]
    cand = schema.get("x-candidates")
    if isinstance(cand, str) and cand:
        d["from"] = cand
        return d
    consts = [(o["const"], o.get("description") or o.get("title") or o["const"])
              for o in (schema.get("oneOf") or []) if isinstance(o, dict) and "const" in o]
    if consts:
        d["choices"] = {str(k): str(v) for k, v in consts}
        return d
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        d["choices"] = {str(v): str(v) for v in schema["enum"]}
        return d
    t = schema.get("type")
    if t == "boolean":
        d["flag"] = True
        return d
    if t == "integer" and isinstance(schema.get("minimum"), int) and isinstance(schema.get("maximum"), int):
        lo, hi = schema["minimum"], schema["maximum"]
        if 2 <= hi - lo + 1 <= MAX_LEVELS:
            d["levels"] = [str(i) for i in range(lo, hi + 1)]
            if "default" in d:
                d["default"] = str(d["default"])
            return d
        raise Unsupported(f"parameter {name!r} is an integer over {hi - lo + 1} values; at most {MAX_LEVELS} compile as levels")
    if t in ("string", None):
        raise Unsupported(f"parameter {name!r} needs free text; declare enum, oneOf, or x-candidates")
    raise Unsupported(f"parameter {name!r} is {t}; only enum, boolean, bounded integer and x-candidates compile")


def compile_tools(tools: list[dict], instructions: str = "", gate: dict | None = None, escalate: bool = True,
                  observe_tool: str = OBSERVE, reset_tool: str = RESET) -> ToolCatalogue:
    """`tools` are plain dicts: {name, description, inputSchema, annotations}."""
    actions: dict = {}
    unsupported: dict[str, str] = {}
    schemas: dict[str, dict] = {}
    observe = reset = None
    for t in tools:
        name = str(t.get("name") or "")
        schema = t.get("inputSchema") or {}
        if name == observe_tool:
            observe = name
            continue
        if name == reset_tool:
            reset = name
            continue
        if name in (FINISH, ESCALATE, NEXT_ACTION, GOAL_REACHED):
            unsupported[name] = "reserved name"
            continue
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        try:
            params = {pn: _param(pn, ps or {}, required) for pn, ps in props.items()}
            for pn, pd in params.items():
                if pd.get("choices") and len(pd["choices"]) > MAX_OPTIONS:
                    raise Unsupported(f"parameter {pn!r} has {len(pd['choices'])} values; the model takes at most {MAX_OPTIONS}")
        except Unsupported as exc:
            unsupported[name] = str(exc)
            continue
        actions[name] = {"description": str(t.get("description") or t.get("title") or name).strip(),
                         "risk": _risk(t.get("annotations")), "params": params}
        schemas[name] = schema
    if not actions:
        raise ActionSpaceError("no tool compiled to an action" + (f"; unsupported: {unsupported}" if unsupported else ""))
    space = ActionSpace.from_dict({"actions": actions, "instructions": instructions, "escalate": escalate,
                                   **({"gate": gate} if gate else {})})
    return ToolCatalogue(space=space, unsupported=unsupported, observe=observe, reset=reset, schemas=schemas)


def coerce(params: dict, schema: dict) -> dict:
    """The model answers in strings; the tool wants its declared types. Unstated optionals are left out."""
    props = schema.get("properties") or {}
    out = {}
    for k, v in params.items():
        if v is None:
            continue
        t = (props.get(k) or {}).get("type")
        if t == "integer" and isinstance(v, str) and v.lstrip("-").isdigit():
            v = int(v)
        elif t == "number" and isinstance(v, str):
            try:
                v = float(v)
            except ValueError:
                pass
        elif t == "boolean" and isinstance(v, str):
            v = v.lower() in ("true", "yes", "1")
        out[k] = v
    return out
