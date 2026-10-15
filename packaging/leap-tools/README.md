# leap-tools

A pip package with only the three CLI tools the LEAP agents call. It leaves out
the `leap` command, the agent prompts and the skills:

| Command | Module |
| :-- | :-- |
| `state_manager_bin` | `leap/state_manager.py` (proof graph + phase machine) |
| `lean_verifier_tool_bin` | `leap/lean_verifier_tool.py` (kimina or local Lean) |
| `lean_search_tool_bin` | `leap/lean_search_tool.py` (leansearch.net / Loogle) |

```bash
pip install "git+https://github.com/billkunghappy/LEAP-CLI#subdirectory=packaging/leap-tools"
```

The wheel is built from the same sources in `../../leap/` via hatchling
`force-include`, so nothing is duplicated. Build wheels only; an sdist would
not contain the files:

```bash
uv build --wheel packaging/leap-tools   # or: python -m build --wheel packaging/leap-tools
```

Without `leap lean`, configure the verifier with environment variables (or
`~/.leap/config.json`): `LEAN_VERIFIER_BACKEND=local` + `LEAN_PROJECT_DIR`, or
`LEAN_VERIFIER_URL` (+ `LEAN_VERIFIER_API_KEY`) for a kimina-lean-server.

Do not install it next to `leap-cli`; both provide the `leap` package and the
same commands.
