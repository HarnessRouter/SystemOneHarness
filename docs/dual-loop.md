# The dual loop: a harness that calibrates another harness's configuration

Status: design for discussion (2026-09-21, third pass). Nothing here is built except the inner
loops themselves.

The design is generic on both sides. The **outer loop** is a reasoning harness running a
calibration method. The **inner loop** is any harness on the platform: a System One reflex (one
model call per step over a declared action space) or a System Two agent (a coding or reasoning
harness with tools and skills). The same objects, the same method and the same platform surface
serve both; what differs per inner harness is a declaration of its configuration schema and of its
objective, both shipped as packages. One instance of each kind is described at the end.

## 1. First principles

**Every harness is a loop from what the model is shown to what the model does, shaped by a
configuration.** A reflex selects one declared action per step from a rendered state. An agent
plans, calls tools and writes files across many steps. In both, the model's behaviour is bounded
by four channels it does not control. The channels are not kinds of artifact; they are the four
ways a configuration reaches the model's loop, and one artifact usually reaches it through more
than one:

| Channel | What it is | System One | System Two |
|---|---|---|---|
| **Told** | authored text that enters the model's input before the run: instructions, the descriptions of every action or tool, skill text, plugin instructions, memory notes | the same list, through the same UHP abstractions: the system prompt is the instructions; a plugin's MCP tools are the actions, each tool's description its action's description; a plugin's instructions and its skills' text are compiled into the instructions once per version, since a reflex cannot open a file mid-step; memory files the same way | the system prompt; every tool's and skill's description and text; each plugin's instructions; memory files, read lazily at run time |
| **Shown** | content that enters the input at run time, and the policy that renders it: what is included, how it is described, how much history | the rendered state and its tunables; the history depth | task input, tool results, file contents, retrieved memory; the context policy that admits and trims them |
| **Allowed** | what the model may invoke, and the checks on invoking it | the plugins' tools as the declared actions, disabled tools removed; the gate thresholds by risk | the tool set, plugins' tools, permissions, disabled tools |
| **Pace** | how often and how much | the decision rate | the step, time and cost budgets |

So a plugin is not "told" or "allowed": it is both, its instructions and descriptions in the first
channel and its tools in the third. A skill is told (its text) and sometimes shown (the files it
brings in). A tool is told (its description) and allowed (its availability). The outer loop edits
artifacts; the channels are how it reasons about what an edit changes for the model.

The two kinds of harness consume the same UHP artifacts (system prompt, plugins with tools and
skills, memory); they differ only in when. An agent reads a skill when it decides to; a reflex
gets one model call per step and no file access inside it, so the harness compiles what the
plugins say into the instructions ahead of the run, bounded by the context budget, and recompiles
on a new version. Today the System One base declares no built-in tools and takes no skills; the
first harness change in section 5 is to take skills and plugin instructions this way, so that a
kit's facts can ship as a skill package like every other kit's.

Those four are the configuration. The model's weights and the world's truth are not.

**Improving a harness without training its model means improving its configuration**, and that
is one empirical loop for both kinds: run it, read the failures, measure the mechanism behind the
largest group, change one thing, run again, keep what helped, write down why. A person ran that
loop by hand on 2026-09-20 to take a reflex from stuck to passing, and the same loop is what a
person does when they tune a coding agent's prompt, skills and tools against a test suite.

**A reasoning harness is the natural place to run the loop**, because reading evidence, forming a
hypothesis, making a change and testing it is what reasoning harnesses already do, and because the
platform already runs harnesses, keeps their sessions and files, and lets one harness call another.
The outer loop is therefore itself a harness with a configuration, which means it can be calibrated
by the same method in turn. The design is recursive by construction; in practice, depth two.

## 2. The objects

