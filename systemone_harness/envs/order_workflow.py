"""A deterministic order-fulfilment workflow: the environment the tests and the benchmark run.

An order moves new -> picking -> packed -> shipped, or is cancelled. Every action's effect is
fixed, so a run is judged on the model's choices alone. The action space is declared beside it,
as YAML in `examples/order_fulfilment.yaml` and here in code, and both are the same declaration.
"""
from __future__ import annotations

import copy
import json

from ..actions import ActionSpace
from ..environment import Environment, Observation, Result

ACTION_SPACE = {
    "instructions": "You run a warehouse's order desk. Move the order to shipped, or cancel it when the "
                    "goal says so. Ship only after every item is picked and the order is packed.",
    "actions": {
        "pick_item": {
            "description": "Pick one unpicked item off the shelf and put it in the order's bin.",
            "risk": "write",
            "params": {"item": {"from": "unpicked_items",
                                "instructions": "Which unpicked item to pick next?"}},
        },
        "pack": {
            "description": "Pack the bin into a parcel. Only possible once every item is picked.",
            "risk": "write",
        },
        "choose_carrier": {
            "description": "Choose the carrier for the parcel.",
            "risk": "write",
            "params": {"carrier": {"from": "carriers",
                                   "instructions": "Which carrier matches what the goal asks for "
                                                   "(cheapest, fastest, or a named one)?"}},
        },
        "ship": {
            "description": "Hand the packed parcel to the chosen carrier. Only after pack and choose_carrier.",
            "risk": "destructive",
        },
        "cancel_order": {
            "description": "Cancel the order and return any picked items to the shelf.",
            "risk": "destructive",
            "params": {"reason": {"choices": {"customer_request": "the customer asked to cancel",
                                              "out_of_stock": "an item cannot be fulfilled",
                                              "fraud": "the order failed a fraud check"},
                                  "instructions": "Why is the order being cancelled?"}},
        },
        "add_note": {
            "description": "Attach a note to the order for the customer service team.",
            "risk": "read",
            "params": {"note": {"choices": {"delay": "the order will ship later than promised",
                                            "partial": "part of the order is unavailable",
                                            "gift": "the parcel is a gift and needs no invoice"},
                                "instructions": "Which note applies?"}},
        },
    },
    "gate": {"read": 0.5, "write": 0.6, "destructive": 0.8, "finish": 0.5},
}

CARRIERS = {"post": "the national post, cheapest, 5 days",
            "courier": "a courier, mid price, 2 days",
            "express": "overnight express, most expensive, next morning"}


