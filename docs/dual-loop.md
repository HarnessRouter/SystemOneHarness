# The dual loop: a System Two harness that drives a System One harness's configuration

Status: design for discussion (2026-09-21). The inner loop exists; the rest does not.

The design is generic: it holds for any reflex model, any environment with a finite action space,
and any reasoning harness on the platform. One instance (Super Mario, calibrated by hand on
2026-09-20) is described at the end as the first test of it, and nothing in the architecture is
shaped for that instance. Where the instance needs something specific, it is a declaration or a
tool that the environment ships, never a structure of the loop.

## 1. First principles

**A reflex is a function from a rendered state to one declared action.** It is fast because it
does not reason; it selects. Its quality is bounded by three things it does not control: what it
is shown, what it is told, and what it is allowed to do. Those three, plus how often it is asked,
are its configuration.

**Improving a reflex without training it means improving its configuration.** That is an
empirical loop: run, observe the failures, measure the mechanism behind the largest one, change
one thing, run again, keep what helped, write down why. A person can run it. A reasoning model
can run it. The loop is the same.

**A reasoning harness is the natural place for that loop**, because the loop is what reasoning
harnesses already do (read evidence, form a hypothesis, make a change, test it), and because the
platform already runs harnesses, keeps their sessions and files, and lets one harness call another.

So: the inner loop is a System One harness; the outer loop is a System Two harness with a method;
the object between them is a versioned configuration; the evidence between them is the inner
loop's runs. Nothing else is needed.

## 2. The objects

| Object | Definition | Who owns it |
|---|---|---|
| **Environment** | A world with a finite action space: it renders a state, declares actions, executes them, and reports observations with fields. It also declares its **objective** (below). | the kit |
| **Reflex** | The inner loop: observe, encode, decide (one model call), gate, execute, record. | System One Harness |
| **Configuration** | Everything that shapes the reflex's decisions except the model and the world: instructions, rendering tunables, action space, gate, pace. A versioned package. | the kit, versioned by the outer loop |
| **Run** | One session of the reflex: the trace (state, questions, probabilities, verdicts, results, latency, usage) plus the artifacts the environment wrote (its observation archive). | the platform (sessions and files) |
| **Objective** | What the environment declares as success and failure, and the fields that locate a failure: a pass predicate, a failure signal, ordered metrics, and locus fields. | the kit |
| **Probe** | A run of the environment driven by a script instead of the model, for measurement. | System One Harness (a scripted provider) |
| **Calibration** | The outer loop's task: a target on the objective, a budget, a run count per verdict. | System Two Harness, with a Skill |
| **Ledger** | The append-only record of configuration versions: the change, the evidence, the validating runs, the verdict. | the kit package |

### The objective, declared by the environment

The outer loop must not know what a "death" or a "level" is. The environment declares, beside its
actions, what to measure:

```yaml
objective:
  pass: cleared == true              # a predicate over the final observation's fields
  failure: lives decreased           # what counts as a setback, as a field change or a terminal reason
  metrics:                           # ordered: the first decides a verdict, the rest break ties
    - {field: cleared, better: true}
    - {field: failures, better: lower}
    - {field: progress, better: higher}
    - {field: elapsed, better: lower}
  locus: [progress]                  # the fields that say where a failure happened, for grouping
  evidence: observations/            # where the environment archives what it showed, if it does
```

With this, the outer loop groups failures by locus, ranks them by count, reads the evidence at the
largest group, and judges a version by the metrics in order. The same Skill calibrates a game, a
form-filling agent, a ticket router or a robot arm; only the declaration differs.

### The configuration, versioned as a package

The reflex's configuration is one file set inside the environment's package (a UHP plugin
package), so a version of the configuration is a version of the package, and the platform's
existing launch mechanism (a harness runs the package captured at launch) is the deployment:

```yaml
version: 12
instructions: |            # what the reflex is told
  ...
tunables:                  # named numbers the environment's rendering reads; no constants in code
  horizon: 24
  ...
gate: {read: 0.5, write: 0.7, destructive: 0.9}
encoder: {history_steps: 3}
```

Three boundaries hold for every environment:

- **The environment's truth is not configuration.** What the world is (its state) is code. When
  the rendering lies, that is a defect; the outer loop may propose the fix as a pull request with
  the failing case as a test, and a person merges it.
- **No scripted behaviour in configuration.** The outer loop may add a fact, a rule, a number or
  a threshold the reflex is told or shown; it may not add anything that chooses for the reflex.
- **Every version is reversible and attributed** through the ledger.

## 3. The outer loop's method, as a Skill

The Skill is environment-agnostic. It reads the objective declaration and the configuration
schema from the package and does the following, with the three MUSTs learned on 2026-09-20:

1. **Baseline.** K runs, one at a time (MUST: never two on one machine; shared machines drop
   frames and fake regressions). Record the metrics.
