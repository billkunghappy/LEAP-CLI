"""Contract tests for the Lean verifier client against a live Lean service.

The live tests call the configured verifier (kimina or local Lean), so they
are skipped unless LEMMA_LIVE_VERIFIER=1:

    LEMMA_LIVE_VERIFIER=1 python3 tests/lean_verifier_tool_test.py

They pin the two properties the soundness gate depends on, both of which are
easy to regress silently:

  1. 'sorry' compiles as a WARNING, so is_valid alone proves nothing about
     whether a proof is complete. #print axioms is the only real check.
  2. Lean's pretty-printer wraps long axiom lists across multiple lines, so the
     axiom list must be parsed from the raw message payload rather than from a
     rendered single-line string.

"""

import _path  # noqa: F401  (puts leap/ on sys.path)
import contextlib
import json
import os
import unittest
from unittest import mock

from absl.testing import absltest

import lean_verifier_tool as lv  # pylint: disable=g-import-not-at-top


_LIVE = os.environ.get("LEMMA_LIVE_VERIFIER") == "1"
_SKIP_REASON = "Set LEMMA_LIVE_VERIFIER=1 to run against the configured Lean verifier."

# The standard Lean axioms every ordinary classical proof may depend on.
_STANDARD = {"propext", "Classical.choice", "Quot.sound"}


def _verify(code, target_name, timeout=180):
  return lv.run_lean_verifier(
      lv.LeanVerifierInput(code=code, target_name=target_name, timeout=timeout)
  )


def _fake_response(payload: bytes):
  """A urlopen stand-in returning `payload` from a context manager."""

  class _Resp:

    def read(self):
      return payload

  def _open(*_args, **_kwargs):
    return contextlib.nullcontext(_Resp())

  return _open


@unittest.skipUnless(_LIVE, _SKIP_REASON)
class SorryIsWarningTest(absltest.TestCase):
  """`sorry` does not fail compilation, which is why the axiom gate exists."""

  def test_false_theorem_with_sorry_still_compiles(self):
    out = _verify("theorem probe_false : 2 + 2 = 5 := by sorry", "probe_false")
    self.assertTrue(
        out.is_valid,
        "If this ever fails, Lean changed sorry to an error and the axiom gate "
        "is no longer load-bearing.",
    )

  def test_sorry_is_reported_through_the_axiom_list(self):
    out = _verify("theorem probe_false : 2 + 2 = 5 := by sorry", "probe_false")
    self.assertIn("sorryAx", out.used_axioms)


@unittest.skipUnless(_LIVE, _SKIP_REASON)
class AxiomExtractionTest(absltest.TestCase):

  def test_constructive_proof_reports_empty_not_none(self):
    """[] means 'confirmed no axioms' and must stay distinct from None."""
    out = _verify("theorem probe_rfl : 2 + 2 = 4 := by rfl", "probe_rfl")
    self.assertIsNotNone(out.used_axioms)
    self.assertEmpty(out.used_axioms)

  def test_classical_proof_reports_the_standard_three(self):
    out = _verify(
        "theorem probe_em (p : Prop) : p ∨ ¬p := Classical.em p", "probe_em"
    )
    self.assertEqual(set(out.used_axioms), _STANDARD)

  def test_long_axiom_list_wraps_and_is_still_parsed(self):
    """Lean breaks long lists onto separate lines; a single-line regex misses.

    This is the regression that let a wrapped list read as zero axioms, which
    silently disabled the sorryAx check on most real decompositions.
    """
    names = [
        f"extremely_long_axiom_identifier_number_{w}"
        for w in ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot",
                  "golf", "hotel", "india", "juliet", "kilo", "lima"]
    ]
    axioms = "\n".join(f"axiom {n} : True" for n in names)
    conclusion = " ∧ ".join(["True"] * len(names))
    proof = "⟨" + ", ".join(names) + "⟩"
    out = _verify(
        f"{axioms}\ntheorem probe_wrap : {conclusion} := {proof}", "probe_wrap"
    )
    self.assertContainsSubset(names, out.used_axioms)

  def test_native_decide_exposes_compiler_trust_axioms(self):
    """native_decide must be detectable: it trusts the compiler, not the kernel."""
    out = _verify(
        "theorem probe_nd : (12345 * 6789 : Nat) = 83810205 := by native_decide",
        "probe_nd",
    )
    self.assertTrue(out.is_valid, "native_decide compiles; only axioms reveal it")
    self.assertFalse(
        set(out.used_axioms) <= _STANDARD,
        "native_decide must fall outside the standard axiom set",
    )