class OrderWorkflow(Environment):
    """One order per episode. `scenario` picks the order; `goal` is what the run is told."""

    SCENARIOS = {
        "ship_cheapest": {"order_id": "A-104", "items": ["blue mug", "tea towel", "kettle"],
                          "goal": "Ship order A-104 with the cheapest carrier."},
        "ship_fastest_gift": {"order_id": "B-220", "items": ["scarf"],
                              "goal": "Order B-220 is a gift: note it, then ship it by the fastest carrier."},
        "cancel_fraud": {"order_id": "C-007", "items": ["laptop", "charger"],
                         "goal": "Order C-007 failed the fraud check. Cancel it."},
    }

    def __init__(self, scenario: str = "ship_cheapest"):
        if scenario not in self.SCENARIOS:
            raise ValueError(f"unknown scenario {scenario!r}; one of {sorted(self.SCENARIOS)}")
        self.scenario = scenario
        self.goal = self.SCENARIOS[scenario]["goal"]
        self.reset(self.goal)

    @staticmethod
    def action_space() -> ActionSpace:
        return ActionSpace.from_dict(copy.deepcopy(ACTION_SPACE))

    def reset(self, goal: str) -> None:
        s = self.SCENARIOS[self.scenario]
        self.order = {"order_id": s["order_id"], "status": "new",
                      "items": [{"name": n, "picked": False} for n in s["items"]],
                      "packed": False, "carrier": None, "notes": [], "cancelled": False,
                      "cancel_reason": None}
        self.log: list[str] = []

    def snapshot(self) -> dict:
        return {"scenario": self.scenario, "order": copy.deepcopy(self.order), "log": list(self.log)}

    def restore(self, snap: dict) -> None:
        self.scenario = snap["scenario"]
        self.order = copy.deepcopy(snap["order"])
        self.log = list(snap["log"])

    def observe(self) -> Observation:
        o = self.order
        unpicked = [i["name"] for i in o["items"] if not i["picked"]]
        picked = [i["name"] for i in o["items"] if i["picked"]]
        text = (f"Order {o['order_id']} is {o['status']}. Picked: {picked or 'none'}. "
                f"Unpicked: {unpicked or 'none'}. Packed: {o['packed']}. Carrier: {o['carrier'] or 'none'}. "
                f"Notes: {o['notes'] or 'none'}.")
        candidates: dict = {"carriers": dict(CARRIERS)}
        if unpicked:
            candidates["unpicked_items"] = unpicked
        return Observation(text=text, fields={"order_id": o["order_id"], "status": o["status"],
                                              "picked": picked, "unpicked": unpicked, "packed": o["packed"],
                                              "carrier": o["carrier"], "notes": list(o["notes"])},
                           candidates=candidates, terminal=o["status"] in ("shipped", "cancelled"))

    def execute(self, action: str, params: dict) -> Result:
        o = self.order
        if o["status"] in ("shipped", "cancelled"):
            return Result(ok=False, text=f"The order is already {o['status']}; nothing more can be done.",
                          terminal=True)
        if action == "pick_item":
            name = params.get("item")
            for it in o["items"]:
                if it["name"] == name and not it["picked"]:
                    it["picked"] = True
                    o["status"] = "picking"
                    return self._ok(f"Picked {name}.")
            return Result(ok=False, text=f"{name!r} is not an unpicked item.")
        if action == "pack":
            if any(not it["picked"] for it in o["items"]):
                return Result(ok=False, text="Cannot pack: some items are still unpicked.")
            o["packed"] = True
            o["status"] = "packed"
            return self._ok("Packed the parcel.")
        if action == "choose_carrier":
            c = params.get("carrier")
            if c not in CARRIERS:
                return Result(ok=False, text=f"{c!r} is not a carrier.")
            o["carrier"] = c
            return self._ok(f"Carrier set to {c}.")
        if action == "ship":
            if not o["packed"]:
                return Result(ok=False, text="Cannot ship: the parcel is not packed.")
            if not o["carrier"]:
                return Result(ok=False, text="Cannot ship: no carrier chosen.")
            o["status"] = "shipped"
            return Result(ok=True, text=f"Shipped by {o['carrier']}.", terminal=True,
                          artifacts=[self._write_manifest()])
        if action == "cancel_order":
            o["status"] = "cancelled"
            o["cancelled"] = True
            o["cancel_reason"] = params.get("reason")
            for it in o["items"]:
                it["picked"] = False
            return Result(ok=True, text=f"Order cancelled ({o['cancel_reason']}).", terminal=True)
        if action == "add_note":
            o["notes"].append(str(params.get("note")))
            return self._ok(f"Note added: {params.get('note')}.")
        return Result(ok=False, text=f"Unknown action {action!r}.")

    def _ok(self, text: str) -> Result:
        self.log.append(text)
        return Result(ok=True, text=text)

    def _write_manifest(self) -> str:
        """The one artifact this environment produces: the shipping manifest, as JSON text."""
        return "manifest.json: " + json.dumps({"order_id": self.order["order_id"],
                                               "carrier": self.order["carrier"],
                                               "items": [i["name"] for i in self.order["items"]],
                                               "notes": self.order["notes"]})

    # what a correct run looks like, for the benchmark's judgment
    def goal_met(self) -> bool:
        o = self.order
        if self.scenario == "ship_cheapest":
            return o["status"] == "shipped" and o["carrier"] == "post"
        if self.scenario == "ship_fastest_gift":
            return o["status"] == "shipped" and o["carrier"] == "express" and "gift" in o["notes"]
        if self.scenario == "cancel_fraud":
            return o["status"] == "cancelled" and o["cancel_reason"] == "fraud"
        return False
