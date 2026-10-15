# LEMMA test suite

96 tests across five files, all runnable standalone with `python3 <file>`.

```bash
# Offline: 96 tests, no network. 17 live tests skip.
python3 dag_test.py && python3 lean_code_parser_test.py && \
python3 state_manager_test.py && python3 lean_verifier_tool_test.py && \
python3 integration_test.py

# Including the live Lean service (~21s of network):
LEMMA_LIVE_VERIFIER=1 python3 lean_verifier_tool_test.py
LEMMA_LIVE_VERIFIER=1 python3 integration_test.py
```

| File | Tests | Needs Lean | Runtime |
|---|---|---|---|
| `dag_test.py` | 22 | no | <0.01s |
| `lean_code_parser_test.py` | 28 | no | <0.01s |
| `state_manager_test.py` | 26 | no (verifier stubbed) | ~1s |
| `lean_verifier_tool_test.py` | 10 | 8 of them | ~7s live |
| `integration_test.py` | 10 | 7 of them | ~14s live |

`state_manager_test.py` stubs `run_lean_verifier` with `mock.patch.object`, so the
whole commit path is exercised deterministically and offline. Only
`lean_verifier_tool_test.py` and `integration_test.py` touch the network, and both
skip unless `LEMMA_LIVE_VERIFIER=1`.

---

## What each test pins, and why

### `dag_test.py` — graph semantics (22)

**StatusPropagationTest** — bare node is `FORMAL_STATEMENT`; a directly proved node
is `FORMAL_PROVED`; a parent resolves once every child is proved and stays
`FORMAL_SKETCHED` while one is not; a failed child retires its AND-node.

- `test_and_node_with_no_dependencies_does_not_prove_parent` — a zero-dependency
  AND-node is vacuously satisfied. It used to mark its parent `FORMAL_PROVED`
  with nothing supporting it, which is how dropped subgoal attribution could
  have recorded an unproved theorem as done.
- `test_empty_decomposition_is_refused` — the same invariant at the writing end:
  `add_proposed_subgoals` raises rather than creating one.

**ActiveAndNodeConsistencyTest** (6) — the divergence that made status and
assembly disagree. Fixture: `and_nodes = [live(→c), failed]`, so "last non-failed"
and `and_nodes[-1]` select different nodes. Pins that status reports proved, that
assembly does **not** emit `sorry` for the same node, that the proved child stays
reachable, that dependencies follow the live branch, and both `active_and_node`
edge cases.

**StatusPurityTest** (3) — `compute_statuses()` leaves the graph byte-identical
(compared via `model_dump_json`), `propagate_failures()` retires the AND-node and
writes the reason, and both report identical statuses.

**AssemblyTest / DynamicHeaderTest / CycleDetectionTest** — topological ordering,
unproved leaf becomes `sorry`, cycles raise, header order preserved with exact
duplicates collapsed, self-dependency rejected, transitive `has_path`.

### `lean_code_parser_test.py` — Lean text handling (28)

**DepthAwareScanningTest / ToLeanTest** (11) — the dropped-colon bug. A
proposition that merely *starts* with a bracket was misread as a binder list,
emitting `theorem h (2:Nat) + 2 = 4`, which Lean rejects with
`unexpected token; expected '_' or identifier`. Covers `(2:Nat) + 2 = 4`,
`(a + b)^2 = ...`, real binders, implicit/instance binders, a bare proposition,
an already-colon-led statement, and a full declaration passing through.

`test_quantified_proposition_gets_colon` is the one that constrains the fix:
`∀ x : Nat, x = x` *has* a top-level colon but is not a binder list, so a naive
depth-0-colon rule would break it.

**CommentStrippingTest** (7) — nested block comments (Lean's nest, so a non-greedy
regex stops one `-/` early and leaks the comment body into live source), triple
nesting, `--` and `/-` inside string literals, doc comments, and index
preservation in `mask_lean_comments`.

**SplitSignatureAndProofTest** (3) — `:=` inside binders must not be mistaken for
the proof separator: `(n : Nat := 0)`, `let x := 3`, `[inst : Inhabited a := ⟨default⟩]`.
This is what `init_graph` uses, and truncating here silently proves the wrong theorem.

**SubgoalToAxiomTest / ExpectedSignatureTest** (7) — generated axioms are
syntactically valid, non-subgoal declarations are dropped, and the signature gate
accepts extra whitespace, newlines, and leading comments.

> One `@unittest.expectedFailure`: `test_tolerates_omitted_space_before_colon`.
> `(n: Nat)` is identical to `(n : Nat)` in Lean but is rejected, because the
> gate joins whitespace-split tokens with `\s+` — it accepts *more* whitespace
> than expected, never less. A usability item, not a soundness one. Delete the
> decorator if you relax the gate.

### `state_manager_test.py` — manager behavior, Lean stubbed (26)

**ClaimLifecycleTest** (5) — a claim marks `processing` and blocks a second
claim; a claim **never expires** (the test back-dates `claim_timestamp` to 0 and
asserts the node still is not reissued); `release` makes it claimable again;
releasing an unknown node reports failure.

