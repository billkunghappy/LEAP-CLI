r"""Native Lean Mathlib Search Tool for the LEMMA harness.

Retrieves mathematical theorems, definitions, and lemmas from Mathlib.

Two backends exist. The public HTTP one (leansearch.net by default) is the only
one this release implements and is the default. The gRPC backend is a seam for
a self-hosted Mathlib search service; selecting it without supplying an
implementation raises `NotImplementedError` -- see `LeanSearchClient._get_stub`
for the service contract it expects.

Usage:
  python3 lean_search_tool.py \
      --query="Irrational (Real.sqrt 2)" \
      --num_results=5

  python3 lean_search_tool.py --query="Continuous.add"
"""

import json
import os
import sys
from typing import Any, Dict, List, Optional
import urllib.request
import pydantic


try:
  # pylint: disable=g-import-not-at-top
  from absl import app
  from absl import flags
  from absl import logging

  _QUERY = flags.DEFINE_string(
      "query", "", "The natural language or formal Mathlib search query string."
  )
  _NUM_RESULTS = flags.DEFINE_integer(
      "num_results", 5, "Maximum number of search results to return."
  )
  _USE_INTERNAL = flags.DEFINE_boolean(
      "use_internal",
      False,
      "Whether to query a self-hosted gRPC search service instead of the"
      " public HTTP endpoint. Requires --internal_address and a local"
      " implementation of LeanSearchClient._get_stub.",
  )
  _INTERNAL_ADDRESS = flags.DEFINE_string(
      "internal_address",
      "",
      "host:port of the gRPC Mathlib search service, when --use_internal.",
  )
  _ENDPOINT = flags.DEFINE_string(
      "endpoint",
      "https://leansearch.net/search",
      "Fallback public HTTP search endpoint.",
  )
  _SUBAGENT_QUOTA = flags.DEFINE_integer(
      "subagent_quota",
      int(os.environ.get("LEAN_SEARCH_SUBAGENT_QUOTA", "-1")),
      "Maximum allowed searches per subagent session. Negative (< 0) means infinite. 0 means 0 searches allowed.",
  )
  _QUOTA_DIR = flags.DEFINE_string(
      "quota_dir",
      "/tmp/jetski_subagent_quota",
      "Directory to track per-subagent search counts.",
  )
  if "json_output" not in flags.FLAGS:
    _JSON_OUTPUT = flags.DEFINE_boolean(
        "json_output", False, "Whether to print raw JSON output."
    )
  else:
    _JSON_OUTPUT = flags.FLAGS["json_output"]
  _HAS_ABSL = True
except (ImportError, ModuleNotFoundError):
  app = None
  flags = None
  logging = None
  _HAS_ABSL = False


class LeanSearchInput(pydantic.BaseModel):
  """Input configuration for Lean Mathlib search."""

  query: str = pydantic.Field(
      description="The natural language or formal Mathlib query string."
  )
  num_results: int = pydantic.Field(
      default=5, description="Maximum number of search results to return."
  )
  use_internal: bool = pydantic.Field(
      default=False,
      description=(
          "Whether to query a self-hosted gRPC search service instead of the"
          " public HTTP endpoint. Not implemented in this release."
      ),
  )
  internal_address: str = pydantic.Field(
      default="",
      description="host:port of the gRPC Mathlib search service.",
  )
  endpoint: str = pydantic.Field(
      default="https://leansearch.net/search",
      description="Fallback public HTTP search endpoint.",
  )


class LeanSearchOutput(pydantic.BaseModel):
  """Search output containing formatted mathematical results and status."""

  results_text: str = pydantic.Field(
      description="Formatted string containing search results."
  )
  success: bool = pydantic.Field(default=True)
  error_message: str = pydantic.Field(default="")


