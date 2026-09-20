"""The order desk served over MCP is the same environment as the one in process."""
import sys

from systemone_harness import Controller, RecordedProvider
from systemone_harness.envs import McpEnvironment, OrderWorkflow
from policy import perfect

SERVER = {"command": sys.executable, "args": ["-m", "systemone_harness.envs.order_mcp", "--scenario", "ship_cheapest"]}


def test_the_order_desk_over_mcp_compiles_to_the_same_actions_and_ships_the_order():
    env = McpEnvironment(SERVER)
    try:
        assert [t["name"] for t in env.tools][:2] == ["observe", "reset"]
        cat = env.catalogue(gate=OrderWorkflow.action_space().gate)
        assert cat.observe == "observe" and cat.reset == "reset" and cat.unsupported == {}
        a, b = cat.space.to_dict(), OrderWorkflow.action_space().to_dict()
        assert " ".join(a["instructions"].split()) == " ".join(b["instructions"].split())
        a["instructions"] = b["instructions"] = ""
        assert a == b
        obs = env.observe()
        assert obs.fields["status"] == "new" and obs.candidates["unpicked_items"] == ["blue mug", "tea towel", "kettle"]
        run = Controller(cat.space, env, RecordedProvider(perfect)).run("Ship order A-104 with the cheapest carrier.")
        assert run.status == "completed" and run.reason == "environment_terminal"
        assert [s.action for s in run.steps] == ["pick_item", "pick_item", "pick_item", "pack", "choose_carrier", "ship"]
        assert run.steps[-1].result["text"] == "Shipped by post." and run.steps[-1].result["ok"] is True
        assert run.artifacts and run.artifacts[0].startswith("manifest.json")
        assert env.observe().terminal
        # a failed action comes back as ok=False with the environment's words, not as an exception
        r = env.execute("pack", {})
        assert r.ok is False and "already shipped" in r.text
        env.reset("again")
        assert env.observe().fields["status"] == "new"
    finally:
        env.close()


def test_a_server_that_cannot_start_is_an_error_with_the_reason():
    import pytest
    with pytest.raises(RuntimeError, match="could not open"):
        McpEnvironment({"command": sys.executable, "args": ["-c", "import sys; sys.exit(3)"]}, timeout=10)