| Object | Definition | Who owns it |
|---|---|---|
| **Inner harness** | Any harness on the platform: a reflex over an environment, or an agent over a task. | System One Harness, or any UHP harness |
| **Environment** | What the inner harness acts on. For a reflex, a world with a finite action space. For an agent, a **task suite**: tasks with inputs and verifiers. Either one declares the **objective**. | the kit or the suite package |
| **Configuration** | The four columns above, as one versioned object: the platform's harness settings plus the package's config file. | the kit, versioned by the outer loop |
| **Run** | One session of the inner harness: its trace (the platform's turns and events; for a reflex, also state, questions, probabilities and verdicts) plus the artifacts it or its environment wrote. | the platform (sessions and files) |
| **Objective** | The environment's declaration of success, failure, ordered metrics and locus. | the environment |
| **Probe** | A run made for measurement rather than for the objective: for a reflex, a scripted action sequence in its environment; for an agent, a minimal task with a known answer. | the outer loop, through the inner harness |
| **Calibration** | The outer loop's task: a target on the objective, a budget, a run count per verdict. | the outer harness, with the Skill |
| **Ledger** | The append-only record of configuration versions: the change, the evidence, the validating runs, the verdict. | the package |

### The objective, declared by the environment

The outer loop knows nothing about lives, levels, tests or tickets. The environment declares what
to measure and where to look:

```yaml
objective:
  pass: <predicate over the final result's fields>
  failure: <a field change or a terminal reason that counts as a setback>
  metrics:                       # ordered: the first decides a verdict, the rest break ties
    - {field: ..., better: ...}
  locus: [<fields that say where a failure happened, for grouping>]
  evidence: <where the environment archives what the model saw, if it does>
```

For a reflex the fields come from the environment's observations (progress, lives, cleared). For an
agent they come from the task suite's verifier (which test, which file, which step failed; cost;
wall time). The Skill reads only the declaration.

### The configuration, versioned as a package

The configuration is one file set inside the inner harness's package (a UHP plugin package) plus
the platform's harness settings, snapshotted together as a version:

```yaml
version: 12
told:      { instructions: ..., skills: [...] }
shown:     { tunables: {...}, context: {...} }          # numbers the rendering reads; what enters the context
allowed:   { actions: [...], gate: {...}, tools: [...], disabled: [...] }
pace:      { max_step: ..., timeout_seconds: ..., cost_cap: ... }
```

A reflex uses `tunables`, `actions` and `gate`; an agent uses `skills`, `tools`, `disabled` and the
budgets; both use `instructions`. The harness declares which keys it reads (its configuration
schema), so the outer loop edits only what exists. Three boundaries hold for every harness:

- **The world's truth is not configuration.** An environment's state and a suite's verifiers are
  code. When they are wrong, that is a defect; the outer loop may propose the fix as a pull request
  with the failing case as a test, and a person merges it.
- **Nothing in configuration chooses for the model.** A fact, a rule, a number, a threshold, a
  tool or a budget may be added or changed; a script that acts in the model's place may not.
- **Every version is reversible and attributed** through the ledger.

## 3. The outer loop's method, as a Skill

Environment-agnostic and harness-agnostic. It reads the objective and the configuration schema
from the package and runs, with the three MUSTs learned on 2026-09-20:

1. **Baseline.** K runs, one at a time (MUST: never two on one machine; shared machines drop
   frames and fake regressions). Record the metrics.
