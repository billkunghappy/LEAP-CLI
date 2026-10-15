r"""Lean 4 Proof Verifier CLI Tool and Library for the LEMMA harness.

Provides a clean, lightweight CLI and Python interface for LEMMA agents
to verify Lean 4 proofs via a Lean Verifier Service. Uses authoritative
Lean 4 '#print axioms' checking to guarantee mathematical soundness.

This module ships no verifier endpoint of its own. Point it at a service
speaking the wire contract documented in `VERIFIER_WIRE_CONTRACT` below, via
the `LEAN_VERIFIER_URL` environment variable, the `--verifier_url` flag, or
`LeanVerifierInput.verifier_url`. Calling it unconfigured raises
`NotImplementedError` rather than silently reporting proofs as unverifiable.

Usage (Python API):
  import lean_verifier_tool

  # Target theorem verification:
  is_valid = lean_verifier_tool.verify_proof(proof_code, target_name="my_thm")
  is_valid, error_msg = lean_verifier_tool.verify_proof(proof_code, target_name="my_thm", return_error_message=True)

Usage (CLI):
  python3 lean_verifier_tool.py --code_file=proof_scaffold.lean --target_name=my_thm
"""

import asyncio
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Literal, Optional, Tuple, overload
import urllib.error
import urllib.request
import pydantic


try:
  # pylint: disable=g-import-not-at-top
  from absl import app
  from absl import flags

  def _def_flag(name, define_fn, *args, **kwargs):
    assert flags is not None
    if name not in flags.FLAGS:
      return define_fn(name, *args, **kwargs)
    return flags.FLAGS[name]

  _CODE = _def_flag("code", flags.DEFINE_string, "", "The Lean 4 code string to verify.")
  _CODE_FILE = _def_flag(
      "code_file", flags.DEFINE_string, "", "Optional path to a local Lean 4 code file to verify."
  )
  _TARGET_NAME = _def_flag(
      "target_name", flags.DEFINE_string, None, "Optional target theorem/lemma name."
  )
  _VERIFIER_URL = _def_flag(
      "verifier_url", flags.DEFINE_string, None, "Optional custom Lean verifier service base URL."
  )
  _TIMEOUT = _def_flag(
      "timeout", flags.DEFINE_integer, 700, "Verification timeout in seconds."
  )
  _JSON_OUTPUT = _def_flag(
      "json_output", flags.DEFINE_boolean, False, "Whether to print output in JSON format."
  )
  _HAS_ABSL = True
except (ImportError, ModuleNotFoundError):
  app = None
  flags = None
  _HAS_ABSL = False


#: Base URL of the Lean 4 verifier service. Empty by design: the deployment
#: this harness was developed against is not public, so an external install has
#: to bring its own. Set `LEAN_VERIFIER_URL`, pass `--verifier_url`, or set
#: `LeanVerifierInput.verifier_url`. Unset, every verification call raises
#: `NotImplementedError` -- see `VERIFIER_WIRE_CONTRACT`.
DEFAULT_VERIFIER_URL = os.environ.get("LEAN_VERIFIER_URL", "")

#: How requests to the verifier are authenticated.
#:
#: "none" (the default) sends no Authorization header, which is what a
#: self-hosted or reverse-proxied Lean service wants. Any other value selects a
#: token provider, and this release ships none -- the original harness minted a
#: Google Cloud ID token for a Cloud Run deployment, which has no meaning
#: outside that environment. See `get_id_token`.
VERIFIER_AUTH_MODE = os.environ.get("LEAN_VERIFIER_AUTH", "none")