@unittest.skipUnless(_LIVE, _SKIP_REASON)
class CompilerErrorTest(absltest.TestCase):

  def test_syntax_error_is_reported_invalid(self):
    out = _verify("theorem broken : := by", "broken")
    self.assertFalse(out.is_valid)
    self.assertTrue(out.error_message)

  def test_missing_target_name_is_reported_invalid(self):
    """#print axioms on an undeclared name is itself a compile error."""
    out = _verify("theorem present : True := trivial", "absent")
    self.assertFalse(out.is_valid)


class NoNetworkTest(absltest.TestCase):
  """Runs everywhere; documents the None-vs-[] sentinel without a network call."""

  def test_used_axioms_defaults_to_none(self):
    self.assertIsNone(lv.LeanVerifierOutput().used_axioms)

  def test_empty_code_is_rejected_without_a_request(self):
    out = lv.run_lean_verifier(lv.LeanVerifierInput(code="   "))
    self.assertFalse(out.is_valid)
    self.assertIn("Empty code", out.error_message)


class InfraFailureTest(absltest.TestCase):
  """A transport failure is not a verdict, and must not read like one."""

  def _run_with_transport(self, side_effect, attempts=2):
    slept = []
    with mock.patch.object(lv, "_MAX_TRANSPORT_ATTEMPTS", attempts), \
        mock.patch.object(lv.time, "sleep", slept.append), \
        mock.patch.object(lv.urllib.request, "urlopen", side_effect=side_effect):
      out = lv.run_lean_verifier(
          lv.LeanVerifierInput(
              code="theorem t : True := trivial",
              timeout=5,
              # urlopen is stubbed, so no request leaves the process.
              verifier_url="http://localhost:9999",
          )
      )
    return out, slept

  def test_socket_timeout_is_flagged_as_infra_not_invalid_proof(self):
    out, _ = self._run_with_transport(TimeoutError("read operation timed out"))
    self.assertFalse(out.is_valid)
    self.assertTrue(out.is_infra_failure)
    self.assertIn("timed out", out.error_message)

  def test_transport_failures_are_retried_with_backoff(self):
    out, slept = self._run_with_transport(TimeoutError("boom"), attempts=3)
    self.assertFalse(out.is_valid)
    # Two endpoint shapes are tried per attempt; only the attempts back off.
    self.assertLen(slept, 2)
    self.assertIn("3 attempt(s)", out.error_message)

  def test_client_errors_are_not_retried(self):
    err = lv.urllib.error.HTTPError(
        "http://x", 400, "Bad Request", {}, None
    )
    out, slept = self._run_with_transport(err, attempts=3)
    self.assertFalse(out.is_valid)
    self.assertEmpty(slept, "a 400 will not become a 200 on a second attempt")

  def test_server_side_timeout_in_the_result_is_infra(self):
    payload = json.dumps(
        {"results": [{"error": "Lean process timed out after 300s"}]}
    ).encode()
    out, _ = self._run_with_transport(_fake_response(payload))
    self.assertFalse(out.is_valid)
    self.assertTrue(out.is_infra_failure)

  def test_a_genuine_compiler_rejection_is_not_infra(self):
    payload = json.dumps({
        "results": [{
            "response": {
                "messages": [
                    {"severity": "error", "data": "unknown identifier 'foo'"}
                ]
            }
        }]
    }).encode()
    out, _ = self._run_with_transport(_fake_response(payload))
    self.assertFalse(out.is_valid)
    self.assertFalse(
        out.is_infra_failure, "Lean answered; the answer was 'no'"
    )


