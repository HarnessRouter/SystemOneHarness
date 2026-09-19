"""The action space and its compiler: what the model may choose, and what it is never asked."""
import pytest

from systemone_harness import ActionSpace, ActionSpaceError
from systemone_harness.actions import ESCALATE, FINISH, GOAL_REACHED, MAX_OPTIONS, NEXT_ACTION
from systemone_harness.envs import OrderWorkflow


def test_a_free_text_parameter_does_not_compile():
    with pytest.raises(ActionSpaceError, match="free text"):
        ActionSpace.from_dict({"actions": {"search": {"description": "search the web",
                                                      "params": {"query": {"instructions": "what to search"}}}}})


def test_reserved_names_and_bad_risks_are_refused():
    with pytest.raises(ActionSpaceError, match="reserved"):
        ActionSpace.from_dict({"actions": {FINISH: {"description": "x"}}})
    with pytest.raises(ActionSpaceError, match="risk"):
        ActionSpace.from_dict({"actions": {"a": {"description": "x", "risk": "fatal"}}})
    with pytest.raises(ActionSpaceError, match="description"):
        ActionSpace.from_dict({"actions": {"a": {}}})
    with pytest.raises(ActionSpaceError, match="unknown level"):
        ActionSpace.from_dict({"actions": {"a": {"description": "x"}}, "gate": {"medium": 0.5}})


def test_one_request_carries_the_action_every_parameter_and_the_goal_check():
    space = OrderWorkflow.action_space()
    c = space.compile({"unpicked_items": ["mug", "kettle"], "carriers": {"post": "cheap", "express": "fast"}})
    q = c.questions
    assert q[NEXT_ACTION]["type"] == "choice" and q[GOAL_REACHED]["type"] == "noul"
    assert set(q[NEXT_ACTION]["criteria"]) == set(space.actions) | {FINISH, ESCALATE}
    assert q["pick_item__item"]["criteria"] == {"mug": "mug", "kettle": "kettle"}
    assert q["choose_carrier__carrier"]["criteria"] == {"post": "cheap", "express": "fast"}
    assert q["cancel_order__reason"]["type"] == "choice" and q["add_note__note"]["type"] == "choice"
    assert c.param_keys["pick_item"] == {"item": "pick_item__item"}
    assert c.feasible == list(space.actions)


def test_an_action_without_candidates_is_not_offered_this_step():
    space = OrderWorkflow.action_space()
    c = space.compile({"carriers": ["post"]})           # nothing left to pick
    assert "pick_item" not in c.questions[NEXT_ACTION]["criteria"]
    assert "pick_item" in c.omitted and "no candidates" in c.omitted["pick_item"]
    assert "pack" in c.feasible


def test_a_disabled_action_is_withheld_by_omission():
    space = OrderWorkflow.action_space()
    c = space.compile({"unpicked_items": ["a"], "carriers": ["post"]}, disabled={"ship", "cancel_order"})
    assert "ship" not in c.questions[NEXT_ACTION]["criteria"] and "cancel_order" not in c.feasible
    assert c.omitted == {"ship": "disabled", "cancel_order": "disabled"}


def test_the_option_ceiling_is_enforced_and_recorded():
    space = ActionSpace.from_dict({"actions": {"open": {"description": "open a record",
                                                        "params": {"record": {"from": "records"}}}}})
    c = space.compile({"records": [f"r{i}" for i in range(300)]})
    assert len(c.questions["open__record"]["criteria"]) == MAX_OPTIONS
    assert c.truncated == {"open__record": 45}
    with pytest.raises(ActionSpaceError, match="at most"):
        ActionSpace.from_dict({"actions": {"a": {"description": "x", "params": {
            "p": {"choices": {str(i): str(i) for i in range(256)}}}}}})


def test_optional_parameters_get_a_stated_question_and_flags_and_levels_compile():
    space = ActionSpace.from_dict({"actions": {"set": {"description": "set the light", "params": {
        "on": {"flag": True, "instructions": "on or off?"},
        "level": {"levels": ["dim", "medium", "bright"], "instructions": "how bright?", "optional": True,
                  "default": "medium"}}}}})
    c = space.compile({})
    assert c.questions["set__on"]["type"] == "noul"
    assert c.questions["set__level"] == {"type": "score", "instructions": "how bright?", "criteria": ["dim", "medium", "bright"]}
    assert c.stated_keys["set"] == {"level": "set__level__stated"}
    assert c.questions["set__level__stated"]["type"] == "noul"


def test_escalate_can_be_switched_off_and_the_space_round_trips():
    d = {"actions": {"a": {"description": "x", "risk": "read"}}, "escalate": False, "instructions": "be brief"}
    space = ActionSpace.from_dict(d)
    assert ESCALATE not in space.compile({}).questions[NEXT_ACTION]["criteria"]
    again = ActionSpace.from_dict(space.to_dict())
    assert again.to_dict() == space.to_dict()


def test_the_yaml_example_equals_the_environment_declaration():
    from pathlib import Path
    yaml_path = Path(__file__).resolve().parents[1] / "systemone_harness" / "examples" / "order_fulfilment.yaml"
    a = ActionSpace.from_yaml(yaml_path).to_dict()
    b = OrderWorkflow.action_space().to_dict()
    a["instructions"] = b["instructions"] = ""   # whitespace folding differs; the words are the same
    assert a == b
