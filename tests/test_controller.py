"""The loop reaches every terminal state for the reason it says, with a scripted model."""
import threading
import time

from systemone_harness import Controller, Environment, Observation, RecordedProvider, Result
from systemone_harness.envs import OrderWorkflow
from policy import choice, fill, perfect


def test_a_perfect_policy_ships_the_order_and_the_trace_holds_every_step():
    env = OrderWorkflow("ship_cheapest")
    seen = []
    run = Controller(OrderWorkflow.action_space(), env, RecordedProvider(perfect), on_step=seen.append).run(env.goal)
    assert run.status == "completed" and run.reason == "environment_terminal" and env.goal_met()
    assert [s.action for s in run.steps] == ["pick_item", "pick_item", "pick_item", "pack", "choose_carrier", "ship"]
    assert len(seen) == 6 and all(s.verdict == "run" for s in run.steps)
    assert run.usage["input_tokens"] > 0 and run.served_model == "recorded/jev"
    assert run.artifacts and run.artifacts[0].startswith("manifest.json")
    assert run.summary() == "The environment reached a terminal state after 6 actions."
    assert run.to_dict()["executed"] == 6


def test_finish_is_checked_against_the_goal_question():
    env = OrderWorkflow("cancel_fraud")
    sure = lambda s, q: fill(q, {"next_action": choice(q, "next_action", "finish"), "goal_reached": {"type": "noul", "noul": 0.9}})
    run = Controller(OrderWorkflow.action_space(), env, RecordedProvider(sure)).run(env.goal)
    assert run.status == "completed" and run.reason == "goal_reached" and run.executed == 0
    assert run.summary() == "Goal reached after 0 actions."
    env = OrderWorkflow("cancel_fraud")
    insist = lambda s, q: fill(q, {"next_action": choice(q, "next_action", "finish"), "goal_reached": {"type": "noul", "noul": 0.1}})
    run = Controller(OrderWorkflow.action_space(), env, RecordedProvider(insist)).run(env.goal)
    assert run.status == "completed" and run.reason == "finish_insisted" and len(run.steps) == 2
    assert "one more step" in run.steps[0].note and "twice" in run.steps[1].note


def test_a_refusal_streak_ends_the_run_and_says_why():
    env = OrderWorkflow("cancel_fraud")
    shaky = lambda s, q: fill(q, {"next_action": choice(q, "next_action", "cancel_order", 0.6),
                                  "cancel_order__reason": choice(q, "cancel_order__reason", "fraud", 0.9)})
    run = Controller(OrderWorkflow.action_space(), env, RecordedProvider(shaky), refusal_streak=3).run(env.goal)
    assert run.status == "incomplete" and run.reason == "no_confident_action"
    assert [s.verdict for s in run.steps] == ["refused"] * 3 and run.steps[0].threshold == 0.8
    assert env.order["status"] == "new"      # nothing ran


def test_escalation_is_a_terminal_reason_of_its_own():
    env = OrderWorkflow("cancel_fraud")
    run = Controller(OrderWorkflow.action_space(), env,
                     RecordedProvider(lambda s, q: fill(q, {"next_action": choice(q, "next_action", "escalate")}))).run(env.goal)
    assert run.status == "incomplete" and run.reason == "escalation_requested"
    assert "asked for help" in run.summary()


def test_a_repeated_action_on_an_unchanged_state_stops_the_loop():
    env = OrderWorkflow("ship_cheapest")
    # `pack` fails while items are unpicked, so the state never changes; the model insists
    run = Controller(OrderWorkflow.action_space(), env,
                     RecordedProvider(lambda s, q: fill(q, {"next_action": choice(q, "next_action", "pack")}))).run(env.goal)
    assert run.status == "incomplete" and run.reason == "repeated_action"
    assert len(run.steps) == 2 and run.steps[0].result["ok"] is False and run.steps[1].result is None