#: The HTTP contract `run_lean_verifier` speaks, spelled out so an external
#: deployment can be stood up against it without reading the request code. It
#: is a string rather than a comment because the `NotImplementedError` raised
#: on an unconfigured install quotes it verbatim.
VERIFIER_WIRE_CONTRACT = """\
REQUEST -- POST to `{base}/api/check`, falling back to `{base}/check` on 404.
  Content-Type: application/json
  {
    "snippets": [{"id": "check_0", "code": "<full Lean 4 source>"}],
    "timeout":  <int, server-side Lean compile budget in seconds>,
    "debug":    false,
    "reuse":    true     // may reuse a warm Lean environment
  }
  Deliberately no "infotree" key: the full pretty-printed tactic tree is
  never read here and cost 576s / 24.6MB on a 4077-line assembly.

RESPONSE -- 200 with JSON:
  {
    "results": [
      {
        "error": "<string>",        // optional; set => nothing was decided.
                                    // Text matching /time-?out/i is treated
                                    // as an infra failure, not a rejection.
        "response": {
          "messages": [
            {
              "severity": "error" | "warning" | "information" | "info",
              "data":     "<message text>",
              "pos":      {"line": <1-based int>, "column": <int>}
            }
          ]
        }
      }
    ]
  }

SEMANTICS the caller depends on:
  * Any message with severity "error" means Lean rejected the code.
  * When `target_name` is given, the tool appends `#print axioms <name>` to
    the snippet and reads the axiom list out of the reply's message data,
    matching `'<ns.>?<name>' depends on axioms: [...]` or
    `'<ns.>?<name>' does not depend on any axioms`. A server that swallows
    `#print axioms` output breaks the soundness gate.
  * An empty `results` array, a transport failure, or a server-side timeout
    is reported as `is_infra_failure=True`, which callers treat as "no
    verdict" rather than "proof is wrong".
  * Retried on HTTP 408/425/429/500/502/503/504 and on any transport
    exception, up to `LEAN_VERIFIER_MAX_ATTEMPTS` times.
"""

# Slack between the server-side Lean compile budget and the client socket
# timeout, to cover response serialization and transfer.
_SOCKET_TIMEOUT_HEADROOM_SEC = 60

# Transport-level retry. A dropped connection or an overloaded server says
# nothing about the mathematics, but the caller previously could not tell that
# apart from a compiler rejection: in scratch/rollout_1, 66 read timeouts were
# reported to agents as regressions and eventually failed 10 AND-nodes as
# "unprovable dead ends". Retry the ones that can succeed on a second attempt,
# and flag the rest as infra failures so the caller can decline to penalize.
_MAX_TRANSPORT_ATTEMPTS = int(os.environ.get("LEAN_VERIFIER_MAX_ATTEMPTS", "3"))
_RETRY_BACKOFF_SEC = (5, 20)
_RETRYABLE_HTTP_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})



class LeanVerifierInput(pydantic.BaseModel):
  """Input configuration for Lean 4 proof verification."""

  code: str = pydantic.Field(
      default="", description="The Lean 4 code string to verify."
  )
  target_name: Optional[str] = pydantic.Field(
      default=None, description="Optional target theorem/lemma name."
  )
  verifier_url: Optional[str] = pydantic.Field(
      default=None, description="Optional custom Lean verifier base URL."
  )
  timeout: int = pydantic.Field(
      default=700, description="Verification timeout in seconds."
  )


class LeanVerifierOutput(pydantic.BaseModel):
  """Verification result for a Lean 4 proof attempt."""

  is_valid: bool = pydantic.Field(
      default=False, description="Whether the proof is valid."
  )
  error_message: Optional[str] = pydantic.Field(
      default=None, description="Error message if verification failed."
  )
  used_axioms: Optional[List[str]] = pydantic.Field(
      default=None, description="List of axioms the target theorem depends on."
  )
  info_messages: str = pydantic.Field(
      default="", description="Information and warning messages (e.g. exact? or #check outputs) from the compiler."
  )
  is_infra_failure: bool = pydantic.Field(
      default=False,
      description=(
          "True when no verdict was obtained at all (transport failure, empty"
          " result set, server-side timeout). Distinct from is_valid=False,"
          " which means Lean actually rejected the code."
      ),
  )


