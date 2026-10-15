"""Tests for the `leap` command and its saved settings. Offline."""

import _path  # noqa: F401  (puts leap/ on sys.path)
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import cli
import config


class _Home(unittest.TestCase):
  """Points LEAP_HOME at a temp dir and isolates the verifier env vars."""

  def setUp(self):
    super().setUp()
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    env = {k: v for k, v in os.environ.items() if k not in config.KNOWN_KEYS}
    env["LEAP_HOME"] = self.tmp.name
    patcher = mock.patch.dict(os.environ, env, clear=True)
    patcher.start()
    self.addCleanup(patcher.stop)


class ConfigTest(_Home):

  def test_save_load_and_remove(self):
    config.save({"LEAN_PROJECT_DIR": "/p", "LEAN_VERIFIER_API_KEY": "k"})
    self.assertEqual(config.load(), {"LEAN_PROJECT_DIR": "/p",
                                     "LEAN_VERIFIER_API_KEY": "k"})
    config.save({"LEAN_VERIFIER_API_KEY": ""})
    self.assertEqual(config.load(), {"LEAN_PROJECT_DIR": "/p"})
    self.assertEqual(os.stat(config.config_path()).st_mode & 0o777, 0o600)

  def test_unknown_key_is_refused(self):
    with self.assertRaises(KeyError):
      config.save({"PATH": "/evil"})

  def test_apply_never_overrides_environment(self):
    config.save({"LEAN_PROJECT_DIR": "/saved", "LEAN_VERIFIER_URL": "http://s"})
    os.environ["LEAN_PROJECT_DIR"] = "/from-env"
    config.apply()
    self.assertEqual(os.environ["LEAN_PROJECT_DIR"], "/from-env")
    self.assertEqual(os.environ["LEAN_VERIFIER_URL"], "http://s")

  def test_corrupt_file_reads_as_empty(self):
    os.makedirs(self.tmp.name, exist_ok=True)
    with open(config.config_path(), "w") as f:
      f.write("{not json")
    self.assertEqual(config.load(), {})


class LeanKiminaTest(_Home):

  def test_kimina_is_saved_and_probed(self):
    with mock.patch.object(cli, "_probe", return_value=True) as probe, \
        contextlib.redirect_stdout(io.StringIO()):
      rc = cli.main(["lean", "--kimina", "http://h:8000/", "--api-key", "abc"])
    self.assertEqual(rc, 0)
    probe.assert_called_once()
    with open(config.config_path()) as f:
      saved = json.load(f)
    self.assertEqual(saved, {"LEAN_VERIFIER_BACKEND": "kimina",
                             "LEAN_VERIFIER_URL": "http://h:8000",
                             "LEAN_VERIFIER_API_KEY": "abc"})

  def test_new_project_is_pinned_to_lean_version(self):
    with mock.patch.object(cli.shutil, "which", return_value="/bin/lake"), \
        mock.patch.object(cli, "_run") as run, \
        mock.patch.object(cli, "_probe", return_value=True), \
        contextlib.redirect_stdout(io.StringIO()):
      rc = cli.main(["lean"])
    self.assertEqual(rc, 0)
    project = os.path.join(self.tmp.name, "leap_lean")
    self.assertEqual(config.project_toolchain(project),
                     "leanprover/lean4:" + config.LEAN_VERSION)
    with open(os.path.join(project, "lakefile.toml")) as f:
      self.assertIn(f'rev = "{config.LEAN_VERSION}"', f.read())
    self.assertEqual([c.args[0][1:] for c in run.call_args_list],
                     [["update"], ["exe", "cache", "get"], ["build"]])
    self.assertEqual(config.load()["LEAN_PROJECT_DIR"], project)

  def test_default_project_on_other_toolchain_is_refused(self):
    project = os.path.join(self.tmp.name, "leap_lean")
    os.makedirs(project)
    for name, text in (("lakefile.toml", ""),
                       ("lean-toolchain", "leanprover/lean4:v4.20.0\n")):
      with open(os.path.join(project, name), "w") as f:
        f.write(text)
    with mock.patch.object(cli.shutil, "which", return_value="/bin/lake"), \
        mock.patch.object(cli, "_run") as run, \
        contextlib.redirect_stdout(io.StringIO()):
      rc = cli.main(["lean"])
    self.assertEqual(rc, 1)
    run.assert_not_called()
    self.assertEqual(config.load(), {})

  def test_existing_project_without_lakefile_is_refused(self):
    with mock.patch.object(cli.shutil, "which", return_value="/bin/lake"), \
        contextlib.redirect_stdout(io.StringIO()):
      rc = cli.main(["lean", "--project", self.tmp.name])
    self.assertEqual(rc, 1)
    self.assertEqual(config.load(), {})


class InitTest(_Home):

  def test_init_writes_agents_and_problems_dir(self):
    dest = os.path.join(self.tmp.name, "ws")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
      rc = cli.main(["init", "claude", "--dest", dest])
    self.assertEqual(rc, 0)
    self.assertTrue(os.path.exists(
        os.path.join(dest, ".claude", "agents", "orchestrator-lemma.md")))
    self.assertTrue(os.path.isdir(os.path.join(dest, "problems")))
    self.assertIn("leap lean", out.getvalue())  # no verifier configured yet
    self.assertIn("claude --agent orchestrator-lemma", out.getvalue())


class LoopTest(_Home):

  def test_codex_gets_positional_resume_and_skill_prefix(self):
    with mock.patch.object(cli.subprocess, "call", return_value=0) as call:
      cli.main(["loop", "codex", "exec", "$leap-prove Prove problems/a.lean"])
    cmd = call.call_args.args[0]
    env = call.call_args.kwargs["env"]
    self.assertTrue(cmd[1].endswith("safe_lemma_runner.sh"))
    self.assertEqual(cmd[2:], ["exec", "$leap-prove Prove problems/a.lean"])
    self.assertEqual(env["LEAP_AGENT_CLI"], "codex")
    self.assertEqual(env["LEAP_RESUME_FLAG"], "")
    self.assertEqual(env["LEAP_RESUME_PREFIX"], "$leap-prove ")


if __name__ == "__main__":
  unittest.main()
