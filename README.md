# LEAP CLI

LEAP proves Lean 4 theorems with a team of coding agents. It runs inside the
agent CLI you already use: **Claude Code**, **Codex**, or **Google
Antigravity**.

An orchestrator agent splits a theorem into a graph of subgoals. Specialised
subagents then plan, prove, decompose, review and refute individual nodes, and
the tree is assembled into one kernel-checked proof once every leaf is closed.
This is the CLI-agent version of the system described in
[arXiv:2606.03303](https://arxiv.org/abs/2606.03303).

```
            ┌────────────────────── orchestrator ──────────────────────┐
            │  claim_next → dispatch role → wait → … → assemble → audit │
            └──────────────────────────────────────────────────────────┘
                 │                 │                  │              │
       informal prover      formal prover      decomposition    final verifier
       (blueprint.md)     (prove / split /        reviewer       (0 sorry, std
                             refute in Lean)                       axioms only)
                 │                 │                  │
                 └──────── state_manager_bin  (the only writer of proof state)
                                   │
                    lean_verifier_tool_bin   lean_search_tool_bin
                    (local Lean or kimina)   (leansearch.net / Loogle)
```

The agents never edit the proof state directly. Every turn ends with one
`state_manager_bin` call, and the state manager decides what the submission
was by running it through Lean: proved, decomposed, or disproved. A proof is
accepted only if the kernel accepts it and `#print axioms` shows nothing beyond
`propext`, `Classical.choice` and `Quot.sound`.

---

## 1. Requirements

- Python ≥ 3.10 and `pydantic` (the only runtime dependency).
- One agent CLI: [Claude Code](https://code.claude.com),
  [Codex CLI](https://developers.openai.com/codex), or
  [Antigravity](https://antigravity.google).
- **A Lean verifier.** Pick one of the two backends below.
- Optional: `graphviz` (`dot`), for SVG renderings of the proof graph.

```bash
git clone <this repo> leap-cli && cd leap-cli
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export PATH="$PWD/bin:$PATH"          # put this in your shell profile
export LEAP_PYTHON="$PWD/.venv/bin/python"
```

## 2. Set up a Lean verifier

### Option A: local Lean + Mathlib (recommended for a single machine)

```bash
# 1. Install elan (the Lean toolchain manager)
curl https://elan.lean-lang.org/elan-init.sh -sSf | sh

# 2. Create a Lake project that depends on Mathlib, and fetch the prebuilt cache
lake +leanprover-community/mathlib4:lean-toolchain new leap-mathlib math
cd leap-mathlib && lake exe cache get && lake build && cd ..

# 3. Point LEAP at it
export LEAN_PROJECT_DIR="$PWD/leap-mathlib"
```

Each check runs `lake env lean --json` on a temporary file inside that project.
Use the same Mathlib version your problem files were written against.

### Option B: kimina-lean-server (recommended for many parallel workers)

[kimina-lean-server](https://github.com/project-numina/kimina-lean-server)
keeps warm Lean REPLs with Mathlib already loaded, so checks are much faster
under concurrency.

```bash
docker run -d --name kimina -p 8000:8000 projectnumina/kimina-lean-server:2.0.0
export LEAN_VERIFIER_URL="http://localhost:8000"
# only if the server sets LEAN_SERVER_API_KEY:
export LEAN_VERIFIER_API_KEY="..."
```

If both `LEAN_VERIFIER_URL` and `LEAN_PROJECT_DIR` are set, the URL wins.
Override with `LEAN_VERIFIER_BACKEND=local|kimina`.

### Check the setup

```bash
leap_doctor
```

`leap_doctor` compiles a probe theorem and confirms that the axiom list comes
back. **If that check fails, do not run proofs**: without the axiom list, the
soundness gate cannot tell a real proof from a `sorry`.

## 3. Install the agents into a workspace

Pick a working directory for your runs and install the agents for your CLI:

```bash
python3 install.py --host claude      --dest ~/leap-work
python3 install.py --host codex       --dest ~/leap-work
python3 install.py --host antigravity --dest ~/leap-work
```

| Host | What gets written |
| :--- | :--- |
| Claude Code | `.claude/agents/*.md` (6 agents), `.claude/skills/` (2 Mathlib skills), `.claude/settings.json`. The settings allow the three LEAP binaries without a prompt each time and raise the Bash timeout to 15 min. |
| Codex | `.codex/agents/*.toml` (4 workers), `.agents/skills/`, and the orchestrator as the skill `$leap-prove`. |
| Antigravity | `.agents/agents/*.md` (6 agents, native tool names), `.agents/skills/`. |

The prompts in [`agents/`](agents) are written once in host-neutral form.
`install.py` translates the tool names and adds a short "Host notes" section
for each CLI. Re-run it after editing a prompt.

## 4. Prove something

Copy a problem file into the workspace. It should be a full Lean file: imports,
any definitions, then the theorem ending in `:= by sorry`.

```lean
import Mathlib

theorem my_problem (a b : ℝ) (ha : 0 ≤ a) (hb : 0 ≤ b) :
    (a + b) / 2 ≥ Real.sqrt (a * b) := by
  sorry
```

**Claude Code**

```bash
cd ~/leap-work
claude --agent orchestrator-lemma \
  "Prove the theorem in problems/my_problem.lean. Problem root: runs/my_problem"
```

**Codex**

```bash
cd ~/leap-work
codex "\$leap-prove Prove the theorem in problems/my_problem.lean. Problem root: runs/my_problem"
```

Codex spawns subagents only on request. The `leap-prove` skill makes that
request for every claimed node. To allow 8 parallel workers, set
`[agents] max_concurrent_threads_per_session = 8` in `~/.codex/config.toml`.

**Antigravity**

Open `~/leap-work`, select `orchestrator_lemma` as the agent, and ask it to
prove `problems/my_problem.lean` with problem root `runs/my_problem`.

### What you get

```
runs/my_problem/
  problem.lean                 working copy of your file (+ a maxHeartbeats line)
  proof_graph.json             the whole search state
  orchestrator_log.md          lifecycle checklist and event log
  nodes/<id>/                  one workspace per subgoal: context.md, scaffold,
                               blueprint, failure reports, kernel traces
  assembled/final_proof.lean   the zero-sorry proof, once the search finishes
```

A run ends in one of two ways:
- a verified `assembled/final_proof.lean`, or
- a kernel-checked disproof of the root.

There is no give-up exit. Long runs are normal.

### Long or unattended runs

[`safe_lemma_runner.sh`](safe_lemma_runner.sh) relaunches a headless CLI until
the orchestrator log records a verified `[PHASE 5: COMPLETE]`. On every restart
it releases any stranded claims and resumes from the existing graph:

```bash
LEAP_AGENT_CLI=claude LEAP_TARGET_DIR=runs/my_problem ./safe_lemma_runner.sh \
  --agent orchestrator-lemma -p "Prove problems/my_problem.lean. Problem root: runs/my_problem"
```

For Codex, use `exec` and pass the prompt positionally (`LEAP_RESUME_FLAG=`,
`LEAP_RESUME_PREFIX='$leap-prove '`). Examples are in the script header.

Run it from the workspace directory. `LEAP_TARGET_DIR` is relative to it.

### Smoke test

Before a real run, install the agents and run the `leap_smoke_test` agent
(`claude --agent leap-smoke-test "go"`, or `$leap-smoke-test` in Codex). It
checks that the binaries are on PATH and that subagent dispatch works.

---

## The tools

| Binary | Purpose |
| :--- | :--- |
| `state_manager_bin` | The only writer of proof state. Claims nodes, validates submissions, runs the soundness gates, assembles the final proof. |
| `lean_verifier_tool_bin` | Compiles Lean 4 with the configured backend and reports kernel acceptance and the target's axioms. |
| `lean_search_tool_bin` | Mathlib search: [leansearch.net](https://leansearch.net) (natural language), falling back to [Loogle](https://loogle.lean-lang.org) if it is unavailable. |
| `leap_doctor` | Setup check. |

```bash
state_manager_bin --root_dir=runs/p --action=inspect
lean_verifier_tool_bin --code_file=foo.lean --target_name=foo --json_output
lean_search_tool_bin --query="AM-GM inequality for two reals"
lean_search_tool_bin --query="Real.sqrt, Irrational" --backend=loogle
```

[`docs/architecture.md`](docs/architecture.md) is the full reference. It
covers the per-node phase machine, every action and flag, the workspace
layout, and the concurrency model.

## Environment variables

| Variable | Default | Effect |
| :--- | :--- | :--- |
| `LEAN_PROJECT_DIR` | — | Lake project with Mathlib; selects the **local** verifier. |
| `LEAN_VERIFIER_URL` | — | kimina-lean-server base URL; selects the **kimina** verifier. |
| `LEAN_VERIFIER_API_KEY` | — | Sent as `Authorization: Bearer …` to kimina. |
| `LEAN_VERIFIER_BACKEND` | inferred | Force `local` or `kimina`. |
| `LEAN_VERIFIER_MAX_ATTEMPTS` | `3` | Transport retries (kimina) before reporting an infra failure. |
| `LEMMA_MAX_INFRA_RETRIES` | `3` | How often `state_manager` retries a node whose verification never returned a verdict. |
| `LEAN_SEARCH_ENDPOINT` | `https://leansearch.net/search` | Override the leansearch endpoint. |
| `LOOGLE_ENDPOINT` | `https://loogle.lean-lang.org/json` | Override the Loogle endpoint. |
| `LEAN_SEARCH_SUBAGENT_QUOTA` | `-1` | Searches per agent id. Negative means unlimited; `0` disables search. |
| `LEAP_AGENT_ID` | — | Identity the search quota is charged to. Antigravity's conversation id is used automatically. |
| `LEAP_PYTHON` | `python3` | Interpreter the `bin/` wrappers run. |

## Tests

```bash
pip install -r requirements-test.txt
for t in *_test.py; do python3 "$t" || break; done
```

The suite has about 870 tests. All are offline: Lean and the search services
are stubbed. To run the live checks against your verifier, see
[`Testing.md`](Testing.md).

## Notes and limitations

- **Cost.** A hard problem can mean hundreds of subagent runs. Watch your
  usage, and start with something small.
- **Throughput.** The local backend starts a fresh `lean` process for every
  check, and loading Mathlib takes several seconds each time. Use kimina for
  real throughput.
- **leansearch.net availability.** It is a free community service and is
  sometimes down. Search falls back to Loogle automatically, and agents can
  always probe names with `#check`.
- **Platform.** The state layer uses POSIX `fcntl` locks, so it runs on Linux
  and macOS. On Windows, use WSL.

## License

Apache 2.0. See [LICENSE](LICENSE).