class NamespacedReadbackTest(absltest.TestCase):
  """Lean prints the name it resolved, not the one we asked about.

  `fmf_v1_002` opened `namespace SimpleGraph` in its immutable context and never
  closed it, so every `#print axioms Foo` answered `SimpleGraph.Foo`. Anchored
  on the bare name the readback missed, `used_axioms` stayed None, and 58 sound
  submissions were reported to the agent as verifier faults.

  Offline: the payload is the shape the service returns, so these run anywhere.
  """

  TARGET = "HadwigerNelsonAtLeastFive"

  def _readback(self, data, target=TARGET):
    payload = json.dumps(
        {"results": [{"response": {"messages": [
            {"severity": "info", "data": data}
        ]}}]}
    ).encode()
    with mock.patch.object(
        lv.urllib.request, "urlopen", side_effect=_fake_response(payload)
    ):
      return lv.run_lean_verifier(
          lv.LeanVerifierInput(
              code="theorem t : True := trivial",
              target_name=target,
              timeout=5,
              verifier_url="http://localhost:9999",
          )
      ).used_axioms

  def test_namespaced_target_axiom_list_is_read(self):
    self.assertEqual(
        ["propext", "Classical.choice"],
        self._readback(
            f"'SimpleGraph.{self.TARGET}' depends on axioms: [propext,"
            " Classical.choice]"
        ),
    )

  def test_namespaced_target_with_no_axioms_reads_empty_not_none(self):
    got = self._readback(f"'SimpleGraph.{self.TARGET}' does not depend on any axioms")
    self.assertEqual([], got, "[] means 'confirmed nothing'; None means 'unknown'")

  def test_nested_namespaces_are_read(self):
    self.assertEqual(
        ["sorryAx"],
        self._readback(f"'A.B.C.{self.TARGET}' depends on axioms: [sorryAx]"),
    )

  def test_bare_target_still_reads(self):
    """The prefix is optional; the no-namespace case must not regress."""
    self.assertEqual(
        ["propext"],
        self._readback(f"'{self.TARGET}' depends on axioms: [propext]"),
    )

  def test_a_different_declaration_is_not_mistaken_for_the_target(self):
    """Loosening the anchor must not make the gate read someone else's axioms."""
    self.assertIsNone(
        self._readback("'SomethingElse' depends on axioms: [sorryAx]")
    )

  def test_a_name_merely_ending_in_the_target_does_not_match(self):
    """`XFoo` is a different declaration from `Foo`; only `Ns.Foo` is the same one."""
    self.assertIsNone(
        self._readback(f"'Prefixed{self.TARGET}' depends on axioms: [sorryAx]")
    )


class BackendSelectionTest(absltest.TestCase):
  """Which backend runs is decided by config, never guessed silently."""

  def _resolve(self, env, url=""):
    with mock.patch.dict(os.environ, env, clear=False):
      for k in ("LEAN_VERIFIER_BACKEND", "LEAN_PROJECT_DIR"):
        if k not in env:
          os.environ.pop(k, None)
      with mock.patch.object(lv, "VERIFIER_BACKEND", ""), \
          mock.patch.object(lv, "LEAN_PROJECT_DIR", ""):
        return lv._resolve_backend(url)

  def test_url_selects_kimina(self):
    self.assertEqual(self._resolve({}, "http://h:8000"), "kimina")

  def test_project_dir_selects_local(self):
    self.assertEqual(self._resolve({"LEAN_PROJECT_DIR": "/x"}), "local")

  def test_explicit_backend_wins(self):
    self.assertEqual(
        self._resolve({"LEAN_VERIFIER_BACKEND": "local"}, "http://h"), "local"
    )

  def test_nothing_configured_raises_not_implemented(self):
    with mock.patch.dict(os.environ, {}, clear=False):
      for k in ("LEAN_VERIFIER_BACKEND", "LEAN_PROJECT_DIR", "LEAN_VERIFIER_URL"):
        os.environ.pop(k, None)
      with mock.patch.object(lv, "DEFAULT_VERIFIER_URL", ""), \
          mock.patch.object(lv, "VERIFIER_BACKEND", ""), \
          mock.patch.object(lv, "LEAN_PROJECT_DIR", ""):
        with self.assertRaises(NotImplementedError):
          lv.run_lean_verifier(lv.LeanVerifierInput(code="theorem t : True := trivial"))