def test_budgets_end_as_incomplete_and_environment_errors_as_failed():
    env = OrderWorkflow("ship_cheapest")
    run = Controller(OrderWorkflow.action_space(), env, RecordedProvider(perfect), max_steps=2).run(env.goal)
    assert run.status == "incomplete" and run.reason == "max_steps" and len(run.steps) == 2

    class Broken(Environment):
        def observe(self):
            return Observation(text="fine", candidates={"unpicked_items": ["a"], "carriers": ["post"]})

        def execute(self, action, params):
            raise RuntimeError("the shelf collapsed")

    run = Controller(OrderWorkflow.action_space(), Broken(), RecordedProvider(perfect)).run("ship")
    assert run.status == "failed" and run.reason == "environment_error" and "shelf collapsed" in run.error
    assert run.summary() == "Failed after 1 action: RuntimeError: the shelf collapsed"   # attempted, and the output says it failed
    assert run.steps[0].result == {"ok": False, "text": "RuntimeError: the shelf collapsed"}


def test_cancel_ends_after_the_step_in_flight():
    class Slow(Environment):
        tick = 0

        def observe(self):
            return Observation(text="t", fields={"unpicked": [], "packed": False, "carrier": None, "tick": self.tick},
                               candidates={"unpicked_items": ["a"], "carriers": ["post"]})

        def execute(self, action, params):
            time.sleep(0.2)
            self.tick += 1
            return Result(text="did " + action)

    env = Slow()
    ctl = Controller(OrderWorkflow.action_space(), env, RecordedProvider(perfect), max_steps=50)
    threading.Timer(0.3, ctl.cancel).start()
    run = ctl.run("go")
    assert run.status == "cancelled" and 1 <= len(run.steps) <= 3
    assert all(s.result for s in run.steps)      # no step was left half-recorded


def test_a_continued_run_keeps_the_environment_and_sees_its_earlier_steps():
    env = OrderWorkflow("ship_cheapest")
    ctl = Controller(OrderWorkflow.action_space(), env, RecordedProvider(perfect), max_steps=3)
    first = ctl.run(env.goal)
    assert first.reason == "max_steps" and env.order["status"] == "picking"
    seen_history = []
    prov = RecordedProvider(lambda s, q: seen_history.append(s.get("history")) or perfect(s, q))
    second = Controller(OrderWorkflow.action_space(), env, prov).run(env.goal, reset=False, prior=first.steps)
    assert second.status == "completed" and env.goal_met()
    assert seen_history[0] and seen_history[0][0].startswith("pick_item(item='blue mug')")


class _Realtime(OrderWorkflow):
    """The order desk, declared real-time: the world moves whether or not a decision is made."""
    def observe(self):
        obs = super().observe()
        obs.realtime = True
        return obs


def test_a_real_time_environment_is_not_stopped_by_refusals_or_repeats():
    env = _Realtime("cancel_fraud")
    shaky = lambda s, q: fill(q, {"next_action": choice(q, "next_action", "cancel_order", 0.6),
                                  "cancel_order__reason": choice(q, "cancel_order__reason", "fraud", 0.9)})
    run = Controller(OrderWorkflow.action_space(), env, RecordedProvider(shaky), refusal_streak=3, max_steps=6).run(env.goal)
    assert run.status == "incomplete" and run.reason == "max_steps"      # six refusals of a WRITE, and the clock is what ends it
    assert [s.verdict for s in run.steps] == ["refused"] * 6
    assert env.order["status"] == "new"                                  # a destructive action still waits for confidence
    env = _Realtime("ship_fastest_gift")
    unsure = lambda s, q: fill(q, {"next_action": choice(q, "next_action", "add_note", 0.4),
                                   "add_note__note": choice(q, "add_note__note", "delay", 0.9)})
    run = Controller(OrderWorkflow.action_space(), env, RecordedProvider(unsure), max_steps=2).run(env.goal)
    assert [s.verdict for s in run.steps] == ["run", "run"]              # a read-risk choice below the threshold runs in real time
    assert "below the read threshold" in run.steps[0].note and env.order["notes"]
    env = _Realtime("ship_cheapest")
    run = Controller(OrderWorkflow.action_space(), env, max_steps=4,
                     provider=RecordedProvider(lambda s, q: fill(q, {"next_action": choice(q, "next_action", "pack")}))).run(env.goal)
    assert run.reason == "max_steps" and len(run.steps) == 4           # the same action on the same state, four times, is four ticks
