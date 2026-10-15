#!/bin/bash
# ==============================================================================
# safe_lemma_runner.sh: Zero-Zombie, Signal-Safe Autonomous Rollout Runner
# Specifically designed for custom/BYOM model endpoints (e.g. beyond-api://)
# that may undergo periodic TPU pod reboots (5-15 minute outages).
#
# The agent CLI it drives has no default: set LEAP_AGENT_CLI to the binary that
# accepts the arguments you pass through to this script.
# ==============================================================================
set -u

export PATH="${HOME}/.local/bin:${PATH:-/usr/local/bin:/usr/bin:/bin}"
AGENT_CLI="${LEAP_AGENT_CLI:-}"
if [[ -z "${AGENT_CLI}" ]]; then
  echo "error: LEAP_AGENT_CLI is unset. Set it to the binary that runs your" \
       "agent -- there is no portable default to guess." >&2
  exit 1
fi
if ! command -v "${AGENT_CLI}" >/dev/null 2>&1 && [[ ! -x "${AGENT_CLI}" ]]; then
  echo "error: LEAP_AGENT_CLI='${AGENT_CLI}' is neither on PATH nor an" \
       "executable file." >&2
  exit 1
fi
child_pid=""

cleanup() {
  echo "[$(date)] safe_lemma_runner received termination signal. Cleaning up..."
  if [[ -n "${child_pid:-}" ]] && kill -0 "${child_pid}" 2>/dev/null; then
    kill -TERM "${child_pid}" 2>/dev/null || true
    wait "${child_pid}" 2>/dev/null || true
  fi
  exit 0
}

trap cleanup SIGINT SIGTERM SIGHUP EXIT

FIRST_RUN=true
TARGET_DIR="${LEAP_TARGET_DIR:-}"
if [[ -z "${TARGET_DIR}" ]]; then
  for arg in "$@"; do
    if [[ "${arg}" =~ (/[^[:space:]]*rollout_[0-9]+) ]]; then
      TARGET_DIR="${BASH_REMATCH[1]}"
    fi
  done
fi

while true; do
  CLI_ARGS=()
  if [[ "${FIRST_RUN}" == "true" ]] || [[ -n "${TARGET_DIR}" && ! -f "${TARGET_DIR}/proof_graph.json" ]]; then
    CLI_ARGS=("$@")
  else
    if [[ -n "${TARGET_DIR}" ]]; then
      echo "[$(date)] Releasing stranded processing subgoals in ${TARGET_DIR} via release_all..."
      python3 -c "
import json, os
pg_file = os.path.join('${TARGET_DIR}', 'proof_graph.json')
if os.path.exists(pg_file):
    with open(pg_file) as f: pg = json.load(f)
    for n in pg.get('nodes', {}).values():
        if 'search_metadata' in n:
            n['search_metadata']['processing'] = False
            n['search_metadata'].pop('claimed_by', None)
    with open(pg_file, 'w') as f: json.dump(pg, f, indent=2)
" 2>/dev/null || true
    fi

    # Strip existing -p / --prompt arguments to replace with continuation prompt
    skip_next=false
    for arg in "$@"; do
      if [[ "${skip_next}" == "true" ]]; then
        skip_next=false
        continue
      fi
      if [[ "${arg}" == "-p" || "${arg}" == "--prompt" ]]; then
        skip_next=true
        continue
      elif [[ "${arg}" =~ ^(-p|--prompt)= ]]; then
        continue
      fi
      CLI_ARGS+=("${arg}")
    done

    CLI_ARGS+=(-p "Resume orchestration on the existing graph at ${TARGET_DIR}. All proved nodes and subgoals are preserved in proof_graph.json. Inspect open nodes, release any stranded claims, and continue solving to completion.")
  fi

  "${AGENT_CLI}" \
    "${CLI_ARGS[@]}" &

  child_pid=$!
  wait "${child_pid}"
  exit_code=$?
  child_pid=""

  if [[ ${exit_code} -eq 0 ]]; then
    # Check if Phase 5 Complete is recorded in THIS specific problem orchestrator log (must be checked [x])
    if [[ -n "${TARGET_DIR}" && -f "${TARGET_DIR}/orchestrator_log.md" ]] && grep -q -- "- \[x\] \[PHASE 5: COMPLETE\]" "${TARGET_DIR}/orchestrator_log.md"; then
      if [[ -f "${TARGET_DIR}/assembled/final_proof.lean" ]] && grep -w -q "sorry" "${TARGET_DIR}/assembled/final_proof.lean"; then
        echo "[$(date)] Assembled final_proof.lean still contains sorry placeholders. Continuing orchestration..."
      else
        echo "[$(date)] Target ${TARGET_DIR} reached verified Phase 5 Complete! Exiting loop."
        break
      fi
    fi
    echo "[$(date)] CLI turn finished. Continuing orchestration in 10s..."
    FIRST_RUN=false
    sleep 10 &
    sleep_pid=$!
    wait "${sleep_pid}" 2>/dev/null || break
  elif [[ ${exit_code} -eq 130 ]]; then
    echo "[$(date)] Execution terminated by user signal (SIGINT / exit code ${exit_code}). Exiting loop."
    break
  else
    echo "[$(date)] Execution exited with code ${exit_code} (transient outage / signal 143). Retrying with clean resume prompt in 60s..."
    FIRST_RUN=false
    sleep 60 &
    sleep_pid=$!
    wait "${sleep_pid}" 2>/dev/null || break
  fi
done
