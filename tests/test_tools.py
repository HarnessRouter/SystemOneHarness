"""Tools become actions only when the model could answer every parameter; the rest are listed."""
import pytest

from systemone_harness.actions import ActionSpaceError
from systemone_harness.tools import coerce, compile_tools


def tool(name, props=None, required=(), annotations=None, description="does a thing"):
    return {"name": name, "description": description, "annotations": annotations,
            "inputSchema": {"type": "object", "properties": props or {}, "required": list(required)}}


def test_every_enumerable_shape_compiles_and_risk_follows_the_annotations():
    cat = compile_tools([
        tool("observe"), tool("reset", {"goal": {"type": "string"}}),
        tool("peek", annotations={"readOnlyHint": True}),
        tool("delete", annotations={"destructiveHint": True}),
        tool("set", {"mode": {"enum": ["a", "b"], "description": "which mode?"},
                     "reason": {"type": "string", "oneOf": [{"const": "x", "description": "ex"}, {"const": "y", "title": "why"}]},
                     "fast": {"type": "boolean", "default": False},
                     "level": {"type": "integer", "minimum": 1, "maximum": 3, "default": 2},
                     "target": {"type": "string", "x-candidates": "targets"}},
             required=["mode", "reason", "target"]),
    ], instructions="be careful")
    assert cat.observe == "observe" and cat.reset == "reset" and cat.unsupported == {}
    assert cat.space.instructions == "be careful"
    assert {n: a.risk for n, a in cat.space.actions.items()} == {"peek": "read", "delete": "destructive", "set": "write"}
    p = cat.space.actions["set"].params
    assert p["mode"].kind == "choices" and p["mode"].choices == {"a": "a", "b": "b"} and p["mode"].instructions == "which mode?"
    assert p["reason"].choices == {"x": "ex", "y": "why"}
    assert p["fast"].kind == "flag" and p["fast"].optional and p["fast"].default is False
    assert p["level"].kind == "levels" and p["level"].levels == ["1", "2", "3"] and p["level"].default == "2"
    assert p["target"].kind == "candidates" and p["target"].from_field == "targets" and not p["target"].optional
    assert cat.schemas["set"]["required"] == ["mode", "reason", "target"]


def test_what_cannot_be_answered_is_listed_with_its_reason_never_dropped():
    cat = compile_tools([
        tool("observe"), tool("ok"),
        tool("search", {"query": {"type": "string", "description": "what to search"}}, required=["query"]),
        tool("bulk", {"ids": {"type": "array", "items": {"type": "string"}}}),
        tool("count", {"n": {"type": "integer", "minimum": 0, "maximum": 1000}}),
        tool("many", {"k": {"enum": [str(i) for i in range(300)]}}),
        tool("finish"),
    ])
    assert list(cat.space.actions) == ["ok"]
    assert "free text" in cat.unsupported["search"] and "'query'" in cat.unsupported["search"]
    assert "array" in cat.unsupported["bulk"]
    assert "1001 values" in cat.unsupported["count"]
    assert "at most 255" in cat.unsupported["many"]
    assert cat.unsupported["finish"] == "reserved name"
    assert "unsupported search" in cat.table() and "ok " in cat.table()
    with pytest.raises(ActionSpaceError, match="no tool compiled"):
        compile_tools([tool("observe"), tool("search", {"q": {"type": "string"}})])


def test_answers_are_coerced_to_the_declared_types_and_unstated_optionals_left_out():
    schema = {"properties": {"level": {"type": "integer"}, "fast": {"type": "boolean"}, "ratio": {"type": "number"},
                             "name": {"type": "string"}}}
    assert coerce({"level": "2", "fast": "true", "ratio": "0.5", "name": "x", "skip": None}, schema) == \
        {"level": 2, "fast": True, "ratio": 0.5, "name": "x"}


def test_a_tool_may_declare_its_risk_in_its_meta():
    """A key held for a moment in a game changes nothing outside the game; gated as a write it was
    refused three times mid-air and the run stopped (2026-09-19). The tool says its risk."""
    cat = compile_tools([tool("observe"),
                         dict(tool("hop"), meta={"risk": "read"}),
                         dict(tool("erase", annotations={"readOnlyHint": True}), meta={"risk": "destructive"}),
                         dict(tool("odd"), meta={"risk": "reckless"})])
    assert {n: a.risk for n, a in cat.space.actions.items()} == {"hop": "read", "erase": "destructive", "odd": "write"}
