"""`s1`: run a goal against an environment, serve the loop over UHP, or benchmark it.

    s1 run --goal "Ship order A-104 with the cheapest carrier." [--env order:ship_cheapest]
    s1 run --actions my_actions.yaml --env-cmd "python3 my_env.py" --goal "…"
    s1 serve --api-key KEY [--port 8710]
    s1 bench [--runs 3]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from .actions import ActionSpace
from .controller import Controller
from .envs import OrderWorkflow, StdioEnvironment
from .provider import OpenRouterProvider, ProviderError, TypeSafeProvider, provider_from_env
from .trace import Step

PRICE_PER_INPUT_TOKEN = 0.042 / 1_000_000   # TypeSafe's published price; output tokens are free


def _env_and_space(a) -> tuple:
    if a.env_cmd:
        if not a.actions:
            sys.exit("--env-cmd needs --actions <yaml>: the harness cannot guess an environment's actions")
        return StdioEnvironment(a.env_cmd), ActionSpace.from_yaml(a.actions)
    spec = a.env or "order:ship_cheapest"
    if spec.startswith("order"):
        scenario = spec.split(":", 1)[1] if ":" in spec else "ship_cheapest"
        env = OrderWorkflow(scenario)
        space = ActionSpace.from_yaml(a.actions) if a.actions else OrderWorkflow.action_space()
        return env, space
    sys.exit(f"unknown --env {spec!r}; use order[:scenario] or --env-cmd")


def _print_step(s: Step) -> None:
    if s.verdict == "run":
        out = (s.result or {}).get("text", "")
        print(f"  {s.index:3d}  {s.action}({', '.join(f'{k}={v!r}' for k, v in s.params.items())})"
              f"  p={s.action_confidence:.2f} weakest={s.weakest:.2f}  {s.latency_ms} ms\n       -> {out}")
    else:
        print(f"  {s.index:3d}  [{s.verdict}] {s.action}  p={s.action_confidence or 0:.2f}"
              + (f"  weakest={s.weakest:.2f} < {s.threshold:.2f}" if s.verdict == "refused" else "")
              + (f"  ({s.note})" if s.note else ""))


def cmd_run(a) -> int:
    env, space = _env_and_space(a)
    goal = a.goal or getattr(env, "goal", None)
    if not goal:
        sys.exit("--goal is required for this environment")
    try:
        provider = provider_from_env(a.model)
    except ProviderError as exc:
        sys.exit(str(exc))
    print(f"goal: {goal}\nmodel: {provider.model}")
    ctl = Controller(space, env, provider, max_steps=a.max_steps, timeout_seconds=a.timeout, on_step=_print_step)
    t0 = time.time()
    run = ctl.run(goal)
    dt = time.time() - t0
    cost = run.usage["input_tokens"] * PRICE_PER_INPUT_TOKEN
    print(f"\n{run.summary()}\nstatus={run.status} reason={run.reason} steps={len(run.steps)} executed={run.executed} "
          f"wall={dt:.2f}s tokens_in={run.usage['input_tokens']} cost=${cost:.6f} model={run.served_model}")
    if hasattr(env, "goal_met"):
        print(f"goal_met={env.goal_met()}")
    if a.json:
        with open(a.json, "w") as f:
            json.dump(run.to_dict(), f, indent=1, default=str)
        print(f"trace written to {a.json}")
    env.close()
    return 0 if run.status == "completed" else 1


def cmd_serve(a) -> int:
    from .uhp import HarnessDef, Server, serve
    keys = {k for k in (a.api_key or os.environ.get("S1_API_KEY") or "").split(",") if k}
    if not keys:
        sys.exit("an API key is required: --api-key KEY or S1_API_KEY")
    or_key, ts_key = os.environ.get("OPENROUTER_API_KEY"), os.environ.get("TYPESAFE_API_KEY")
    if not (or_key or ts_key):
        sys.exit("no provider key: set OPENROUTER_API_KEY or TYPESAFE_API_KEY")

    def provider_factory(model: str):
        if or_key:
            return OpenRouterProvider(or_key, model)
        return TypeSafeProvider(ts_key, model.split("/")[-1])

    harnesses = []
    if a.actions and a.env_cmd:
        space = ActionSpace.from_yaml(a.actions)
        cmd = a.env_cmd
        harnesses.append(HarnessDef(id="chrn_" + _slug(a.name or "custom"), name=a.name or "custom", space=space,
                                    env_factory=lambda cmd=cmd: StdioEnvironment(cmd)))
    for scenario in OrderWorkflow.SCENARIOS:
        harnesses.append(HarnessDef(id="chrn_order_" + scenario, name=f"Order desk ({scenario})",
                                    space=OrderWorkflow.action_space(),
                                    env_factory=lambda sc=scenario: OrderWorkflow(sc)))
    server = Server(harnesses, provider_factory, keys)
    httpd = serve(server, a.host, a.port)
    print(f"UHP core server on http://{a.host}:{a.port}  harnesses: {', '.join(h.id for h in harnesses)}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_bench(a) -> int:
    try:
        provider = provider_from_env(a.model)
    except ProviderError as exc:
        sys.exit(str(exc))
    rows = []
    for scenario in OrderWorkflow.SCENARIOS:
        for i in range(a.runs):
            env = OrderWorkflow(scenario)
            ctl = Controller(OrderWorkflow.action_space(), env, provider, max_steps=a.max_steps)
            t0 = time.time()
            run = ctl.run(env.goal)
            dt = time.time() - t0
            rows.append({"scenario": scenario, "run": i + 1, "status": run.status, "reason": run.reason,
                         "goal_met": env.goal_met(), "steps": len(run.steps), "executed": run.executed,
                         "refused": sum(1 for s in run.steps if s.verdict == "refused"),
                         "wall_s": round(dt, 2), "ms_per_step": round(dt * 1000 / max(len(run.steps), 1)),
                         "input_tokens": run.usage["input_tokens"],
                         "cost_usd": round(run.usage["input_tokens"] * PRICE_PER_INPUT_TOKEN, 6),
                         "model": run.served_model})
            print(f"{scenario:18s} run {i+1}: {run.status:10s} goal_met={env.goal_met()!s:5s} steps={len(run.steps)} "
                  f"wall={dt:.2f}s tokens={run.usage['input_tokens']}")
    print("\n| scenario | runs | goal met | steps (mean) | ms per step (mean) | wall s (mean) | input tokens (mean) | cost per run |")
    print("|---|---|---|---|---|---|---|---|")
    for scenario in OrderWorkflow.SCENARIOS:
        rs = [r for r in rows if r["scenario"] == scenario]
        n = len(rs)
        print(f"| {scenario} | {n} | {sum(r['goal_met'] for r in rs)}/{n} | {sum(r['steps'] for r in rs)/n:.1f} | "
              f"{sum(r['ms_per_step'] for r in rs)/n:.0f} | {sum(r['wall_s'] for r in rs)/n:.2f} | "
              f"{sum(r['input_tokens'] for r in rs)/n:.0f} | ${sum(r['cost_usd'] for r in rs)/n:.6f} |")
    print(f"\nmodel: {rows[0]['model'] if rows else '?'}; price: ${0.042}/M input tokens, output free (TypeSafe's published price)")
    if a.json:
        with open(a.json, "w") as f:
            json.dump(rows, f, indent=1)
    return 0


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in s.lower())[:40]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="s1", description="A harness for System One models.")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run one goal against an environment")
    r.add_argument("--goal")
    r.add_argument("--env", help="order[:scenario] (built in)")
    r.add_argument("--env-cmd", help="a command that speaks the stdio environment protocol")
    r.add_argument("--actions", help="an action space YAML (required with --env-cmd)")
    r.add_argument("--model")
    r.add_argument("--max-steps", type=int, default=100)
    r.add_argument("--timeout", type=float)
    r.add_argument("--json", help="write the trace here")
    r.set_defaults(fn=cmd_run)
    s = sub.add_parser("serve", help="serve the loop as a UHP core server")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8710)
    s.add_argument("--api-key", help="bearer token(s) clients present, comma separated (or S1_API_KEY)")
    s.add_argument("--actions")
    s.add_argument("--env-cmd")
    s.add_argument("--name")
    s.set_defaults(fn=cmd_serve)
    b = sub.add_parser("bench", help="run the built-in scenarios and report the numbers")
    b.add_argument("--runs", type=int, default=3)
    b.add_argument("--model")
    b.add_argument("--max-steps", type=int, default=20)
    b.add_argument("--json")
    b.set_defaults(fn=cmd_bench)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
