# The dual loop: a System Two harness that drives a System One harness's configuration

Status: design for discussion (2026-09-21). Nothing here is built yet except the inner loop.

## 1. What we learned by doing it by hand

On 2026-09-20 a reasoning model (Claude in Claude Code) took the Super Mario kit from "stuck at
the first pipe" to clearing level 1-1, without touching the reflex model. Every improvement came
from one loop, run by hand about forty times:

1. run the reflex in the environment and record it (the trace, plus every frame the page shows)
2. read the failures from the recording (a contact sheet around each lost life), not from guesses
3. measure the environment where the failure happened (probes: how far a jump carries, where a
   brake stops, when an enemy spawns)
4. change one thing the reflex is told or shown
5. run again, compare the scoreboard (deaths, furthest point, pass), keep or revert

Three rules made it work and two mistakes cost hours. The rules: one change per iteration; the
change is validated by the reflex model itself, not by a stand-in; and measurements run one at a
time (two games on one machine dropped frames and faked regressions). The mistakes: batching four
changes and having to untangle them, and trusting a deterministic follower of the state's own
advice as a proxy for the model (fourteen patches that took the follower to zero deaths took the
model from three passes in three to none in six).

That loop is the outer loop. The inner loop is the reflex. This design makes the outer loop a
harness of its own, so the improvement that took a person a night runs on the platform.

## 2. The two loops, defined

**Inner loop (System One Harness).** observe, encode, decide, gate, execute, record. One model
call per step, a probability on every transition, two to five decisions a second. It never
generates actions; it selects from the ones the environment declares.

**Outer loop (System Two Harness).** A reasoning harness (a coding agent on HarnessRouter: Claude
Code, pi, or another) running a calibration task over the inner loop: run, record, read, measure,
change one thing, run again, keep or revert, write it down. Minutes per iteration.

The outer loop changes the inner loop's **configuration**, never the reflex's weights and never the
environment's truth. First principles: a configuration is everything that shapes the reflex's
decisions other than the model and the world. There are five parts:

| Part | What it is | Example from the kit |
|---|---|---|
| Instructions | the facts and rules the reflex is told | "a 4-tile pipe is cleared only at a full run, taking off 2 to 3 tiles before it" |
| State rendering | what the environment says and how | the "Now:" line, the 24-tile enemy horizon, "the ground drops 4 tiles" |
| Action space | the actions declared, their guards and notes | run_right, jump_right, jump, walk_left, wait |
| Gate | risk thresholds by action class | read 0.5, write 0.7, destructive 0.9 |
| Pace | how often the reflex decides | act returns at once; no tick |

Today the first is text, the fourth is a table, and the second and third are code inside the
environment. The outer loop can only drive what it can see, diff and revert, so the design's first
job is to make the configuration an explicit, versioned object.

## 3. The configuration surface

The inner harness gets a `config` that lives beside the environment and travels with the kit:

```yaml
version: 12                      # every change is a new version; the ledger names it
instructions: |                  # the facts and rules (today: the kit's system prompt)
  ...
tunables:                        # named numbers the environment reads instead of constants
  enemy_horizon_tiles: 24
  takeoff_window_tiles: [1.5, 4.0]
  tall_wall_tiles: 4
  stop_distance_tiles: 2.8
gate:
  read: 0.5
  write: 0.7
  destructive: 0.9
encoder:
  history_steps: 3
```

Rules for the surface:

- **The environment reads its tunables from the config; it never hardcodes them.** A state
  description rule that needs a number names the number. The outer loop may change any number.
- **The environment's truth is not configuration.** What the game's state is (where Mario is,
  what is ahead) is code. When the state lies (the "gap 12 tiles wide" that was Mario below the
  floor), that is a bug, and fixing it is an engineering change: the outer loop may propose it as
  a pull request with a test, and a person merges it. The outer loop does not edit environment
  code in place.
- **No scripted play.** The outer loop may add a fact or a rule the reflex is told; it may not add
  code that plays the game for the reflex. The reflex chooses every step.
- **Every version is reversible and attributed.** The ledger (below) says what changed, the
  evidence it came from, the runs that validated it, and the verdict.

## 4. What the outer loop needs from the platform

The outer harness is an ordinary HarnessRouter harness with a Skill (the method) and a set of
tools (the levers). The tools are the platform's job:

| Tool | What it does | Exists today |
|---|---|---|
| `run_inner(harness, attempts, goal)` | starts N sessions of the inner harness, one at a time, and returns their ids | responses API with `x-harness-id`, one at a time by hand |
| `trace(session)` | the full trace: state, questions, probabilities, verdicts, results, latency, usage | yes (the harness records it; the driver emits it) |
| `recording(session)` | every frame the environment showed, with timestamps | no: only the live `frame.jpg` exists; the recorder was a scratchpad script |
| `failures(session)` | the last live state before each failure, and a contact sheet around it | no: `analyze.py` and `sheets.py` in a scratchpad |
| `probe(env, script)` | scripted actions in the environment, sampled, for measurement | by hand, through the environment's own tools |
| `config(harness)` / `set_config(harness, version)` | read and write the configuration surface | partly: `system_prompt` through the harness API; tunables do not exist |
| `handoff` on the result | the state, questions and probabilities of a refused or escalated step, at the top of the result | in progress: SystemOneHarness#3, harnessrouter#227 |
| `ledger(harness)` | the calibration log: versions, evidence, verdicts | no |

Two platform properties matter as much as the tools:

- **Decision rate.** The inner loop is realtime; the outer loop cannot fix what the pace makes
  impossible. Measured: 0.38 s per decision through the runner's relay against 0.20 s direct
  (a fresh connection per call). A kept-alive upstream connection is the first platform change.
- **One game at a time per machine.** Measurements are serialized by the platform, not by
  convention.

## 5. The outer loop's method, as a Skill

The Skill is the procedure from section 1 written for a coding harness, with the two rules that
were learned the hard way stated as MUSTs. Its shape:

1. **Baseline.** Run K attempts (K = 3 locally, 5 hosted). Record the scoreboard: pass rate,
   deaths per attempt, furthest point, seconds to the flag, cost.
2. **Read.** Build the failure report from the recordings. Group the deaths by place and by the
   last live state. Pick the largest group.
3. **Measure.** Probe the environment at that place until the mechanism is a number (a distance,
   a window, a timing). No change without a measurement.
4. **Change one thing.** A fact in the instructions, a tunable, a gate threshold, or, when the
   state lied, a proposed code fix with a test. New config version, ledger entry with the evidence.
5. **Validate with the reflex.** Run K attempts again, one at a time. Keep the version if the
   scoreboard improved, revert if it did not; either way the ledger says so.
6. **Stop** when the pass rate reaches the target, the budget is spent, or three versions in a row
   were reverted (the environment's truth, the pace, or the reflex is the limit, and the report
   says which).

The Skill ships in the starter kit beside the game kit, the way kits already ship their Skills as
Agent Plugins packages.

## 6. Where the pieces go, and who builds them

| Piece | Repo | Owner |
|---|---|---|
| Config surface in the harness (`config` beside the action space; tunables read by environments; version in the trace) | SystemOneHarness | this session |
| `handoff` on the run result (#3) | SystemOneHarness | this session |
| `s1 bench`: K attempts one at a time, the scoreboard, the failure report from a recording | SystemOneHarness | this session |
| The Mario environment reading its tunables from the config; the kit ships config v1 | starter-kit | this session |
| The calibration Skill (the method above) | starter-kit | this session |
| Session recordings kept with the trace (frame archive per session) | harnessrouter | OSS session |
| Harness config versioning through the API; `set_config` for a harness from another harness | harnessrouter | OSS session |
| `handoff` passed through on the result (#227); the relay's kept-alive connection | harnessrouter | OSS session |
| The outer harness's tools as an MCP server the platform provides to any harness | harnessrouter | OSS session |
| Everything above on the hosted service; a "Calibrate" action on the kit page that launches the outer harness on the inner one; the ledger and scoreboard in the console | SaaS | SaaS session |

## 7. The demo

Super Mario on the kit page. Jev plays and loses its lives at one place. The result carries the
handoff. The person presses Calibrate. The outer harness (Claude Code on HarnessRouter) opens a
session, reads the failure report with its contact sheets, measures the spot, writes one fact
into the kit's instructions as config version 2 with the evidence in the ledger, runs three
attempts, and the scoreboard shows the pass rate rise. Both sessions are visible in the console:
the reflex's trace with a probability on every step, and the reasoning harness's turn that
changed it. That is the answer to "how does System Two work as the external loop for System One",
in a running product.

## 8. Open questions for the discussion

1. **How much of the state rendering becomes configuration?** Tunables cover the numbers. The
   "Now:" line's rules are code today; making them a rule table the outer loop can edit is more
   power and more risk (the no-scripted-play line gets thin). Proposal: numbers and facts first,
   rules stay code, revisit after the first calibration runs.
2. **Where does the outer loop's validation run?** Locally the pace is 3 decisions a second and the
   pass rate is high; hosted it is 2 and lower. The scoreboard that matters is the hosted one, so
   validation should run where the product runs, once the relay is fixed.
3. **Who merges an environment code fix proposed by the outer loop?** Proposal: always a person,
   through a pull request with the failing case as a test.
4. **Cost.** A local attempt is about $0.08 and 3 minutes; ten hosted attempts are a few dollars.
   The Skill takes a budget and stops at it.