def get_id_token(verifier_url: str) -> Optional[str]:
  """Returns the bearer token for `verifier_url`, or None when unauthenticated.

  Selected by `VERIFIER_AUTH_MODE` (`LEAN_VERIFIER_AUTH`):

    "none"  -- the default and the only mode this release implements. Returns
               None, and the caller sends no Authorization header. Correct for
               a self-hosted Lean service, or one behind a reverse proxy that
               terminates auth itself.

    anything else -- raises `NotImplementedError`.

  The harness this was extracted from ran its verifier on Google Cloud Run,
  which requires a signed Google ID token whose `aud` claim equals the service
  URL. That flow tried three sources in order, and an external replacement
  wanting the same behaviour needs to reimplement it here:

    1. Application Default Credentials. Read the JSON at
       `$GOOGLE_APPLICATION_CREDENTIALS`; if it carries a `refresh_token`,
       POST `client_id`, `client_secret`, `refresh_token` and
       `grant_type=refresh_token` (form-encoded) to
       `https://oauth2.googleapis.com/token` and take the `id_token` field of
       the JSON reply.
    2. `gcloud auth print-identity-token`, whose stdout is the raw token.
    3. The GCE/Cloud Run metadata server identity endpoint, with `audience`
       set to `verifier_url`.

  Whatever replaces this must return a raw token string; the caller sends it
  as `Authorization: bearer <token>`. Returning None means "no header".

  Args:
    verifier_url: Base URL being called. An audience-scoped token provider
      needs it; an audience-free one can ignore it.

  Returns:
    The bearer token, or None when the endpoint takes unauthenticated calls.

  Raises:
    NotImplementedError: For any `VERIFIER_AUTH_MODE` other than "none".
  """
  if VERIFIER_AUTH_MODE == "none":
    return None

  raise NotImplementedError(
      f"LEAN_VERIFIER_AUTH={VERIFIER_AUTH_MODE!r} requests a verifier token"
      f" provider for {verifier_url!r}, and this release ships none. The"
      " original implementation minted a Google Cloud ID token, which is"
      " meaningless outside that deployment. Implement get_id_token() against"
      " your own identity provider -- its contract is in this function's"
      " docstring -- or unset LEAN_VERIFIER_AUTH to call the endpoint"
      " unauthenticated."
  )



def extract_info_messages(proof: str, messages: List[Dict[str, Any]]) -> str:
  """Extracts info and warning messages from Lean responses."""
  body_lines = proof.splitlines()
  info_lines = []
  for m in messages:
    sev = m.get("severity")
    if sev in ["information", "info", "warning"]:
      data = m.get("data", "").strip()
      pos = m.get("pos") or {}
      line_num = pos.get("line")
      if line_num is not None and 1 <= line_num <= len(body_lines):
        snippet = body_lines[line_num - 1].strip()
        info_lines.append(f"[{sev.upper()}] Line {line_num} ('{snippet}'): {data}")
      elif line_num is not None:
        info_lines.append(f"[{sev.upper()}] Line {line_num}: {data}")
      else:
        info_lines.append(f"[{sev.upper()}] {data}")
  return "\n".join(info_lines)

def extract_error_messages(proof: str, messages: List[Dict[str, Any]]) -> str:
  """Extracts and formats compiler error messages from Lean responses."""
  body_lines = proof.splitlines()
  error_lines = []
  for m in messages:
    if m.get("severity") == "error":
      data = m.get("data", "").strip()
      pos = m.get("pos") or {}
      line_num = pos.get("line")
      if line_num is not None and 1 <= line_num <= len(body_lines):
        snippet = body_lines[line_num - 1].strip()
        error_lines.append(f"Line {line_num} ('{snippet}'): {data}")
      elif line_num is not None:
        error_lines.append(f"Line {line_num}: {data}")
      else:
        error_lines.append(data)

  if not error_lines:
    return "Unknown Lean compilation error."
  return "\n".join(error_lines)


def _normalize_lean_imports(code: str) -> str:
  """Ensures all 'import' statements are hoisted to the absolute top of the Lean code.

  If no import statements are present, prepends 'import Mathlib'.
  Correctly tracks multi-line and nested block comments (/- ... -/) and single-line comments (--).
  Preserves non-import statements, comments, and structure after imports.
  """
  lines = code.splitlines()
  import_lines = []
  non_import_lines = []

  comment_depth = 0

  for line in lines:
    stripped = line.strip()
    if not stripped:
      non_import_lines.append(line)
      continue

    # Single-line comment when outside block comment: preserve directly
    if comment_depth == 0 and stripped.startswith("--"):
      non_import_lines.append(line)
      continue

    # If outside block comment and is a genuine import statement
    if comment_depth == 0 and re.match(r"^import\s+[A-Za-z0-9_.]", stripped):
      import_lines.append(stripped)
      # Check if import line opened a block comment that was not closed
      code_part = stripped
      if "--" in code_part:
        code_part = code_part[:code_part.find("--")]
      open_c = len(re.findall(r"/-", code_part))
      close_c = len(re.findall(r"-/", code_part))
      comment_depth = max(0, comment_depth + open_c - close_c)
      continue

    # Update comment depth for lines inside comments or other non-import lines
    code_part = stripped
    if comment_depth == 0 and "--" in code_part:
      code_part = code_part[:code_part.find("--")]
    open_c = len(re.findall(r"/-", code_part))
    close_c = len(re.findall(r"-/", code_part))
    comment_depth = max(0, comment_depth + open_c - close_c)

    non_import_lines.append(line)

  deduped_imports = list(dict.fromkeys(import_lines))
  if not deduped_imports:
    deduped_imports = ["import Mathlib"]

  header = "\n".join(deduped_imports)
  body = "\n".join(non_import_lines).strip()
  if body:
    return f"{header}\n\n{body}"
  return header


