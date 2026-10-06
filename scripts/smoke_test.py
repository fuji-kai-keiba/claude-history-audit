#!/usr/bin/env python3
"""Synthetic end-to-end audit and portable skill installation, no real history."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import zipfile
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
        subprocess.run([sys.executable, str(runner), "--source", str(source), "--all", "--output", str(folder / "report")], check=True, capture_output=True, cwd=str(folder))
        for name in ("report.json", "summary.md", "report.html", "local-map.json"):
            assert (folder / "report" / name).is_file(), name
        r = json.loads((folder / "report" / "report.json").read_text())
        assert r["coverage"]["unique_requests"] == 1
        assert r["cost"]["priced_requests"] == 1
        assert r["totals"]["output_tokens"] == 10
        assert r["deep"]["diagnosis"]["cache_writes"]["reconciles_to_total"]
        assert r["deep"]["diagnosis"]["cost_coverage"]["unknown_ttl_write_tokens"] == 200
        assert r["deep"]["summary"]["requests"] == 1
        assert r["deep"]["summary"]["priced_requests"] == 1
        assert r["deep"]["executions"][0]["trajectory"][0]["context_tokens"] == 800
        for name in ("report.json", "summary.md", "report.html"):
            assert "SYNTHETIC_PRIVATE_TEXT" not in (folder / "report" / name).read_text()
        assert hashlib.sha256(source.read_bytes()).hexdigest() == before
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "install_skill.py"), "--destination", str(folder / "skill")], capture_output=True)
        assert result.returncode != 0
        # A portable installed skill must discover its persistent source registry.
        home = folder / "home"
        home.mkdir()
        env = dict(os.environ, HOME=str(home), USERPROFILE=str(home))
        mounted = folder / "mounted"
        mounted.mkdir()
        (mounted / "copy.jsonl").write_bytes(source.read_bytes())
        for command in (["sources", "local", "off"], ["sources", "add-path", "work", str(mounted)],
                        ["--all", "--output", str(folder / "collected")]):
            subprocess.run([sys.executable, str(runner)] + command, check=True, capture_output=True, env=env, cwd=str(folder))
        combined = json.loads((folder / "collected/report.json").read_text())
        assert combined["collection"]["successful"] == 1
        assert combined["coverage"]["unique_requests"] == 1
        # The distributed agent must run outside the checkout with no dependencies.
        bundle = folder / 'audit-agent.pyz'
        subprocess.run([sys.executable, str(ROOT / 'scripts/build_agent.py'), '--output', str(bundle)], check=True, capture_output=True)
        with zipfile.ZipFile(bundle) as z:
            assert '__main__.py' in z.namelist()
            assert 'claude_history_audit/sync.py' in z.namelist()
            assert not any('device.json' in n or '__pycache__' in n for n in z.namelist())
        help_result = subprocess.run([sys.executable, str(bundle), '--help'], check=True, capture_output=True, cwd=str(folder), env=env)
        assert b'install' in help_result.stdout and b'sync' in help_result.stdout
        subprocess.run([sys.executable, str(bundle), 'status'], check=True, capture_output=True, cwd=str(folder), env=env)
        bad_config = folder / 'device.json'
        bad_config.write_text('{}')
        failed = subprocess.run([sys.executable, str(bundle), 'sync', '--config', str(bad_config)], capture_output=True, cwd=str(folder), env=env)
        assert failed.returncode == 2
    print("PASS: portable skill install, reports, privacy, read-only input, registry, standalone zip agent and failure exit code")
    subprocess.run([sys.executable,str(ROOT/'scripts/smoke_sync.py')],check=True)


if __name__ == "__main__":
    main()
