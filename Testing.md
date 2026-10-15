# Testing

Every test file runs standalone with `python3 <file>`. The suite is offline and
deterministic: the Lean verifier and both search services are stubbed.

```bash
pip install -r requirements.txt -r requirements-test.txt
for t in *_test.py; do python3 "$t" || break; done
```

| File | Covers |
| :--- | :--- |
| `dag_test.py` | Proof-graph semantics: status propagation, active AND-node selection, assembly order, cycles. |
| `lean_code_parser_test.py` | Lean text handling: binder/colon parsing, nested comments, signature gate. One `expectedFailure` documents that `(n: Nat)` is not accepted for `(n : Nat)`. |
| `pipeline_test.py` | The phase table (`pipeline.PIPELINE`) and its import-time validation. |
| `actions_test.py` | Declared actions (`planned`, `refute`, `failed`, `replan`, `approved`): phase checks, reason checks, orphan handling. |
| `review_trigger_test.py` | The `subtree_stalled` predicate that pulls a stalled branch back into review. |
| `claim_terminal_test.py` | The message `claim_next` gives when nothing is claimable. |
| `graph_init_guard_test.py` | `init_graph` admission checks (preamble, root gate, root compiles). |
| `state_manager_test.py` | Claims, submissions, the axiom/soundness gate, optimistic commits, atomic writes, `--backup_dir`. |
| `state_machine_test.py` | Whole-node lifecycles through the phase machine. |
| `lean_verifier_tool_test.py` | Verifier backends (kimina transport + Bearer auth, local `lake env lean --json` mapping), infra-failure vs. rejection, axiom read-back. |
| `lean_search_tool_test.py` | leansearch.net request shape, Loogle fallback, per-agent quota. |
| `install_test.py` | `install.py` renders a complete, parseable layout for every host. |

## Live verifier tests

`lean_verifier_tool_test.py` also has 8 tests that compile real Lean (e.g. that
`sorry` compiles but shows up as `sorryAx`, and that wrapped axiom lists are
parsed). They use whatever verifier you have configured and are skipped unless
`LEMMA_LIVE_VERIFIER=1`:

```bash
LEAN_PROJECT_DIR=~/lean/leap-mathlib LEMMA_LIVE_VERIFIER=1 python3 lean_verifier_tool_test.py
# or
LEAN_VERIFIER_URL=http://localhost:8000 LEMMA_LIVE_VERIFIER=1 python3 lean_verifier_tool_test.py
```

Run them once after setting up a verifier: they are what proves the soundness
gate works against *your* Lean.