def run_lean_verifier(input_data: LeanVerifierInput) -> LeanVerifierOutput:
  """Verifies a Lean 4 proof using the Lean Verifier Service.

  Args:
    input_data: Code, optional target theorem, endpoint override and timeout.

  Returns:
    The verdict. `is_infra_failure=True` means no verdict was reached and says
    nothing about the mathematics.

  Raises:
    NotImplementedError: No verifier endpoint is configured. This release does
      not ship one, so it cannot fall back to a default -- and reporting the
      absence as `is_valid=False` would look like Lean rejecting every proof.
  """
  code = input_data.code.strip()
  if not code:
    return LeanVerifierOutput(
        is_valid=False,
        error_message="Empty code provided.",
    )

  code = _normalize_lean_imports(code)


  # Inject #print axioms natively if we have a target
  if input_data.target_name:
    code += f"\n#print axioms {input_data.target_name}\n"


  verifier_url = (input_data.verifier_url or DEFAULT_VERIFIER_URL).rstrip("/")
  if not verifier_url:
    raise NotImplementedError(
        "No Lean verifier endpoint is configured. This release ships no"
        " default -- the deployment it was developed against is not public."
        " Set the LEAN_VERIFIER_URL environment variable, pass --verifier_url,"
        " or set LeanVerifierInput.verifier_url to a service speaking the"
        " following contract:\n\n"
        + VERIFIER_WIRE_CONTRACT
    )

  is_local = any(
      h in verifier_url for h in ["localhost", "127.0.0.1", "0.0.0.0"]
  )

  headers = {"Content-Type": "application/json"}
  if not is_local:
    token = get_id_token(verifier_url)
    if token:
      headers["Authorization"] = f"bearer {token}"

  # Construct request payload matching Kimina API check format.
  # Deliberately no 'infotree': the server renders the full tactic tree with
  # every goal state pretty-printed, which nothing here reads -- only
  # 'response.messages' is consumed. On a 4077-line assembly it cost 576s /
  # 24.6MB versus 95s / 0.04MB without it, which is what drove the
  # 'read operation timed out' failures on large proof trees.
  payload = json.dumps({
      "snippets": [{"id": "check_0", "code": code}],
      "timeout": input_data.timeout,
      "debug": False,
      "reuse": True,
  }).encode("utf-8")

  endpoints = [f"{verifier_url}/api/check", f"{verifier_url}/check"]
  resp_data = None
  last_err = None

  # The payload 'timeout' bounds the Lean compile only; the server still has to
  # serialize and ship the result afterwards. Giving the socket the same budget
  # meant any job using most of its compile time could never return.
  socket_timeout = input_data.timeout + _SOCKET_TIMEOUT_HEADROOM_SEC

  for attempt in range(max(1, _MAX_TRANSPORT_ATTEMPTS)):
    retryable = False
    for endpoint in endpoints:
      req = urllib.request.Request(
          endpoint, data=payload, headers=headers, method="POST"
      )
      try:
        with urllib.request.urlopen(req, timeout=socket_timeout) as resp:
          resp_data = json.loads(resp.read().decode("utf-8"))
          break
      except urllib.error.HTTPError as e:
        last_err = f"HTTP {e.code}: {e.reason}"
        # 404 means this URL shape is wrong, not that the service is down; fall
        # through to the next endpoint rather than burning a retry.
        if e.code == 404:
          continue
        retryable = e.code in _RETRYABLE_HTTP_CODES
        break
      except Exception as e:  # pylint: disable=broad-exception-caught
        # Socket timeout, connection reset, DNS, truncated body. All transient.
        last_err = str(e)
        retryable = True
        break
    if resp_data is not None:
      break
    if not retryable or attempt >= max(1, _MAX_TRANSPORT_ATTEMPTS) - 1:
      break
    time.sleep(_RETRY_BACKOFF_SEC[min(attempt, len(_RETRY_BACKOFF_SEC) - 1)])

  if resp_data is None:
    # No verdict was obtained, so this says nothing about the proof.
    return LeanVerifierOutput(
        is_valid=False,
        is_infra_failure=True,
        error_message=(
            "Lean verifier service request failed after"
            f" {max(1, _MAX_TRANSPORT_ATTEMPTS)} attempt(s): {last_err}"
        ),
    )

  results = resp_data.get("results", [])
  if not results:
    return LeanVerifierOutput(
        is_valid=False,
        is_infra_failure=True,
        error_message="No verification results returned from verifier service.",
    )

  res = results[0]
  if res.get("error"):
    # A server-side timeout is an infra failure wearing a result's clothing: the
    # snippet never finished elaborating, so nothing was decided about it.
    err_text = str(res["error"])
    return LeanVerifierOutput(
        is_valid=False,
        is_infra_failure=bool(
            re.search(r"time(?:d)?\s*-?\s*out|timeout", err_text, re.IGNORECASE)
        ),
        error_message=f"Lean Verifier Error: {err_text}",
    )

  resp_obj = res.get("response", {})
  messages = resp_obj.get("messages", []) if isinstance(resp_obj, dict) else []
  severities = [m.get("severity") for m in messages]


  has_compiler_error = "error" in severities


  info_string = extract_info_messages(code, messages)
  # None means "could not determine"; [] means "confirmed to depend on nothing".
  # Collapsing the two lets an unparseable result silently pass the axiom gate.
  used_axioms = None
  if input_data.target_name:
    escaped_target = re.escape(input_data.target_name)
    # Lean answers `#print axioms foo` under the name it resolved, so an open
    # `namespace Ns` in the problem statement makes the reply say `Ns.foo`.
    # Anchored on the bare name the match misses, `used_axioms` stays None,
    # and the caller reports a verifier-side failure for a fine submission.
    target_pat = f"'(?:[^'\\s]+\\.)?{escaped_target}'"
    # Parse the raw message payloads rather than the flattened info_string:
    # Lean's pretty-printer wraps long axiom lists across multiple lines, which
    # a single-line regex over the rendered string cannot match.
    for message in messages:
      data = str(message.get("data", ""))
      ax_match = re.search(
          target_pat + " depends on axioms: \\[(.*?)\\]", data, re.DOTALL
      )
      if ax_match:
        used_axioms = [
            ax.strip() for ax in ax_match.group(1).split(",") if ax.strip()
        ]
        break
      if re.search(target_pat + " does not depend on any axioms", data):
        used_axioms = []
        break
  else:
    warning_notice = "[TOOL WARNING] Verification ran without --target_name argument. The tool only verified if the code compiles natively, it did NOT cryptographically check for sorryAx or other cheat axioms. To verify axiom safety, provide --target_name."
    info_string = warning_notice + ("\n" + info_string if info_string else "")

  if has_compiler_error:
    error_msg = extract_error_messages(code, messages)
    return LeanVerifierOutput(
        is_valid=False,
        error_message=error_msg,
        used_axioms=used_axioms,
        info_messages=info_string,
    )

  return LeanVerifierOutput(
      is_valid=True,
      error_message=None,
      used_axioms=used_axioms,
      info_messages=info_string,
  )



