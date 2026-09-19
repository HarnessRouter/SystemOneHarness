"""The encoder keeps the state inside the model's budget; the gate judges the model's judgment."""
from systemone_harness import ActionSpace, Gate, Observation, StateEncoder
from systemone_harness.encoder import estimate_tokens
from systemone_harness.trace import Step


def _step(i, action, params, verdict="run", text="ok"):
    return Step(index=i, started_at=0, state={}, state_tokens=0, questions={}, answers={}, action=action, params=params,
                action_confidence=0.9, weakest=0.9, threshold=0.6, verdict=verdict,
                result={"text": text} if verdict == "run" else None, latency_ms=1, usage={}, served_model="m", request_id="r")


def test_the_state_carries_goal_observation_bounded_history_and_memory():
    enc = StateEncoder(history_steps=2, instructions="be careful")
    steps = [_step(i, "pick", {"item": f"i{i}"}) for i in range(4)]
    e = enc.encode("ship it", Observation(text="order new", fields={"status": "new"}), steps, {"skill": "x"}, "today")
    assert e.state["goal"] == "ship it" and e.state["instructions"] == "be careful today"
    assert e.state["observation"] == {"summary": "order new", "status": "new"}
    assert e.state["history"] == ["pick(item='i2') -> ok", "pick(item='i3') -> ok"]
    assert e.state["memory"] == {"skill": "x"} and e.truncations == []


def test_over_budget_the_encoder_drops_history_first_then_clips_then_memory_then_fields():
    big = "x" * 9000
    enc = StateEncoder(budget_tokens=estimate_tokens({"goal": "g", "observation": {"summary": big[:2000]}}) + 40, history_steps=3)
    steps = [_step(i, "a", {}, text="y" * 400) for i in range(3)]
    e = enc.encode("g", Observation(text=big, fields={"blob": "z" * 3000}), steps, {"m": "n" * 500})
    assert "history" not in e.state and "memory" not in e.state
    assert e.state["observation"]["summary"].endswith("…") and "blob" not in e.state["observation"]
    assert e.truncations[0] == "dropped the oldest history line"
    assert "clipped the observation summary to 2000 characters" in e.truncations
    assert "dropped memory" in e.truncations and "dropped observation field 'blob'" in e.truncations
    assert e.tokens <= enc.budget


def _space():
    return ActionSpace.from_dict({"actions": {
        "peek": {"description": "look", "risk": "read"},
        "move": {"description": "move", "risk": "write",
                 "params": {"to": {"choices": {"north": "n", "south": "s"}},
                            "fast": {"flag": True, "optional": True, "default": False}}},
        "delete": {"description": "delete", "risk": "destructive"}},
        "gate": {"read": 0.5, "write": 0.7, "destructive": 0.9}})


def _answers(action, conf, extra=None):
    a = {"next_action": {"type": "choice", "choice": action, "probabilities": {action: conf}, "confidence": conf},
         "goal_reached": {"type": "noul", "noul": 0.1}}
    a.update(extra or {})
    return a


def test_the_threshold_follows_the_risk_and_the_weakest_judgment_decides():
    space = _space(); gate = Gate(space.gate); c = space.compile({})
    assert gate.judge(_answers("peek", 0.55), c, space).kind == "run"
    assert gate.judge(_answers("delete", 0.85), c, space).kind == "refused"
    v = gate.judge(_answers("delete", 0.95), c, space)
    assert v.kind == "run" and v.threshold == 0.9
    # a confident action with a shaky parameter is refused on the parameter
    v = gate.judge(_answers("move", 0.95, {
        "move__to": {"type": "choice", "choice": "north", "probabilities": {"north": 0.55, "south": 0.45}, "confidence": 0.1},
        "move__fast__stated": {"type": "noul", "noul": 0.95},
        "move__fast": {"type": "noul", "noul": 0.9}}), c, space)
    assert v.kind == "refused" and v.weakest == 0.55 and v.params == {"to": "north", "fast": True}


def test_an_unstated_optional_parameter_takes_its_default():
    space = _space(); gate = Gate(space.gate); c = space.compile({})
    v = gate.judge(_answers("move", 0.9, {
        "move__to": {"type": "choice", "choice": "south", "probabilities": {"north": 0.05, "south": 0.95}, "confidence": 0.9},
        "move__fast__stated": {"type": "noul", "noul": 0.1},
        "move__fast": {"type": "noul", "noul": 0.9}}), c, space)
    assert v.kind == "run" and v.params == {"to": "south", "fast": False}


def test_finish_escalate_and_an_unoffered_action_are_their_own_verdicts():
    space = _space(); gate = Gate(space.gate); c = space.compile({}, disabled={"delete"})
    assert gate.judge(_answers("finish", 0.8), c, space).kind == "finish"
    assert gate.judge(_answers("escalate", 0.8), c, space).kind == "escalate"
    assert gate.judge(_answers("delete", 0.99), c, space).kind == "refused"
    assert gate.judge(_answers("teleport", 0.99), c, space).kind == "refused"
