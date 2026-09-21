"""The configuration, the objective, the scripted provider and the handoff branch."""
import json

import pytest

from systemone_harness import Config, Controller, RecordedProvider, ScriptProvider, evaluate, report, compare
from systemone_harness.envs import OrderWorkflow
from policy import choice, fill


def test_a_configuration_loads_with_version_tunables_encoder_and_objective(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "version: 3\ninstructions: 'Ship it.'\ngate: {write: 0.8}\nencoder: {history_steps: 2}\n"
        "tunables: {horizon: 24}\nobjective:\n  pass: 'shipped == true'\n  failure: 'lives decreased'\n"
        "  metrics: [{field: shipped, better: true}, {field: cost, better: lower}]\n  locus: [stage]\n")
    skills = tmp_path / "skills" / "one"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("# Packing\nPack before choosing a carrier.")
    c = Config.load(tmp_path / "config.yaml", [str(tmp_path / "skills")])
    assert c.version == 3 and c.gate["write"] == 0.8 and c.gate["read"] == 0.5
    assert c.encoder == {"history_steps": 2} and c.tunables == {"horizon": 24}
    assert c.objective["locus"] == ["stage"]
    assert "Pack before choosing a carrier." in c.instructions and c.skills and c.space() is None
    assert c.overlay()["instructions"].startswith("Ship it.")


def test_a_scripted_provider_runs_a_probe_without_a_model():
    env = OrderWorkflow("ship_cheapest")
    run = Controller(OrderWorkflow.action_space(), env, ScriptProvider(["pick_item", "pack"]), config_version=7).run(env.goal)
    assert [s.action for s in run.steps][:2] == ["pick_item", "pack"] and all(s.verdict == "run" for s in run.steps[:2])
    assert run.config_version == 7 and run.served_model == "script/s1"
    assert run.steps[-1].action == "finish"


def test_a_refusal_streak_attaches_the_handoff_and_a_pass_does_not(tmp_path):
    env = OrderWorkflow("ship_cheapest")
    unsure = lambda s, q: fill(q, {"next_action": choice(q, "next_action", "ship", 0.3)})
    ctl = Controller(OrderWorkflow.action_space(), env, RecordedProvider(unsure), trace_path=str(tmp_path / "trace.json"))
    run = ctl.run(env.goal)
    assert run.status == "incomplete" and run.reason == "no_confident_action"
    h = run.handoff
    assert h and h["reason"] == "no_confident_action" and h["weakest"] == pytest.approx(0.3)
    assert h["risk"] in ("write", "destructive") and h["threshold"] == OrderWorkflow.action_space().gate[h["risk"]]
    assert "observation" in h["state"] and "next_action" in h["questions"]
    d = json.load(open(tmp_path / "trace.json"))
    assert d["handoff"]["step"] == h["step"] and d["summary"].endswith("A handoff is attached.")
    env = OrderWorkflow("ship_cheapest")
    from policy import perfect
    run = Controller(OrderWorkflow.action_space(), env, RecordedProvider(perfect)).run(env.goal)
    assert run.handoff is None


def test_escalation_attaches_the_handoff_too():
    env = OrderWorkflow("cancel_fraud")
    esc = lambda s, q: fill(q, {"next_action": choice(q, "next_action", "escalate", 0.9)})
    run = Controller(OrderWorkflow.action_space(), env, RecordedProvider(esc)).run(env.goal)
    assert run.reason == "escalation_requested" and run.handoff["reason"] == "escalation_requested"


def _run(fields_by_step, status="completed", reason="environment_terminal"):
    steps = [{"index": i, "action": "a", "result": {"ok": True, "text": f"step {i}", "fields": f}} for i, f in enumerate(fields_by_step)]
    return {"status": status, "reason": reason, "steps": steps, "started_at": 0.0, "finished_at": 12.5}


OBJ = {"pass": "cleared == true", "failure": "lives decreased",
       "metrics": [{"field": "cleared", "better": True}, {"field": "deaths", "better": "lower"}, {"field": "x", "better": "higher"}],
       "locus": ["x"]}


def test_the_objective_evaluates_a_run_and_groups_failures_by_locus():
    r1 = _run([{"lives": 3, "x": 1}, {"lives": 3, "x": 12}, {"lives": 2, "x": 12}, {"lives": 2, "x": 30, "cleared": True}])
    e = evaluate(r1, OBJ)
    assert e["metrics"]["pass"] is True and e["metrics"]["failures"] == 1 and e["metrics"]["elapsed"] == 12.5 and e["metrics"]["x"] == 30
    assert e["failures"][0]["fields"]["x"] == 12
    r2 = _run([{"lives": 3, "x": 1}, {"lives": 2, "x": 13}, {"lives": 1, "x": 14}], status="incomplete", reason="max_steps")
    rep = report([r1, r2], OBJ)
    assert rep["scoreboard"]["pass"] == 0.5 and rep["scoreboard"]["runs"] == 2
    # a failure is attributed to the last live state before it, so the second run's first death sits at x 1
    assert rep["failure_groups"][0]["locus"] == {"x": 10.0} and rep["failure_groups"][0]["count"] == 2
    assert rep["failure_groups"][1]["locus"] == {"x": 0.0} and rep["failure_groups"][1]["count"] == 1


def test_a_change_is_kept_or_reverted_by_the_ordered_metrics():
    before = [evaluate(_run([{"lives": 3, "x": 5}, {"lives": 2, "x": 9}], status="incomplete", reason="max_steps"), OBJ)]
    after = [evaluate(_run([{"lives": 3, "x": 5}, {"lives": 3, "x": 40, "cleared": True}]), OBJ)]
    assert compare(before, after, OBJ) == "kept" and compare(after, before, OBJ) == "reverted" and compare(after, after, OBJ) == "same"