@overload
def verify_proof(
    proof: str,
    timeout: int = 700,
    target_name: Optional[str] = None,
    return_error_message: Literal[False] = False,
    verifier_url: Optional[str] = None,
) -> bool:
  ...


@overload
def verify_proof(
    proof: str,
    timeout: int = 700,
    target_name: Optional[str] = None,
    return_error_message: Literal[True] = True,
    verifier_url: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
  ...


def verify_proof(
    proof: str,
    timeout: int = 700,
    target_name: Optional[str] = None,
    return_error_message: bool = False,
    verifier_url: Optional[str] = None,
) -> Any:
  """Judge/verify an agent-generated Lean 4 proof attempt.

  Args:
    proof: Complete Lean 4 formal code string.
    timeout: Verification timeout in seconds.
    target_name: Target theorem name to ensure proof correctness for the goal.
    return_error_message: If True, returns a tuple (is_valid, error_message).
    verifier_url: Optional custom Lean verifier service base URL.

  Returns:
    - If return_error_message=True: Tuple[bool, Optional[str]]
    - Otherwise: bool (True if proof is judged valid, False otherwise).
  """
  input_data = LeanVerifierInput(
      code=proof,
      target_name=target_name,
      verifier_url=verifier_url,
      timeout=timeout,
  )
  output = run_lean_verifier(input_data)
  if return_error_message:
    return output.is_valid, output.error_message
  return output.is_valid


@overload
async def verify_proof_async(
    proof: str,
    timeout: int = 700,
    target_name: Optional[str] = None,
    return_error_message: Literal[False] = False,
    verifier_url: Optional[str] = None,
) -> bool:
  ...


@overload
async def verify_proof_async(
    proof: str,
    timeout: int = 700,
    target_name: Optional[str] = None,
    return_error_message: Literal[True] = True,
    verifier_url: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
  ...


async def verify_proof_async(
    proof: str,
    timeout: int = 700,
    target_name: Optional[str] = None,
    return_error_message: bool = False,
    verifier_url: Optional[str] = None,
) -> Any:
  """Asynchronous proof judge interface for concurrent agent proving loops."""
  input_data = LeanVerifierInput(
      code=proof,
      target_name=target_name,
      verifier_url=verifier_url,
      timeout=timeout,
  )
  output: LeanVerifierOutput = await asyncio.to_thread(
      run_lean_verifier, input_data
  )
  if return_error_message:
    return output.is_valid, output.error_message
  return output.is_valid


def main(argv: List[str]) -> None:
  if _HAS_ABSL and flags is not None:
    code = _CODE.value
    code_file = _CODE_FILE.value
    target_name = _TARGET_NAME.value
    verifier_url = _VERIFIER_URL.value
    timeout = _TIMEOUT.value
    json_output = _JSON_OUTPUT.value
  else:
    import argparse  # pylint: disable=g-import-not-at-top

    parser = argparse.ArgumentParser(
        description="Lean 4 Proof Verifier CLI Tool"
    )
    parser.add_argument(
        "--code", type=str, default="", help="Lean 4 code string"
    )
    parser.add_argument(
        "--code_file", type=str, default="", help="Path to Lean 4 code file"
    )
    parser.add_argument(
        "--target_name", type=str, default=None, help="Target theorem name"
    )
    parser.add_argument(
        "--verifier_url",
        type=str,
        default=None,
        help="Custom verifier service URL",
    )
    parser.add_argument(
        "--timeout", type=int, default=700, help="Timeout in seconds"
    )
    parser.add_argument(
        "--json_output",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Print JSON output",
    )
    args, _ = parser.parse_known_args(argv[1:])
    code = args.code
    code_file = args.code_file
    target_name = args.target_name
    verifier_url = args.verifier_url
    timeout = args.timeout
    json_output = args.json_output

  if code_file and os.path.exists(code_file):
    with open(code_file, "r", encoding="utf-8") as f:
      code = f.read()

  if not code.strip():
    err_msg = "Error: No Lean 4 code provided."
    if json_output:
      print('{"is_valid": false, "error_message": "Error: No Lean 4 code provided."}')
    else:
      print(err_msg)
    sys.exit(1)

  input_data = LeanVerifierInput(
      code=code,
      target_name=target_name,
      verifier_url=verifier_url,
      timeout=timeout,
  )
  output = run_lean_verifier(input_data)

  if json_output:
    print(output.model_dump_json(indent=2))
  else:
    if output.is_valid:
      print("VALID")
      if output.info_messages:
        print(output.info_messages)
    else:
      print("INVALID: " + str(output.error_message))

  if not output.is_valid:
    sys.exit(1)


if __name__ == "__main__":
  if _HAS_ABSL and app is not None:
    app.run(main)
  else:
    main(sys.argv)