2. **Read.** Group the failures by locus; take the largest group; read the evidence there. The
   environment may ship an evidence renderer for its kind (a contact sheet of frames around a
   failure; a diff and the failing test's output around a step).
3. **Measure.** Probe until the mechanism is a number or a reproducible case. MUST: no change
   without a measurement.
4. **Change one thing.** A fact, a tunable, a threshold, a skill line, a tool, a budget, or a
   proposed code fix. New version; ledger entry with the evidence. MUST: one change per version.
5. **Validate with the inner harness's own model.** K runs again. Keep the version if the metrics
   improved in order, revert if not. MUST: never a stand-in for the model (a deterministic
   follower of the rendered advice went to zero failures while the model went from three passes
   in three to none in six).
6. **Stop** at the target, at the budget, or after three reverted versions in a row, and say
   which limit was reached: the world's truth, the pace, or the model.

## 4. How it fits the platform (UHP), with no protocol change

| Need | UHP component | Fit |
|---|---|---|
| Start inner runs, one at a time | `/v1/responses` with `metadata.harness_id`; sessions; cancel | as is; the outer harness serializes by awaiting; it authenticates with an org API key, never a provider key |
| The inner run's trace | sessions, turns, streaming events; session files | an agent's trace is the turns and events already; a reflex adds `trace.json` to the workspace (a file name convention, not a schema) |
| The evidence archive | session files and `files/archive` | the environment writes it into the workspace under the path its objective declares |
| The failure report and renderers | Plugins: Skills | scripts inside the outer loop's Skill and the environment's package |
| Probes | `metadata` on the request (the additive extension point); ordinary tasks | a reflex takes a scripted provider named in metadata; an agent takes a minimal task |
| Configuration versions | Harnesses: settings through the harness API; Plugins: a package installed as one unit; kit launch captures the package | the package version plus a snapshot of the harness settings is the configuration version; publish and relaunch, or `PUT` the settings, is `set_config`. Gap: harness settings carry no version id today; the ledger holds the snapshot until the platform does |
| The objective for an agent | Plugins: a package | a task suite is a package of tasks and verifiers; the existing support-matrix scenarios are one |
| The handoff of a refused, escalated or blocked step | ResponseStatus | today `incomplete` with a reason and a `handoff.json` in the workspace (SystemOneHarness#3, harnessrouter#227); properly the roadmap's waiting state (#25), which an agent's pending ask and a reflex's refusal both are |
| The ledger | the package | `ledger.jsonl` beside the configuration, versioned with it; the console renders it |
| Decision rate; one run per machine | the runner | a kept-alive upstream connection in the relay (measured 0.38 s a decision against 0.20 s direct); serialization by the outer loop |

Three mappings make it fit: the workspace is the artifact channel; the package version (with the
settings snapshot) is the configuration version; the responses API is how one harness drives
another. The only protocol-level growth is the waiting state, already planned, which serves both
kinds of inner harness.

Storage stays in the package and the workspace; no spreadsheet or external store is introduced.
The ledger is one small table read by an agent (which needs diff and revert, which git gives) and
by a person (who needs to read it, which the console gives). A dashboard across harnesses can be
an export later, as a view, not as the store.

## 5. Work, by repository

| Piece | Repo | Owner |
|---|---|---|
| The configuration file format and schema declaration; the reflex reads tunables, actions and gate from it; `trace.json` to the workspace; skills and plugin instructions compiled into the instructions per version | SystemOneHarness | this session |
| The objective declaration read and exposed in the result's fields; the scripted provider for probes; `handoff` (#3) | SystemOneHarness | this session |
| The calibration Skill, agnostic of harness kind and environment | starter-kit | this session |
| Instance one: the Mario package ships config v1, its objective, its evidence renderer | starter-kit | this session |
| Instance two: a task suite package for an agent harness, from the existing support-matrix scenarios, with an objective declaration | harnessrouter | OSS session |
| Harness settings snapshot with a version id; package publish and kit relaunch callable by a harness with an org key; workspace retention for archives; `handoff` on the result event (#227); the relay's kept-alive connection | harnessrouter | OSS session |
| Everything above hosted; a Calibrate action on any harness's page that starts the outer harness on it; the ledger and metrics rendered in the console | SaaS | SaaS session |

## 6. Open questions

1. Rendering rules as configuration (System One): numbers and facts first; rules stay code until
   the first calibration runs show whether the boundary holds.
2. What the outer loop may change in an agent's configuration: prompt and skills text, tool set,
   budgets, yes; the model, probably not (that is a different harness); permissions, never.
3. Where validation runs: where the product runs; the metrics that matter are the hosted ones.
4. Merging code fixes proposed by the outer loop: always a person, through a pull request with the
   failing case as a test.
5. Budgets: the Skill takes one and stops at it.

## Appendix: the two first instances

**A reflex: Super Mario.** Objective: `pass: cleared`, `failure: lives decreased`, metrics
`cleared, deaths lower, level_x higher, elapsed lower`, locus `level_x`, evidence the archived
frames. Config v1 tunables: the enemy horizon, the take-off window, the tall-wall height, the stop
distance, the gate thresholds, the history depth. Evidence renderer: the contact sheet around each
failure. First ledger entries: what the manual loop found on 2026-09-20 (the pipe cleared only at
full speed from a measured take-off window; the ground read from the reflex's level, not its feet;
a held jump going again on landing; the nearest thing ahead deciding the advice), and the two
failures of method that the MUSTs encode.

**An agent: a coding harness against a task suite.** Objective: `pass: verifier ok`, failure a
failed check, an error, a refusal or a budget cut; metrics `pass, cost lower, wall time lower`;
locus the failing check and the step; evidence the diff and the check's output. Configuration: the
system prompt, the skill texts, the tool set, the budgets. Probe: one scenario with a known answer.
The existing support-matrix scenarios are the first suite; the outer loop's first job on it is the
one a person does today by hand after a matrix run: read the failures, change one line of a skill
or the prompt, run the matrix again.
