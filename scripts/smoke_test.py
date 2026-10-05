#!/usr/bin/env python3
"""Synthetic end-to-end audit and portable skill installation, no real history."""
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory() as temp:
        folder = Path(temp)
        source = folder / "synthetic.jsonl"
        rows = [{"type": "assistant", "sessionId": "synthetic", "timestamp": "2026-10-01T12:00:00Z",
            "message": {"id": "synthetic-message", "model": "claude-sonnet-4-6", "role": "assistant",
                "content": [{"type": "text", "text": "SYNTHETIC_PRIVATE_TEXT"}],
                "usage": {"input_tokens": 100, "output_tokens": 10, "cache_creation_input_tokens": 200, "cache_read_input_tokens": 500}}}]
        source.write_text("".join(json.dumps(r) + "\n" for r in rows))
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        subprocess.run([sys.executable, str(ROOT / "scripts" / "install_skill.py"), "--destination", str(folder / "skill")], check=True, capture_output=True)
        runner = folder / "skill" / "scripts" / "run.py"
        subprocess.run([sys.executable, str(runner), "--source", str(source), "--all", "--output", str(folder / "report"),
                        "--price-book", str(ROOT / "docs" / "prices.example.json")], check=True, capture_output=True, cwd=str(folder))
        for name in ("report.json", "summary.md", "report.html", "local-map.json"):
            assert (folder / "report" / name).is_file(), name
        r = json.loads((folder / "report" / "report.json").read_text())
        assert r["coverage"]["unique_requests"] == 1
        assert r["cost"]["priced_requests"] == 1
        assert r["totals"]["output_tokens"] == 10
        for name in ("report.json", "summary.md", "report.html"):
            assert "SYNTHETIC_PRIVATE_TEXT" not in (folder / "report" / name).read_text()
        assert hashlib.sha256(source.read_bytes()).hexdigest() == before
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "install_skill.py"), "--destination", str(folder / "skill")], capture_output=True)
        assert result.returncode != 0
    print("PASS: portable skill install, CLI, four reports, cost coverage, privacy, read-only input, overwrite protection")


if __name__ == "__main__":
    main()
