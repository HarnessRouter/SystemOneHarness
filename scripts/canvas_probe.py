"""Can the model read a sampled canvas? An experiment with ground truth, not a demo.

Opens the Super Mario page in a headless Chrome, plays it with a scripted controller to collect
frames in varied situations, and for every frame records two things: the canvas as the harness's
own sampler renders it at several sample rates, with and without operator-named colours, and the
truth about the situation read from the game's own state (experiment only; the harness never
sees it). Then it asks the live model the situational questions over each rendering and scores
the answers against the truth. Output: a table of accuracy, tokens and latency per sample rate,
with the majority-class baseline beside every accuracy so a number cannot look better than doing
nothing.

    OPENROUTER_API_KEY=... python scripts/canvas_probe.py --frames 24 --out docs/reports/canvas-probe.json
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time

from systemone_harness import provider_from_env
from systemone_harness.envs.browser import BrowserEnvironment

URL = "https://supermarioplay.com/game/mario.html?v=1.0.1"
TILE = 32                     # px: 8 units at unitsize 4, read off the game
ITEMS = {"Coin", "Mushroom", "FireFlower", "Star", "Vine", "Text", "Shell"}
GOAL = "Play this side-scrolling platform game: keep the player moving right and jump over enemies, gaps and obstacles."
INSTRUCTIONS = ("The canvas is drawn as a grid of characters, row 0 at the top, column 0 at the left; the player moves "
                "to the right. Distances are in cells. Judge the situation from the grid alone.")

TRUTH_JS = """JSON.stringify({
  canvas: [document.querySelector('canvas').width, document.querySelector('canvas').height],
  screen: [gamescreen.left, gamescreen.right],
  player: window.player ? {left: player.left, right: player.right, top: player.top, bottom: player.bottom,
                           resting: !!player.resting, dead: !!player.dead} : null,
  characters: (window.characters || []).filter(function(c){ return c.alive && c !== player; }).map(function(c){
    return {title: c.title, left: c.left, right: c.right, top: c.top, bottom: c.bottom}; }),
  solids: (window.solids || []).filter(function(s){ return s.alive; }).map(function(s){
    return {title: s.title, left: s.left, right: s.right, top: s.top, bottom: s.bottom}; }),
  paused: !!window.paused, lives: data && data.lives && data.lives.amount})"""

# the colours of things, read once from the live canvas inside each thing's own box: this is what an
# operator would write into the harness's configuration after one look at the game
COLORS_JS = """(function(boxes){
  var c = document.querySelector('canvas'); var ctx = c.getContext('2d', {willReadFrequently: true});
  var out = {};
  Object.keys(boxes).forEach(function(name){
    var b = boxes[name]; var x = Math.max(0, Math.floor(b[0])), y = Math.max(0, Math.floor(b[1]));
    var w = Math.max(1, Math.floor(b[2])), h = Math.max(1, Math.floor(b[3]));
    var d = ctx.getImageData(x, y, w, h).data, counts = {};
    for (var i = 0; i < w * h; i++) { var k = ((d[i*4] >> 4) << 8) | ((d[i*4+1] >> 4) << 4) | (d[i*4+2] >> 4); counts[k] = (counts[k] || 0) + 1; }
    var top = Object.keys(counts).map(function(k){ return [parseInt(k, 10), counts[k]]; }).sort(function(a, b){ return b[1] - a[1]; });
    out[name] = top.slice(0, 3).map(function(e){ return '#' + [e[0] >> 8, (e[0] >> 4) & 15, e[0] & 15].map(function(v){ return (v * 17).toString(16).padStart(2, '0'); }).join(''); });
  });
  return out;
})"""


def evaluate(env: BrowserEnvironment, expression: str):
    async def go():
        cdp = await env._session.get_or_create_cdp_session()
        r = await cdp.cdp_client.send.Runtime.evaluate(params={"expression": expression, "returnByValue": True}, session_id=cdp.session_id)
        return (r.get("result") or {}).get("value")
    return env._run(go())


def truth_of(t: dict) -> dict | None:
    p = t.get("player")
    if not p or p.get("dead"):
        return None
    sl, sr = t["screen"]
    enemies = [c for c in t["characters"] if c["title"] not in ITEMS]
    ahead = lambda o, tiles: o["left"] > p["right"] and o["left"] - p["right"] <= tiles * TILE   # noqa: E731
    enemy = any(ahead(c, 6) and abs(c["bottom"] - p["bottom"]) <= 2 * TILE for c in enemies)
    enemy_near = any(ahead(c, 3) and abs(c["bottom"] - p["bottom"]) <= 2 * TILE for c in enemies)
    floors = [s for s in t["solids"] if s["title"] == "Floor" and s["top"] >= p["bottom"] - 4]
    gap = False
    x = p["right"]
    while x <= p["right"] + 6 * TILE:
        if not any(f["left"] <= x <= f["right"] for f in floors):
            gap = True
            break
        x += 8
    gap_near = False
    x = p["right"]
    while x <= p["right"] + 3 * TILE:
        if not any(f["left"] <= x <= f["right"] for f in floors):
            gap_near = True
            break
        x += 8
    walls = [s for s in t["solids"] if s["title"] in ("Pipe", "Block", "Brick") and s["bottom"] > p["top"] and s["top"] < p["bottom"]]
    obstacle = any(ahead(s, 3) for s in walls)
    frac = (p["left"] - sl) / max(sr - sl, 1)
    where = "left" if frac < 1 / 3 else "center" if frac < 2 / 3 else "right"
    return {"enemy_ahead": enemy, "gap_ahead": gap, "obstacle_ahead": obstacle, "player_where": where,
            "on_ground": bool(p["resting"]), "next_control": "right_jump" if (enemy_near or gap_near or obstacle) else "right",
            "player_px": [p["left"], p["top"]]}


def questions() -> dict:
    return {
        "enemy_ahead": {"type": "noul", "instructions": "Is there an enemy within 6 cells to the right of the player, at about the player's height?"},
        "gap_ahead": {"type": "noul", "instructions": "Is there a gap in the ground within 6 cells to the right of the player?"},
        "obstacle_ahead": {"type": "noul", "instructions": "Is there a pipe, block or wall within 3 cells to the right of the player, in the player's way?"},
        "on_ground": {"type": "noul", "instructions": "Is the player standing on the ground right now?"},
        "player_where": {"type": "choice", "instructions": "Where is the player horizontally in the frame?",
                         "criteria": {"left": "in the left third", "center": "in the middle third", "right": "in the right third"}},
        "next_control": {"type": "choice", "instructions": "Which control should be held next?",
                         "criteria": {"right": "run right, nothing to clear", "right_jump": "jump while moving right, something to clear",
                                      "jump": "jump in place", "wait": "wait", "left": "move left"}},
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--frames", type=int, default=24)
    ap.add_argument("--rates", default="32x16,64x32,96x48,128x64")
    ap.add_argument("--colors", type=int, default=12)
    ap.add_argument("--model")
    ap.add_argument("--out", default="docs/reports/canvas-probe.json")
    a = ap.parse_args(argv)
    rates = [tuple(int(v) for v in r.lower().split("x")) for r in a.rates.split(",")]
    provider = provider_from_env(a.model)
    random.seed(7)

    env = BrowserEnvironment(headless=True, start_url=URL, canvas=True, canvas_colors=a.colors)
    env.reset("play")
    time.sleep(1.0)
    # ── the operator's legend: the things' colours, read once, away from the background ──
    def band_colors(box):
        return (evaluate(env, f"({COLORS_JS})({json.dumps({'b': box})})") or {}).get("b", [])

    names: dict[str, str] = {}
    for _ in range(60):
        env.execute("hold_key", {"key": "ArrowRight", "duration": "short"})
        t = json.loads(evaluate(env, TRUTH_JS))
        p = t.get("player")
        cw, chh = t["canvas"]
        enemies = [c for c in t["characters"] if c["title"] not in ITEMS and p and c["left"] > p["right"] and c["left"] < cw - TILE]
        if p and enemies and p["resting"]:
            background = set(band_colors([0, 0, cw, chh // 4])[:2] + band_colors([0, p["bottom"] + 2, cw, TILE - 4])[:3])
            def things(box, n=2):
                return [h for h in band_colors(box) if h not in background][:n]
            names.update({h: "sky" for h in band_colors([0, 0, cw, chh // 4])[:1]})
            names.update({h: "ground" for h in band_colors([0, p["bottom"] + 2, cw, TILE - 4])[:1]})
            names.update({h: "player" for h in things([p["left"] + 4, p["top"] + 2, p["right"] - p["left"] - 8, p["bottom"] - p["top"] - 4])})
            e = enemies[0]
            names.update({h: "enemy" for h in things([e["left"] + 4, e["top"] + 2, e["right"] - e["left"] - 8, e["bottom"] - e["top"] - 4])})
            pipes = [s for s in t["solids"] if s["title"] == "Pipe" and 0 <= s["left"] < cw]
            if pipes:
                names.update({h: "pipe" for h in things([pipes[0]["left"] + 6, pipes[0]["top"] + 6, max(8, pipes[0]["right"] - pipes[0]["left"] - 12), TILE], 1)})
            break
    print("legend from the game's own things:", names, file=sys.stderr)

    # ── collect frames: a scripted player on fine ticks, and a balanced sample of situations ──
    # The player runs in short holds so it can jump at the right moment (an enemy within three
    # tiles, a gap or a wall within two); a frame is kept when it adds to a class that is still
    # under a third of the sample (enemy ahead, gap or obstacle ahead, in the air), otherwise every
    # fifth tick, so the truths are not all "nothing ahead, on the ground".
    frames = []
    counts = {"enemy": 0, "hazard": 0, "air": 0, "plain": 0}
    tick = 0
    while len(frames) < a.frames:
        t = json.loads(evaluate(env, TRUTH_JS))
        tr = truth_of(t)
        if tr is None:
            time.sleep(3.0)
            tick = 0
            continue
        tick += 1
        p = t["player"]
        dx = min([c["left"] - p["right"] for c in t["characters"] if c["title"] not in ITEMS and c["left"] > p["right"]] or [9999])
        if p["resting"] and (dx <= 3 * TILE or tr["obstacle_ahead"] or tr["gap_ahead"]):
            env.execute("hold_key", {"key": "ArrowUp", "duration": "medium"})
        env.execute("hold_key", {"key": "ArrowRight", "duration": "short"})
        if tick < 6:
            continue
        evaluate(env, "window.pause && pause(); 'ok'")
        t = json.loads(evaluate(env, TRUTH_JS))
        tr = truth_of(t)
        if tr is None:
            evaluate(env, "window.unpause && unpause(); 'ok'")
            continue
        cls = ("enemy" if tr["enemy_ahead"] else "hazard" if (tr["gap_ahead"] or tr["obstacle_ahead"])
               else "air" if not tr["on_ground"] else "plain")
        third = max(1, len(frames) // 3)
        keep = counts[cls] < third if cls != "plain" else (tick % 5 == 0 and counts["plain"] < 2 * third + 2)
        if not keep:
            evaluate(env, "window.unpause && unpause(); 'ok'")
            continue
        renders = {}
        for cols, rows in rates:
            for named in (False, True):
                env.canvas_cells = (cols, rows)
                env.canvas_legend = dict(names) if named else {}
                obs = env.observe()
                lines = obs.text.split("\n")
                renders[f"{cols}x{rows}:{'named' if named else 'plain'}"] = {
                    "summary": "\n".join(l for l in lines if l.startswith(("Canvas ", "On the canvas"))), "canvas": obs.fields["canvas"]}
        evaluate(env, "window.unpause && unpause(); 'ok'")
        counts[cls] += 1
        frames.append({"truth": tr, "renders": renders})
        print(f"frame {len(frames)} [{cls}]: {tr}", file=sys.stderr)
    env.close()

    # ── ask the model, score against the truth ──
    qs = questions()
    results: dict[str, list] = {}
    for f in frames:
        for key, ren in f["renders"].items():
            state = {"goal": GOAL, "instructions": INSTRUCTIONS, "observation": {"summary": ren["summary"], "canvas": ren["canvas"]}}
            d = provider.decide(state, qs)
            ans = d.answers
            row = {"tokens": d.usage.get("input_tokens", 0), "ms": d.latency_ms}
            for q in ("enemy_ahead", "gap_ahead", "obstacle_ahead", "on_ground"):
                p = float((ans.get(q) or {}).get("noul", 0.5))
                row[q] = {"p": p, "truth": bool(f["truth"][q])}
            for q in ("player_where", "next_control"):
                row[q] = {"choice": (ans.get(q) or {}).get("choice"), "truth": f["truth"][q]}
            results.setdefault(key, []).append(row)

    def majority(vals):
        return max(set(vals), key=vals.count)

    table = []
    for key, rows in results.items():
        line = {"render": key, "n": len(rows), "tokens": round(statistics.mean(r["tokens"] for r in rows)),
                "ms": round(statistics.mean(r["ms"] for r in rows))}
        for q in ("enemy_ahead", "gap_ahead", "obstacle_ahead", "on_ground"):
            truths = [r[q]["truth"] for r in rows]
            acc = sum((r[q]["p"] >= 0.5) == r[q]["truth"] for r in rows) / len(rows)
            base = sum(t == majority(truths) for t in truths) / len(truths)
            brier = statistics.mean((r[q]["p"] - (1.0 if r[q]["truth"] else 0.0)) ** 2 for r in rows)
            line[q] = {"acc": round(acc, 2), "base": round(base, 2), "brier": round(brier, 3), "positives": sum(truths)}
        for q in ("player_where", "next_control"):
            truths = [r[q]["truth"] for r in rows]
            acc = sum(r[q]["choice"] == r[q]["truth"] for r in rows) / len(rows)
            base = sum(t == majority(truths) for t in truths) / len(truths)
            line[q] = {"acc": round(acc, 2), "base": round(base, 2)}
        table.append(line)

    print("\n| render | tokens | ms | enemy ahead | gap ahead | obstacle ahead | on ground | player where | next control |")
    print("|---|---|---|---|---|---|---|---|---|")
    for l in table:
        cells = [f"{l[q]['acc']:.2f} (base {l[q]['base']:.2f})" for q in ("enemy_ahead", "gap_ahead", "obstacle_ahead", "on_ground", "player_where", "next_control")]
        print(f"| {l['render']} | {l['tokens']} | {l['ms']} | " + " | ".join(cells) + " |")
    tr = [f["truth"] for f in frames]
    print(f"\nframes: {len(frames)}; positives: enemy {sum(t['enemy_ahead'] for t in tr)}, gap {sum(t['gap_ahead'] for t in tr)}, "
          f"obstacle {sum(t['obstacle_ahead'] for t in tr)}, on ground {sum(t['on_ground'] for t in tr)}; "
          f"next_control right_jump {sum(t['next_control'] == 'right_jump' for t in tr)}")
    with open(a.out, "w") as fh:
        json.dump({"date": time.strftime("%Y-%m-%d"), "model": provider.model, "frames": frames, "results": results,
                   "table": table, "legend": names, "questions": qs}, fh, indent=1, default=str)
    print(f"written {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
