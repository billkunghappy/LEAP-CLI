# LEAP — LEMMA Formal Proof Harness

A multi-agent harness for proving Lean 4 theorems. An orchestrator decomposes a
theorem into a DAG of subgoals, dispatches prover / reviewer / verifier
subagents against individual nodes, and assembles a single kernel-checked proof
once every leaf closes.

The Python here is the **state layer**, not the agents: a process-safe proof
graph, the phase machine that decides what each node may do next, a Lean
verifier client, and a Mathlib search client. The agent behaviour lives in
[`agents/`](agents) as markdown prompt definitions.

---

## Requirements

- Python 3 (developed and verified on 3.13)
- `pydantic` — the only runtime dependency
- `absl-py` — **tests only.** The three binaries prefer absl for flag parsing
  but fall back to `argparse` when it is absent, so a runtime install needs
  just `pydantic`.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install pydantic          # runtime
pip install absl-py           # to run the test suite
```

- **A Lean 4 verifier service.** None is bundled. See
  [Verifier service](#verifier-service) — without one, every verification call
  raises `NotImplementedError`.

---

## Install

The three tools are invoked by bare name from the agent prompts, so put
[`bin/`](bin) on your `PATH`:

```bash
export PATH="/path/to/LEAP_CLI/bin:${PATH}"
export LEAN_VERIFIER_URL="https://your-lean-service.example.com"
```

Check it:

```bash
state_manager_bin --help
lean_verifier_tool_bin --code='theorem t : True := trivial' --target_name=t
```

The wrappers run the modules by absolute path, so they work from any working
directory and through a symlink. Set `LEAP_PYTHON` to pick a specific
interpreter (e.g. your venv) instead of `python3`.

> [!NOTE]
> The modules import each other flatly (`import dag`, `import pipeline`). That
> resolves because Python puts a script's own directory on `sys.path[0]`. It is
> deliberately not a package — the tools are consumed as subprocesses, never
> imported.

---

## The three tools

| Binary | Purpose |
| :--- | :--- |
| `state_manager_bin` | The only writer of proof state. Claims nodes, validates submissions, runs the gates, assembles the final proof. |
| `lean_verifier_tool_bin` | Submits Lean 4 to the verifier service; reports kernel acceptance and the axioms the target depends on. |
| `lean_search_tool_bin` | Semantic and signature search over Mathlib. |

Every agent turn is a single `state_manager_bin` call:

```bash
state_manager_bin --root_dir=<problem_root> --action=claim_next --json_output
state_manager_bin --root_dir=<problem_root> --action=submit \
    --node_id=<node> --code_file=nodes/<node>/proof_scaffold.lean
state_manager_bin --root_dir=<problem_root> --action=replan \
    --node_id=<node> --reason="<one or two sentences>"
```

`submit` takes `--code_file`; every other action takes `--reason`. Which
actions a node accepts is decided by the phase it is in, declared in
`pipeline.PIPELINE`.

[`docs/architecture.md`](docs/architecture.md) is the full reference: the phase
machine, the gates, every action and flag.

---

## Verifier service

`lean_verifier_tool` is a **client**. You supply the server.

Point it at yours with `LEAN_VERIFIER_URL`, `--verifier_url`, or
`LeanVerifierInput.verifier_url`. The expected HTTP contract is documented in
full in `VERIFIER_WIRE_CONTRACT` in
[`lean_verifier_tool.py`](lean_verifier_tool.py) — request shape, response
shape, and the semantics the soundness gate depends on. In brief:

```
POST {base}/api/check          (falls back to {base}/check on 404)
  {"snippets": [{"id": "check_0", "code": "..."}],
   "timeout": <sec>, "debug": false, "reuse": true}

200 {"results": [{"error": "...",
                  "response": {"messages": [{"severity": ..., "data": ...,
                                             "pos": {"line": ...}}]}}]}
```

The critical part: when `--target_name` is given, the tool appends
`#print axioms <name>` to the snippet and parses the axiom list out of the
reply. **A server that swallows `#print axioms` output silently defeats the
soundness gate**, which is the one thing standing between the harness and a
proof that leans on `sorryAx`.

Unconfigured, `run_lean_verifier` raises `NotImplementedError` rather than
returning `is_valid=False` — a missing endpoint must not look like Lean
rejecting every proof.

---

## Not implemented in this release

Two seams were deliberately left unimplemented. Both raise
`NotImplementedError` with the full contract needed to fill them in, so a
missing backend surfaces loudly instead of degrading into plausible-looking
results.

| Seam | Triggered by | What it needs |
| :--- | :--- | :--- |
| `lean_verifier_tool.get_id_token` | `LEAN_VERIFIER_AUTH` set to anything but `none` | A bearer-token provider for your verifier. The default `none` sends no `Authorization` header, which is correct for a self-hosted service or one behind an auth-terminating proxy. |
| `LeanSearchClient._grpc_search` | `use_internal=True` / `--use_internal` | A gRPC Mathlib search backend. The default HTTP path (leansearch.net) needs nothing. |

---

## Environment variables

| Variable | Default | Effect |
| :--- | :--- | :--- |
| `LEAN_VERIFIER_URL` | *(none)* | **Required.** Base URL of the Lean verifier service. |
| `LEAN_VERIFIER_AUTH` | `none` | Token provider for the verifier. Any other value raises `NotImplementedError`. |
| `LEAN_VERIFIER_MAX_ATTEMPTS` | `3` | Transport retries before a call is reported as an infra failure. |
| `LEMMA_MAX_INFRA_RETRIES` | `3` | How often `state_manager` re-tries a node whose verification never returned a verdict. |
| `LEAN_SEARCH_SUBAGENT_QUOTA` | `-1` | Searches allowed per subagent. Negative is unlimited; `0` disables search. |
| `ANTIGRAVITY_CONVERSATION_ID` | *(none)* | Identity the search quota is tracked against. Unset means no quota tracking. |
| `LEAP_PYTHON` | `python3` | Interpreter the `bin/` wrappers exec. |
| `LEAP_AGENT_CLI` | *(none)* | **Required by `safe_lemma_runner.sh`.** The agent CLI binary to drive. |
| `LEAP_TARGET_DIR` | *(inferred)* | Problem root `safe_lemma_runner.sh` watches for completion. |
| `LEMMA_LIVE_VERIFIER` | *(unset)* | Set to `1` to un-skip the live verifier contract tests. |

---

## Tests

828 tests, all offline and deterministic — the Lean verifier is stubbed:

```bash
for t in *_test.py; do python3 "$t"; done
```

`lean_verifier_tool_test.py` is the exception: it makes real network calls and
skips unless `LEMMA_LIVE_VERIFIER=1`. See [`Testing.md`](Testing.md).

---

## Running a rollout

[`safe_lemma_runner.sh`](safe_lemma_runner.sh) drives an agent CLI in a
signal-safe retry loop, intended for BYOM endpoints that go away for minutes at
a time. It releases stranded claims and resumes from the existing graph on
every restart, and stops once the orchestrator log records a verified
`[PHASE 5: COMPLETE]`:

```bash
LEAP_AGENT_CLI=/path/to/your/agent/cli \
LEAP_TARGET_DIR=/path/to/problem_root \
  ./safe_lemma_runner.sh <args passed through to the agent CLI>
```
