"""Unit tests for LeanSearchTool."""

from unittest import mock
from absl.testing import absltest
from absl.testing import flagsaver
import lean_search_tool


class LeanSearchToolTest(absltest.TestCase):

  def test_empty_query(self):
    res = lean_search_tool.run_lean_search(
        lean_search_tool.LeanSearchInput(query="")
    )
    self.assertFalse(res.success)
    self.assertIn("empty query", res.results_text.lower())

  @mock.patch("urllib.request.urlopen")
  def test_http_search(self, mock_urlopen):
    mock_response = mock.MagicMock()
    mock_response.read.return_value = (
        b'[[{"result": {"name": ["Real", "sqrt"], "module_name": ["Mathlib",'
        b' "Data", "Real", "Basic"], "kind": "def", "type": "Real ->'
        b' Real"}}]]'
    )
    mock_response.__enter__.return_value = mock_response
    mock_urlopen.return_value = mock_response

    res = lean_search_tool.run_lean_search(
        lean_search_tool.LeanSearchInput(query="sqrt", use_internal=False)
    )
    self.assertTrue(res.success)
    self.assertIn("Real.sqrt", res.results_text)
    self.assertIn("Mathlib.Data.Real.Basic", res.results_text)

  def test_grpc_backend_is_not_implemented(self):
    """Asking for the gRPC backend fails loudly rather than degrading to HTTP."""
    with self.assertRaises(NotImplementedError):
      lean_search_tool.run_lean_search(
          lean_search_tool.LeanSearchInput(
              query="Irrational", use_internal=True
          )
      )

  @mock.patch.object(lean_search_tool.LeanSearchClient, "_grpc_search")
  def test_grpc_search_formatting(self, mock_grpc_search):
    """A supplied gRPC backend is rendered the same way as the HTTP one."""
    mock_grpc_search.return_value = [{
        "name": "Irrational.sqrt",
        "module": "Mathlib.Data.Real.Irrational",
        "type": "Irrational (Real.sqrt 2)",
        "is_prop": True,
    }]

    res = lean_search_tool.run_lean_search(
        lean_search_tool.LeanSearchInput(query="Irrational", use_internal=True)
    )
    self.assertTrue(res.success)
    self.assertIn("[THEOREM] Irrational.sqrt", res.results_text)
    self.assertIn("Mathlib.Data.Real.Irrational", res.results_text)

  @mock.patch("lean_search_tool.run_lean_search")
  def test_main_cli(self, mock_run):
    mock_run.return_value = lean_search_tool.LeanSearchOutput(
        results_text="[THEOREM] Continuous.add", success=True
    )
    with flagsaver.flagsaver(
        query="Continuous.add", num_results=3, use_internal=False
    ):
      lean_search_tool.main([])
    mock_run.assert_called_once()

  @mock.patch("lean_search_tool.run_lean_search")
  def test_quota_negative_infinite(self, mock_run):
    mock_run.return_value = lean_search_tool.LeanSearchOutput(
        results_text="[THEOREM] Continuous.add", success=True
    )
    with mock.patch.dict("os.environ", {"ANTIGRAVITY_CONVERSATION_ID": "test_conv_inf"}):
      with flagsaver.flagsaver(
          query="Continuous.add", subagent_quota=-1, use_internal=False
      ):
        lean_search_tool.main([])
    mock_run.assert_called_once()

  @mock.patch("lean_search_tool.run_lean_search")
  def test_quota_zero_blocks(self, mock_run):
    with mock.patch.dict("os.environ", {"ANTIGRAVITY_CONVERSATION_ID": "test_conv_zero"}):
      with flagsaver.flagsaver(
          query="Continuous.add", subagent_quota=0, use_internal=False
      ):
        lean_search_tool.main([])
    mock_run.assert_not_called()

  @mock.patch("lean_search_tool.run_lean_search")
  def test_quota_positive_enforced(self, mock_run):
    mock_run.return_value = lean_search_tool.LeanSearchOutput(
        results_text="[THEOREM] Continuous.add", success=True
    )
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
      with mock.patch.dict("os.environ", {"ANTIGRAVITY_CONVERSATION_ID": "test_conv_pos"}):
        with flagsaver.flagsaver(
            query="Continuous.add", subagent_quota=2, quota_dir=tmpdir, use_internal=False
        ):
          # Call 1: allowed
          lean_search_tool.main([])
          self.assertEqual(mock_run.call_count, 1)

          # Call 2: allowed
          lean_search_tool.main([])
          self.assertEqual(mock_run.call_count, 2)

          # Call 3: blocked by quota
          lean_search_tool.main([])
          self.assertEqual(mock_run.call_count, 2)


if __name__ == "__main__":
  absltest.main()
