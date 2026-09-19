"""The UHP Core surface, driven over HTTP against a scripted model."""
import json
import threading
import time

import httpx
import pytest

from systemone_harness import Environment, Observation, RecordedProvider, Result
from systemone_harness.envs import OrderWorkflow
from systemone_harness.uhp import HarnessDef, Server, serve
from policy import perfect

KEY = "test-key"
H = {"authorization": f"Bearer {KEY}"}


class Slow(Environment):
    """An environment that never ends and takes a while per step, for the cancel tests."""

    def __init__(self):
        self.tick = 0

    def observe(self):
        return Observation(text=f"tick {self.tick}", fields={"unpicked": [], "packed": False, "carrier": None,
                                                              "tick": self.tick},
                           candidates={"unpicked_items": ["a"], "carriers": ["post"]})

    def execute(self, action, params):
        time.sleep(0.15)
        self.tick += 1
        return Result(text=f"did {action} at {self.tick}")


@pytest.fixture(scope="module")
def base():
    harnesses = [HarnessDef(id=f"chrn_order_{sc}", name=f"Order desk ({sc})", space=OrderWorkflow.action_space(),
                            env_factory=lambda sc=sc: OrderWorkflow(sc)) for sc in OrderWorkflow.SCENARIOS]
    harnesses.append(HarnessDef(id="chrn_slow", name="Slow", space=OrderWorkflow.action_space(), env_factory=Slow))
    server = Server(harnesses, lambda model: RecordedProvider(perfect), {KEY})
    httpd = serve(server, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_discovery_needs_no_token_and_every_response_carries_the_version(base):
    r = httpx.get(base + "/v1/uhp")
    assert r.status_code == 200 and r.headers["uhp-version"] == "2026-09-12"
    d = r.json()
    assert d["protocol"] == "uhp" and d["versions"] == ["2026-09-12"] and d["conformance_class"] == "core"
    assert d["capabilities"]["streaming"] and d["capabilities"]["sessions"] and d["capabilities"]["cancellation"]
    assert d["capabilities"]["files_input"] is False
    r = httpx.get(base + "/v1/harnesses")
    assert r.status_code == 401 and r.headers["uhp-version"] == "2026-09-12"
    assert r.json()["error"]["type"] == "authentication_error"
    assert httpx.get(base + "/v1/harnesses", headers={"authorization": "Bearer nope"}).status_code == 401
    r = httpx.get(base + "/v1/harnesses", headers={**H, "UHP-Version": "1999-01-01"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "unsupported_protocol_version"
    assert r.json()["error"]["detail"]["supported"] == ["2026-09-12"]
    assert httpx.get(base + "/v1/harnesses", headers={**H, "UHP-Version": "2026-09-12"}).status_code == 200


def test_harnesses_and_models_are_listed_and_a_missing_harness_is_a_404(base):
    hs = httpx.get(base + "/v1/harnesses", headers=H).json()["harnesses"]
    assert [h["id"] for h in hs][:3] == ["chrn_order_ship_cheapest", "chrn_order_ship_fastest_gift", "chrn_order_cancel_fraud"]
    assert hs[0]["object"] == "harness" and hs[0]["base"] == "systemone" and hs[0]["defaultModel"] == "~typesafe/jev-latest"
    one = httpx.get(base + "/v1/harnesses/chrn_order_cancel_fraud", headers=H).json()
    assert one["name"] == "Order desk (cancel_fraud)"
    r = httpx.get(base + "/v1/harnesses/chrn_nope", headers=H)
    assert r.status_code == 404 and r.json()["error"]["code"] == "harness_not_found"
    m = httpx.get(base + "/v1/models", headers=H).json()
    assert m["backends"]["systemone"]["default"] == "~typesafe/jev-latest"
    assert [x["id"] for x in m["backends"]["systemone"]["models"]] == ["~typesafe/jev-latest", "typesafe/jev-1.13"]
    hm = httpx.get(base + "/v1/harnesses/chrn_slow/models", headers=H).json()
    assert hm["harness_id"] == "chrn_slow" and hm["backend"] == "systemone"


def test_a_blocking_task_returns_the_whole_run_as_items(base):
    body = {"input": "Ship order A-104 with the cheapest carrier.", "tools": [{"type": "web_search"}], "include": ["x"],
            "metadata": {"harness_id": "chrn_order_ship_cheapest"}}
    r = httpx.post(base + "/v1/responses", json=body, headers=H, timeout=30)
    assert r.status_code == 200
    resp = r.json()
    assert resp["id"].startswith("resp_") and resp["object"] == "response" and resp["status"] == "completed"
    assert resp["metadata"]["session_id"].startswith("hsess") and resp["metadata"]["ignored_fields"] == ["tools", "include"]
    assert resp["metadata"]["reason"] == "environment_terminal" and resp["metadata"]["executed"] == 6
    kinds = [o["type"] for o in resp["output"]]
    assert kinds == ["function_call", "function_call_output"] * 6 + ["message"]
    fc, fco = resp["output"][0], resp["output"][1]
    assert fc["name"] == "pick_item" and json.loads(fc["arguments"]) == {"item": "blue mug"} and fc["call_id"] == fco["call_id"]
    assert fco["output"]
    msg = resp["output"][-1]
    assert msg["role"] == "assistant" and msg["content"][0]["text"] == "The environment reached a terminal state after 6 actions."
    assert resp["usage"]["total_tokens"] == resp["usage"]["input_tokens"] + resp["usage"]["output_tokens"] > 0
    assert resp["model"] == "recorded/jev" and resp["error"] is None and resp["incomplete_details"] is None
    # the stored record is the same thing, and the input items are the goal
    assert httpx.get(base + f"/v1/responses/{resp['id']}", headers=H).json() == resp
    items = httpx.get(base + f"/v1/responses/{resp['id']}/input_items", headers=H).json()
    assert items["object"] == "list" and items["data"][0]["content"][0]["text"] == body["input"]
    r = httpx.get(base + "/v1/responses/resp_missing", headers=H)
    assert r.status_code == 404 and r.json()["error"]["code"] == "response_not_found"


def test_an_unknown_model_is_refused_before_anything_runs(base):
    r = httpx.post(base + "/v1/responses", json={"input": "x", "model": "gpt-9"}, headers=H)
    assert r.status_code == 422 and r.json()["error"]["code"] == "model_unavailable"
    assert r.json()["error"]["detail"]["available"] == ["~typesafe/jev-latest", "typesafe/jev-1.13"]
    r = httpx.post(base + "/v1/responses", json={}, headers=H)
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_input"


def _stream(base, body):
    events = []
    with httpx.stream("POST", base + "/v1/responses", json={**body, "stream": True}, headers=H, timeout=30) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["uhp-version"] == "2026-09-12"
        for line in r.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def test_a_streamed_task_is_gapless_progressive_and_ends_with_one_terminal_event(base):
    events = _stream(base, {"input": [{"role": "user", "content": [{"type": "input_text", "text": "Cancel A-104, it is fraud."}]}],
                            "metadata": {"harness_id": "chrn_order_cancel_fraud"}})
    assert [e["sequence_number"] for e in events] == list(range(len(events)))
    assert events[0]["type"] == "response.created" and events[0]["response"]["status"] == "in_progress"
    assert events[1]["type"] == "response.in_progress"
    terminal = [e for e in events if e["type"] in ("response.completed", "response.incomplete", "response.failed")]
    assert len(terminal) == 1 and terminal[0] is events[-1]
    final = terminal[0]["response"]
    assert final["status"] == "completed" and final["output"][-1]["type"] == "message" and final["usage"]["total_tokens"] > 0
    # progressive: items and text arrive before the terminal event
    types = [e["type"] for e in events]
    assert "response.output_item.done" in types and "response.output_text.delta" in types
    assert types.index("response.output_text.delta") < len(types) - 1
    stored = httpx.get(base + f"/v1/responses/{final['id']}", headers=H).json()
    assert stored == final


def test_continuation_keeps_the_session_and_the_environment(base):
    first = httpx.post(base + "/v1/responses", json={"input": "Ship A-104 cheaply.", "max_step": 2,
                                                       "metadata": {"harness_id": "chrn_order_ship_cheapest"}},
                       headers=H, timeout=30).json()
    assert first["status"] == "incomplete" and first["incomplete_details"] == {"reason": "max_steps"}
    assert first["metadata"]["executed"] == 2
    second = httpx.post(base + "/v1/responses", json={"input": "Carry on.", "previous_response_id": first["id"]},
                        headers=H, timeout=30).json()
    assert second["status"] == "completed" and second["previous_response_id"] == first["id"]
    assert second["metadata"]["session_id"] == first["metadata"]["session_id"]
    assert second["metadata"]["executed"] == 4          # the four steps the first run had not done
    names = [o["name"] for o in second["output"] if o["type"] == "function_call"]
    assert names == ["pick_item", "pack", "choose_carrier", "ship"]
    r = httpx.post(base + "/v1/responses", json={"input": "x", "previous_response_id": "resp_missing"}, headers=H)
    assert r.status_code == 404 and r.json()["error"]["code"] == "response_not_found"
    r = httpx.post(base + "/v1/responses", json={"input": "x", "previous_response_id": first["id"],
                                                  "metadata": {"harness_id": "chrn_slow"}}, headers=H)
    assert r.status_code == 409 and r.json()["error"]["code"] == "harness_mismatch"


def test_cancel_stops_a_running_task_and_leaves_a_finished_one_alone(base):
    done = httpx.post(base + "/v1/responses", json={"input": "Cancel A-104, fraud.",
                                                      "metadata": {"harness_id": "chrn_order_cancel_fraud"}},
                      headers=H, timeout=30).json()
    r = httpx.post(base + f"/v1/responses/{done['id']}/cancel", headers=H)
    assert r.status_code == 200 and r.json() == done
    running = httpx.post(base + "/v1/responses", json={"input": "Never ends.", "background": True,
                                                         "metadata": {"harness_id": "chrn_slow"}}, headers=H).json()
    assert running["status"] == "in_progress"
    sid = running["metadata"]["session_id"]
    r = httpx.post(base + "/v1/responses", json={"input": "again", "previous_response_id": running["id"]}, headers=H)
    assert r.status_code == 409 and r.json()["error"]["code"] == "session_busy"
    time.sleep(0.3)
    r = httpx.post(base + f"/v1/sessions/{sid}/cancel", headers=H)
    assert r.status_code == 200 and r.json()["id"] == running["id"]
    deadline = time.time() + 10
    while time.time() < deadline:
        cur = httpx.get(base + f"/v1/responses/{running['id']}", headers=H).json()
        if cur["status"] != "in_progress":
            break
        time.sleep(0.05)
    assert cur["status"] == "cancelled" and cur["metadata"]["reason"] == "cancelled" and cur["metadata"]["executed"] >= 1
    assert cur["output"][-1]["content"][0]["text"].startswith("Cancelled after")
    assert httpx.post(base + f"/v1/sessions/{sid}/cancel", headers=H).json() == {"session_id": sid, "status": "idle"}
    r = httpx.post(base + "/v1/sessions/hsess_nope/cancel", headers=H)
    assert r.status_code == 404 and r.json()["error"]["code"] == "session_not_found"


def test_delete_forgets_the_record(base):
    done = httpx.post(base + "/v1/responses", json={"input": "Cancel A-104, fraud.",
                                                      "metadata": {"harness_id": "chrn_order_cancel_fraud"}},
                      headers=H, timeout=30).json()
    r = httpx.request("DELETE", base + f"/v1/responses/{done['id']}", headers=H)
    assert r.status_code == 200 and r.json() == {"id": done["id"], "object": "response", "deleted": True}
    assert httpx.get(base + f"/v1/responses/{done['id']}", headers=H).status_code == 404
    assert httpx.get(base + "/nowhere", headers=H).status_code == 404
