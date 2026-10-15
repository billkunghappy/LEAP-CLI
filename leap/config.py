"""Persistent LEAP settings, so users never have to export environment variables.

`leap lean` / `leap lean --kimina URL` write `$LEAP_HOME/config.json` (default
`~/.leap/config.json`). Every LEAP command calls `apply()` before doing work,
which copies the saved settings into the environment *without* overriding
anything already set there -- an explicit environment variable always wins.

Keys are the environment variable names the tools read, e.g.
`{"LEAN_PROJECT_DIR": "/Users/me/.leap/mathlib"}`.
"""

import json
import os
from typing import Dict

#: Settings `leap` knows how to save. Anything else in the file is ignored.
KNOWN_KEYS = (
    "LEAN_VERIFIER_BACKEND",
    "LEAN_PROJECT_DIR",
    "LEAN_VERIFIER_URL",
    "LEAN_VERIFIER_API_KEY",
    "LEAN_SEARCH_SUBAGENT_QUOTA",
)

#: The Lean release `leap lean` installs. Mathlib tags a release for each Lean
#: version, so the project pins both the toolchain and Mathlib to this tag.
LEAN_VERSION = "v4.34.0"
LEAN_TOOLCHAIN = f"leanprover/lean4:{LEAN_VERSION}"


def leap_home() -> str:
  return os.path.expanduser(os.environ.get("LEAP_HOME", "~/.leap"))


def config_path() -> str:
  return os.path.join(leap_home(), "config.json")


def project_toolchain(project: str) -> str:
  """The `lean-toolchain` of a Lake project, or "" if it has none."""
  try:
    with open(os.path.join(project, "lean-toolchain"), encoding="utf-8") as f:
      return f.read().strip()
  except OSError:
    return ""


def load() -> Dict[str, str]:
  try:
    with open(config_path(), encoding="utf-8") as f:
      data = json.load(f)
  except (OSError, ValueError):
    return {}
  return {k: str(v) for k, v in data.items() if k in KNOWN_KEYS and v is not None}


def save(updates: Dict[str, str]) -> str:
  """Merges `updates` into the config file. A value of "" removes the key."""
  data = load()
  for k, v in updates.items():
    if k not in KNOWN_KEYS:
      raise KeyError(k)
    if v:
      data[k] = v
    else:
      data.pop(k, None)
  os.makedirs(leap_home(), exist_ok=True)
  path = config_path()
  tmp = path + ".tmp"
  with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
  os.chmod(tmp, 0o600)  # may hold an API key
  os.replace(tmp, path)
  return path


def apply() -> None:
  """Loads saved settings into os.environ (env vars win) and finds elan."""
  for k, v in load().items():
    os.environ.setdefault(k, v)
  # elan installs `lake` here; a fresh install is not on PATH until the shell
  # is restarted, and agent CLIs often run with a minimal PATH.
  elan_bin = os.path.expanduser("~/.elan/bin")
  path = os.environ.get("PATH", "")
  if os.path.isdir(elan_bin) and elan_bin not in path.split(os.pathsep):
    os.environ["PATH"] = path + os.pathsep + elan_bin if path else elan_bin
