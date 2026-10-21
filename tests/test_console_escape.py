"""Text from pages, LLM answers and configs reaches the console literally (CLAUDE.md rule 7)."""

import tempfile
from pathlib import Path
from unittest.mock import patch

import yaml
from typer.testing import CliRunner

from agent2kb.main import app

runner = CliRunner()

# A closing tag with no opener raises MarkupError; the link tag would make a fake hyperlink.
HOSTILE = "see [[concepts/foo]] and [/x] then [link=https://example.com]click[/link]"


def _config(tmpdir: str) -> Path:
    path = Path(tmpdir) / "config.yaml"
    path.write_text(yaml.dump({"profiles": {"test": {
        "s3_endpoint": "http://localhost:8333",
        "access_key": "testkey",
        "secret_key": "testsecret",
        "bucket": "test-bucket",
        "prefix": "test/",
        "compile_provider": "ollama",
    }}}))
    return path


def _query(answer: str):
    with tempfile.TemporaryDirectory() as tmpdir, \
         patch("agent2kb.query.QueryEngine") as engine:
        engine.return_value.query.return_value = answer
        return runner.invoke(app, [
            "--profile", "test", "--config", str(_config(tmpdir)),
            "query", "what is foo", "--no-save",
        ])


def test_query_answer_with_markup_prints_literally():
    result = _query(HOSTILE)
    assert result.exit_code == 0, result.output
    assert HOSTILE in result.output


def test_query_answer_with_unbalanced_close_tag_does_not_crash():
    result = _query("[/x]")
    assert result.exit_code == 0, result.output
    assert "[/x]" in result.output


def test_safety_warning_escapes_reasons():
    from rich.console import Console

    from agent2kb.ingest.safety import RiskLevel, SafetyResult, format_warning

    res = SafetyResult(url="https://example.com/", risk=RiskLevel.HIGH)
    res.reasons.append("Path contains phishing keywords (/[/x][link=https://example.com]a)")
    console = Console(record=True, width=200)
    console.print(format_warning(res))
    assert "(/[/x][link=https://example.com]a)" in console.export_text()