class KiminaAuthTest(absltest.TestCase):

  def test_api_key_sent_as_capitalised_bearer(self):
    # kimina-lean-server strips a case-sensitive "Bearer " prefix.
    seen = {}

    def _open(req, *_a, **_k):
      seen.update(req.headers)
      return _fake_response(b'{"results": [{"response": {"messages": []}}]}')()

    with mock.patch.dict(os.environ, {"LEAN_VERIFIER_API_KEY": "k123"}), \
        mock.patch.object(lv.urllib.request, "urlopen", _open):
      out = lv.run_lean_verifier(
          lv.LeanVerifierInput(code="theorem t : True := trivial",
                               verifier_url="http://localhost:9999")
      )
    self.assertTrue(out.is_valid)
    self.assertEqual(seen.get("Authorization"), "Bearer k123")


class LocalBackendTest(absltest.TestCase):
  """`lake env lean --json` output is mapped onto the kimina message shape."""

  def _run(self, stdout, returncode=0, stderr="", target="t"):
    proc = mock.Mock(stdout=stdout, stderr=stderr, returncode=returncode)
    env = {"LEAN_VERIFIER_BACKEND": "local", "LEAN_PROJECT_DIR": "/"}
    with mock.patch.dict(os.environ, env), \
        mock.patch.object(lv.shutil, "which", return_value="/usr/bin/lake"), \
        mock.patch.object(lv.subprocess, "run", return_value=proc) as run:
      out = lv.run_lean_verifier(
          lv.LeanVerifierInput(code="theorem t : True := trivial", target_name=target)
      )
    return out, run

  def test_clean_compile_with_axioms(self):
    msg = {"severity": "information", "pos": {"line": 5, "column": 0},
           "data": "'t' depends on axioms: [propext]"}
    out, run = self._run(json.dumps(msg) + "\n")
    self.assertTrue(out.is_valid)
    self.assertEqual(out.used_axioms, ["propext"])
    self.assertEqual(run.call_args.args[0][1:4], ["env", "lean", "--json"])

  def test_error_message_is_a_rejection_not_infra(self):
    msg = {"severity": "error", "pos": {"line": 3, "column": 2},
           "data": "unknown identifier 'foo'"}
    out, _ = self._run(json.dumps(msg) + "\n", returncode=1)
    self.assertFalse(out.is_valid)
    self.assertFalse(out.is_infra_failure)
    self.assertIn("unknown identifier", out.error_message)

  def test_crash_without_source_error_is_infra(self):
    out, _ = self._run("", returncode=1, stderr="unknown package 'Mathlib'")
    self.assertFalse(out.is_valid)
    self.assertTrue(out.is_infra_failure)
    self.assertIn("Mathlib", out.error_message)

  def test_timeout_is_infra(self):
    env = {"LEAN_VERIFIER_BACKEND": "local", "LEAN_PROJECT_DIR": "/"}
    with mock.patch.dict(os.environ, env), \
        mock.patch.object(lv.shutil, "which", return_value="/usr/bin/lake"), \
        mock.patch.object(lv.subprocess, "run",
                          side_effect=lv.subprocess.TimeoutExpired("lake", 5)):
      out = lv.run_lean_verifier(lv.LeanVerifierInput(code="theorem t : True := trivial"))
    self.assertTrue(out.is_infra_failure)


if __name__ == "__main__":
  absltest.main()