- `test_redecomposition_claim_sets_require_decompose` — a node whose AND-nodes
  have all failed is claimed as `decompose`, but `get_node_context` derives the
  role from `require_decompose`. Without the flag the worker was handed a
  *prove* scaffold for a mandated re-decomposition.

**GetContextTest** (2) — `get_context` leaves the on-disk scaffold byte-identical
and returns the agent's content rather than a fresh baseline; a re-claim archives
the previous work to `*_old_<ms>.*` instead of destroying it.

**AxiomGateTest** (6) — the soundness gate across all outcomes: standard axioms
pass, `[]` (genuinely none) passes, `sorryAx` rejected, `native_decide`'s
`Lean.ofReduceBool` / `Lean.trustCompiler` rejected, `None` (unparseable) **fails
closed**, and a per-problem `allowed_axioms` override is honoured.

**SyntacticGateTest** (6) — `sorry` in the definitions block rejected on *both*
paths; `axiom` in definitions rejected on the decompose path; a decomposition
with no subgoals rejected; an active collision is a rejection and **not** an
early success.

- `test_failed_strategy_cannot_be_resubmitted_with_a_padding_lemma` —
  `dependency_ids` holds only kernel-attributed subgoals, so comparing against it
  let an agent re-propose a known-failed strategy by adding one unreferenced
  lemma. The guard compares the full declared set.
- `test_validation_never_mutates_the_prior_branch` — a rejected retry must leave
  the previous decomposition viable. This one is the regression that matters
  most: validation used to retire the prior AND-node before verification ran, so
  a submission rejected for a typo permanently destroyed a working branch.

**CommitTest** (3) — a stale `revision` is refused; a successful commit bumps
`revision`, clears `decomposition_failed`/`processing`; headers are append-only.

**AtomicityTest** (3) — an exception mid-transaction leaves the state file intact
with no partial write; no `.proof_graph_*` temp files survive; four threads × 20
mutations lose no updates.

**SummaryTest** (1) — `BROKEN` nodes appear in `inspect` totals.

### `lean_verifier_tool_test.py` — live Lean contract (10, 8 live)

**SorryIsWarningTest** (2) — `theorem probe_false : 2 + 2 = 5 := by sorry` compiles
`is_valid=True`. A **false** theorem. This is the load-bearing fact behind the
whole axiom gate; if it ever fails, Lean changed `sorry` to an error.

**AxiomExtractionTest** (4) — a constructive proof reports `[]` and not `None`
(they must stay distinct or "unparseable" reads as "no axioms"); a classical proof
reports exactly the standard three; `native_decide` falls outside them.

- `test_long_axiom_list_wraps_and_is_still_parsed` — 12 long-named axioms force
  Lean's pretty-printer to wrap. This is the regression that made a wrapped list
  read as **zero** axioms, silently disabling the `sorryAx` check on most real
  decompositions.

**CompilerErrorTest** (2) — a syntax error and an undeclared `target_name` both
report invalid.

**NoNetworkTest** (2) — always runs: `used_axioms` defaults to `None`, and empty
code is rejected without a request.

### `integration_test.py` — end to end (10, 7 live)

**LifecycleTest** (1 live) — a whole problem: init → claim(prove) → surrender →
claim(**decompose**, correct scaffold filename) → `record_decomposition` with two
declared subgoals but only one referenced → assert only the referenced one became
a child, the other filed as `unused_subgoal_signatures`, header appended with
provenance → claim the child → prove it → `formal_proved == 2` → **assembled
output contains no `sorry`** and orders the lemma before the theorem.

**AxiomGateIntegrationTest** (6 live) — the gate through `_verify_before_commit`
against real Lean: `native_decide` rejected, `sorry` rejected, ordinary and
classical proofs accepted, a decomposition whose axiom list wraps accepted with
all three subgoals attributed, and `native_decide` smuggled into decomposition
glue rejected while its legitimate subgoal axiom is still allowed.

**ConcurrencyTest** (3, always runs) — real OS processes, the only cover for the
`fcntl` locking and atomic-replace path that threads cannot exercise:
8 × 12 mutations lose no updates and create every node; no temp files survive;
and 8 processes racing for 6 claimable nodes never claim one twice.

---

## Known gaps

- **Timing under load is untested.** The concurrency tests mutate in
  microseconds. In production the verify phase runs unlocked for tens of seconds.
  The optimistic-commit logic is correct and covered, but its behavior under real
  Lean latency across many workers wants a live multi-worker run.
- **`workspace_manager.render_context_md` has no direct tests.** It is exercised
  indirectly through claim/get_context. The markdown it emits is agent-facing
  prompt text, so it is arguably better validated by reading it than asserting on it.
- **The `lean_code_parser` deprecated island** (`run_lean_code_parser`,
  `assemble_with_markers`, and friends) is untested. It has no in-tree caller;
  if another binary depends on it, it needs its own coverage.