class LeanSearchClient:
  """Client querying a gRPC Mathlib search service or leansearch.net."""

  #: No default: this release ships no gRPC backend, so `--use_internal`
  #: without an explicit `--internal_address` has nowhere to go.
  DEFAULT_INTERNAL_ADDRESS = ""
  DEFAULT_ENDPOINT = "https://leansearch.net/search"

  def __init__(
      self,
      use_internal: bool = False,
      internal_address: Optional[str] = None,
      endpoint: Optional[str] = None,
  ):
    """Initializes the Lean search client."""
    self.use_internal = use_internal
    self.internal_address = internal_address or self.DEFAULT_INTERNAL_ADDRESS
    self.endpoint = endpoint or self.DEFAULT_ENDPOINT
    self._stub: Optional[Any] = None

  def _grpc_search(self, query: str, num_results: int) -> List[Dict[str, Any]]:
    """Fetches Mathlib declarations from a gRPC search service.

    Not implemented. The harness this was extracted from spoke to a Google
    internal `MathlibSearchService` over an authenticated gRPC channel, which
    does not exist outside that network. `search_http` covers the same need
    against the public leansearch.net endpoint and is the default.

    An implementation should connect to `self.internal_address`, cache its stub
    on `self._stub`, issue the query, and return the declarations ranked
    best-first. The service shape the original spoke to was:

      rpc Search(SearchRequest) returns (SearchResponse)

      SearchRequest  { string query = 1; }

      SearchResponse { repeated Declaration declarations = 1; }

      Declaration {
        string fully_qualified_name = 1;  // "Real.sqrt_nonneg"
        string module_name          = 2;  // "Mathlib.Analysis.SpecialFunctions.Sqrt"
        bool   is_prop              = 3;  // true => THEOREM, false => DEF
        string type                 = 4;  // pretty-printed signature
      }

    issued as `stub.Search(request, timeout=10)`.

    Args:
      query: Natural-language or formal Mathlib query, already stripped and
        known non-empty.
      num_results: Cap the caller wants. Returning more is allowed; the caller
        truncates.

    Returns:
      One dict per declaration, in rank order, with keys `name` (str),
      `module` (str), `is_prop` (bool) and `type` (str). An empty list means
      "the service answered, and found nothing" -- distinct from raising,
      which means the query never got an answer.

    Raises:
      NotImplementedError: Always, in this release.
    """
    raise NotImplementedError(
        "No gRPC Mathlib search backend is available. The service this was"
        f" built against ({self.internal_address or 'address unset'}) is not"
        " reachable outside the network it was developed on. Either drop"
        " use_internal to query the public HTTP endpoint, or implement"
        " LeanSearchClient._grpc_search() against your own service -- its"
        f" contract is in this method's docstring. Query was: {query!r},"
        f" num_results={num_results}."
    )

  def search_internal(self, input_data: LeanSearchInput) -> LeanSearchOutput:
    """Queries the gRPC Mathlib search service."""
    query = input_data.query.strip()
    if not query:
      return LeanSearchOutput(
          results_text="No results found for empty query.", success=False
      )

    try:
      declarations = self._grpc_search(query, input_data.num_results)
    except NotImplementedError:
      # Not a transport hiccup. Degrading to the HTTP endpoint here would hide
      # the missing backend behind results that look fine, which is the one
      # outcome this seam exists to prevent.
      raise
    except Exception as e:  # pylint: disable=broad-except
      if logging is not None:
        logging.warning("gRPC Mathlib search failed: %s", e)
      return LeanSearchOutput(
          results_text="",
          success=False,
          error_message=f"gRPC Mathlib search error: {e}",
      )

    if not declarations:
      return LeanSearchOutput(
          results_text=f"No results found for query: '{query}'",
          success=True,
      )

    output = []
    for i, decl in enumerate(declarations[: input_data.num_results], 1):
      kind = "theorem" if decl.get("is_prop") else "def"
      output.append(f"\n{i}. [{kind.upper()}] {decl.get('name', '')}")
      output.append(f"Module: {decl.get('module', '')}")
      output.append(f"Signature: {decl.get('type', '')}")

    return LeanSearchOutput(results_text="\n".join(output).strip(), success=True)

  def search_http(self, input_data: LeanSearchInput) -> LeanSearchOutput:
    """Queries the fallback public HTTP search endpoint."""
    query = input_data.query.strip()
    if not query:
      return LeanSearchOutput(
          results_text="No results found for empty query.", success=False
      )

    payload = json.dumps({
        "num_results": str(input_data.num_results),
        "query": [query],
    }).encode("utf-8")

    req = urllib.request.Request(
        self.endpoint,
        data=payload,
        headers={
            "User-Agent": "leap-lemma/1.0",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
      with urllib.request.urlopen(req, timeout=10) as response:
        results = json.loads(response.read())
        if not results or not results[0]:
          return LeanSearchOutput(
              results_text=f"No results found for query: '{query}'",
              success=True,
          )

        raw_results = [
            r["result"] for r in results[0][: input_data.num_results]
        ]
        output = []
        for i, r in enumerate(raw_results, 1):
          name = ".".join(r.get("name", []))
          mod = ".".join(r.get("module_name", []))
          kind = r.get("kind", "")
          typ = r.get("type", "")
          output.append(f"\n{i}. [{kind.upper()}] {name}")
          output.append(f"Module: {mod}")
          output.append(f"Signature: {typ}")

        return LeanSearchOutput(
            results_text="\n".join(output).strip(), success=True
        )
    except Exception as e:  # pylint: disable=broad-except
      return LeanSearchOutput(
          results_text="",
          success=False,
          error_message=f"Lean HTTP search service error: {e}",
      )

  def search(self, input_data: LeanSearchInput) -> LeanSearchOutput:
    """Performs Mathlib search over HTTP, or over gRPC when asked for it."""
    if input_data.use_internal:
      res = self.search_internal(input_data)
      if res.success:
        return res
      if logging is not None:
        logging.info("Falling back to HTTP search endpoint.")
    return self.search_http(input_data)


def run_lean_search(input_data: LeanSearchInput) -> LeanSearchOutput:
  """Runs Lean Mathlib search for a given input configuration."""
  client = LeanSearchClient(
      use_internal=input_data.use_internal,
      internal_address=input_data.internal_address,
      endpoint=input_data.endpoint,
  )
  return client.search(input_data)


def main(argv: List[str]) -> None:
  """Main CLI entry point for Lean Mathlib search."""
  if _HAS_ABSL and flags is not None:
    query = _QUERY.value
    num_results = _NUM_RESULTS.value
    use_internal = _USE_INTERNAL.value
    internal_address = _INTERNAL_ADDRESS.value
    endpoint = _ENDPOINT.value
    json_output = _JSON_OUTPUT.value
    subagent_quota = _SUBAGENT_QUOTA.value
    quota_dir = _QUOTA_DIR.value
  else:
    import argparse  # pylint: disable=g-import-not-at-top

    parser = argparse.ArgumentParser(description="Lean Search Tool")
    parser.add_argument("--query", type=str, default="")
    parser.add_argument("--num_results", type=int, default=5)
    parser.add_argument("--use_internal", action="store_true", default=False)
    parser.add_argument(
        "--no_use_internal", dest="use_internal", action="store_false"
    )
    parser.add_argument(
        "--internal_address",
        type=str,
        default="",
    )
    parser.add_argument(
        "--endpoint", type=str, default="https://leansearch.net/search"
    )
    parser.add_argument("--json_output", action="store_true", default=False)
    parser.add_argument(
        "--subagent_quota",
        type=int,
        default=int(os.environ.get("LEAN_SEARCH_SUBAGENT_QUOTA", "-1")),
    )
    parser.add_argument(
        "--quota_dir", type=str, default="/tmp/jetski_subagent_quota"
    )
    args, _ = parser.parse_known_args(argv[1:])
    query = args.query
    num_results = args.num_results
    use_internal = args.use_internal
    internal_address = args.internal_address
    endpoint = args.endpoint
    json_output = args.json_output
    subagent_quota = args.subagent_quota
    quota_dir = args.quota_dir

  if not query:
    if logging is not None:
      logging.error("--query must be provided.")
    else:
      print("Error: --query must be provided.")
    return

  # Enforce per-subagent search quota:
  # negative (< 0) = infinite (no quota, no tracking)
  # 0 = zero searches allowed
  # positive (> 0) = quota enforced per ANTIGRAVITY_CONVERSATION_ID
  conv_id = os.environ.get("ANTIGRAVITY_CONVERSATION_ID")
  quota_file = None
  count = 0
  if conv_id is not None and subagent_quota >= 0:
    if subagent_quota == 0:
      msg = (
          "\n[QUOTA EXCEEDED] Subagent search quota is set to 0.\n"
          "Mathlib search is disabled for this subagent.\n"
          "You MUST NOT attempt any searches. Proceed from first principles.\n"
      )
      if json_output:
        print(
            json.dumps({
                "success": False,
                "error_message": msg.strip(),
                "quota_exceeded": True,
            })
        )
      else:
        print(msg)
      return

    os.makedirs(quota_dir, exist_ok=True)
    quota_file = os.path.join(quota_dir, f"{conv_id}.count")
    if os.path.exists(quota_file):
      try:
        with open(quota_file, "r") as f:
          count = int(f.read().strip())
      except Exception:
        count = 0
    if count >= subagent_quota:
      msg = (
          f"\n[QUOTA EXCEEDED] Subagent search quota reached ({count}/{subagent_quota} queries used).\n"
          "Mathlib search is now permanently CLOSED for this subagent.\n"
          "You MUST NOT attempt any more searches.\n"
          "Proceed immediately to Phase 3/4 and draft scratch/blueprint.md from first principles.\n"
      )
      if json_output:
        print(
            json.dumps({
                "success": False,
                "error_message": msg.strip(),
                "quota_exceeded": True,
            })
        )
      else:
        print(msg)
      return

  input_data = LeanSearchInput(
      query=query,
      num_results=num_results,
      use_internal=use_internal,
      internal_address=internal_address,
      endpoint=endpoint,
  )
  output = run_lean_search(input_data)

  if quota_file and subagent_quota > 0:
    count += 1
    try:
      with open(quota_file, "w") as f:
        f.write(str(count))
    except Exception:
      pass

  if json_output:
    print(output.model_dump_json(indent=2))
  else:
    if output.success:
      print(output.results_text)
      if conv_id is not None and subagent_quota > 0:
        print(
            f"\n[SEARCH QUOTA]: {count}/{subagent_quota} queries used for this"
            f" subagent ({subagent_quota - count} remaining)."
        )
    else:
      if logging is not None:
        logging.error("Search failed: %s", output.error_message)
      else:
        print(f"Search failed: {output.error_message}")


if __name__ == "__main__":
  if _HAS_ABSL and app is not None:
    app.run(main)
  else:
    main(sys.argv)
