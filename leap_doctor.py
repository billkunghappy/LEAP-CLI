"""Checks that a machine is ready to run LEAP, and says what to fix if not.

  leap_doctor            # all checks, live verifier + search calls included
  leap_doctor --offline  # skip the network / Lean calls

Exit status is 0 when every required check passes.
"""

import argparse
import os
import shutil
import sys

OK, WARN, FAIL = "ok  ", "warn", "FAIL"


def _line(status: str, what: str, detail: str = "") -> None:
  print(f"[{status}] {what}" + (f" -- {detail}" if detail else ""))


def main(argv) -> int:
  parser = argparse.ArgumentParser(description="LEAP setup check")
  parser.add_argument("--offline", action="store_true")
  args = parser.parse_args(argv[1:])
  failed = False

  # Python + deps
  if sys.version_info < (3, 10):
    _line(FAIL, "python >= 3.10", sys.version.split()[0])
    failed = True
  else:
    _line(OK, "python", sys.version.split()[0])
  try:
    import pydantic  # pylint: disable=g-import-not-at-top
    _line(OK, "pydantic", pydantic.VERSION)
  except ImportError:
    _line(FAIL, "pydantic", "pip install -r requirements.txt")
    return 1

  # Binaries on PATH
  for b in ("state_manager_bin", "lean_verifier_tool_bin", "lean_search_tool_bin"):
    path = shutil.which(b)
    if path:
      _line(OK, b, path)
    else:
      _line(FAIL, b, "not on PATH: export PATH=\"<leap-cli>/bin:$PATH\"")
      failed = True

  if shutil.which("dot"):
    _line(OK, "graphviz dot", "proof-graph SVGs will be rendered")
  else:
    _line(WARN, "graphviz dot", "optional; install graphviz for graph plots")

  # Verifier configuration
  import lean_verifier_tool as lv  # pylint: disable=g-import-not-at-top
  url = os.environ.get("LEAN_VERIFIER_URL", "")
  try:
    backend = lv._resolve_backend(url.rstrip("/"))  # pylint: disable=protected-access
  except ValueError as e:
    _line(FAIL, "verifier backend", str(e))
    return 1
  if backend == "kimina":
    _line(OK, "verifier backend", f"kimina at {url}")
  elif backend == "local":
    project = os.environ.get("LEAN_PROJECT_DIR", "")
    lake = shutil.which("lake")
    _line(OK if lake else FAIL, "lake", lake or "install Lean via elan")
    has_lakefile = any(os.path.exists(os.path.join(project, f))
                       for f in ("lakefile.lean", "lakefile.toml"))
    _line(OK if has_lakefile else FAIL, "LEAN_PROJECT_DIR", project
          if has_lakefile else f"{project!r} has no lakefile")
    failed |= not (lake and has_lakefile)
  else:
    _line(FAIL, "verifier backend",
          "set LEAN_PROJECT_DIR (local Lean + Mathlib) or LEAN_VERIFIER_URL"
          " (kimina-lean-server)")
    failed = True

  if args.offline:
    return 1 if failed else 0

  # Live verifier: must compile, and must report axioms (the soundness gate).
  if backend:
    print("... compiling a probe theorem (first local run can take a minute)")
    out = lv.run_lean_verifier(lv.LeanVerifierInput(
        code="import Mathlib\ntheorem leap_probe : (2:ℝ) + 2 = 4 := by norm_num",
        target_name="leap_probe", timeout=600))
    if out.is_valid and out.used_axioms is not None:
      _line(OK, "verifier", f"probe compiled; axioms {out.used_axioms}")
    elif out.is_valid:
      _line(FAIL, "verifier", "compiled but `#print axioms` output was not"
            " returned -- the soundness gate cannot work")
      failed = True
    else:
      _line(FAIL, "verifier", (out.error_message or "")[:300])
      failed = True

  # Live search
  import lean_search_tool as ls  # pylint: disable=g-import-not-at-top
  res = ls.run_lean_search(ls.LeanSearchInput(query="Nat.add_comm", num_results=1))
  if res.success:
    note = " (leansearch.net down, Loogle used)" if "[note:" in res.results_text else ""
    _line(OK if not note else WARN, "mathlib search", "reachable" + note)
  else:
    _line(WARN, "mathlib search", res.error_message[:200] +
          " -- agents can still probe names with #check")

  return 1 if failed else 0


if __name__ == "__main__":
  sys.exit(main(sys.argv))