2. **Read.** Group the failures by locus; take the largest group; read the evidence there (the
   trace around the failure, and the archived observations if the environment kept them).
3. **Measure.** Probe the environment at that locus until the mechanism is a number. MUST: no
   change without a measurement.
4. **Change one thing.** A fact, a tunable, a threshold, or a proposed code fix. New package
   version; ledger entry with the evidence. MUST: one change per version.
5. **Validate with the reflex.** K runs again. Keep the version if the metrics improved in order,
   revert if not. MUST: the verdict comes from the reflex model's own runs, never from a stand-in
   (a deterministic follower of the rendered advice went to zero failures while the model went
   from three passes in three to none in six).
6. **Stop** at the target, at the budget, or after three reverted versions in a row, and say
   which limit was reached: the world's truth, the pace, or the reflex.

## 4. How it fits the platform (UHP), with no protocol change

| Need | UHP component | Fit |
|---|---|---|
| Start inner runs, one at a time | `/v1/responses` with `metadata.harness_id`; sessions; cancel | as is; the outer harness serializes by awaiting; it authenticates with an org API key, never a provider key |
| The structured trace | session files | the reflex writes `trace.json` into the session workspace; a file name convention, not a schema |
| The observation archive | session files and `files/archive` | the environment writes it into the workspace under the path its objective declares |
| The failure report | Plugins: Skills | scripts inside the outer loop's Skill, reading trace and archive |
| Probes | `metadata` on the request (the additive extension point) | the reflex takes a scripted provider named in metadata; a probe is an ordinary response |
| Configuration versions | Plugins: a package installed as one unit; kit launch captures the package | a version of the package is a version of the configuration; publish and relaunch is `set_config` |
| The handoff of a refused or escalated step | ResponseStatus | today `incomplete` with a reason and `handoff.json` in the workspace (SystemOneHarness#3, harnessrouter#227); properly the roadmap's waiting state (#25), of which this is the first use |
| The ledger | the package | `ledger.jsonl` beside the configuration, versioned with it; the console renders it |
| Decision rate; one run per machine | the runner | a kept-alive upstream connection in the relay (measured 0.38 s a decision against 0.20 s direct); serialization by the outer loop |

The three mappings that make it fit: the workspace is the artifact channel; the package version is
the configuration version; the responses API is how one harness drives another. The only
protocol-level growth is the waiting state, already planned.

Storage stays in the package and the workspace. No spreadsheet or external store is introduced:
the ledger is one small table whose readers are an agent (which needs diff and revert) and a
person (who needs to read it); git gives the first, the console gives the second. A dashboard
across environments can be an export later, as a view, not as the store.

## 5. Work, by repository

| Piece | Repo | Owner |
|---|---|---|
| Configuration package format; environments read tunables from it; the version in the trace; `trace.json` written to the workspace | SystemOneHarness | this session |
| The objective declaration read by the harness and exposed in the result's fields | SystemOneHarness | this session |
| Scripted provider for probes, selected by request metadata | SystemOneHarness | this session |
| `handoff` on the run result (#3) | SystemOneHarness | this session |
| The calibration Skill, environment-agnostic | starter-kit | this session |
| The first instance: the Mario package ships config v1, its objective, its observation archive | starter-kit | this session |
| Workspace retention and size for observation archives; `handoff` on the result event (#227); the relay's kept-alive connection | harnessrouter | OSS session |
| Package publish and kit relaunch callable by a harness with an org key (the `set_config` path) | harnessrouter | OSS session |
| Everything above hosted; a Calibrate action on a kit page that starts the outer harness on the inner one; the ledger and metrics rendered in the console | SaaS | SaaS session |

## 6. Open questions

1. Rendering rules as configuration: numbers and facts first; rules stay code until the first
   calibration runs show whether the boundary holds.
2. Where validation runs: where the product runs, once the relay is fixed; the metrics that matter
   are the hosted ones.
3. Merging code fixes proposed by the outer loop: always a person, through a pull request with
   the failing case as a test.
4. Budgets: the Skill takes one and stops at it.

## Appendix: the first instance, Super Mario

Everything the instance adds is a declaration or a tool inside its package:

- **Objective:** `pass: cleared`, `failure: lives decreased`, metrics `cleared, deaths lower,
  level_x higher, elapsed lower`, locus `level_x`, evidence `observations/` (the frames the page
  shows, about eighteen a second, archived per run).
- **Tunables (config v1):** the enemy horizon, the take-off window, the tall-wall height, the stop
  distance, the gate thresholds, the history depth.
- **Evidence tool:** a renderer that cuts the frame archive into a contact sheet around each
  failure (today `sheets.py` in a scratchpad; ships in the package).
- **What the manual loop found**, as the first ledger entries: the pipe cleared only at full
  speed from a measured take-off window; the ground read from the reflex's level, not its feet; a
  held jump going again on landing; the nearest thing ahead deciding the advice. And the two
  failures of method that the MUSTs above encode.
