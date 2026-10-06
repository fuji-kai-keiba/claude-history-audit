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
from datetime import datetime, timedelta, timezone

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
        # A single installed-skill audit discovers the resume pattern, then its
        # evidence/review commands enforce completion outside the checkout.
        resumed = folder/'resume.jsonl'
        history = []
        for i, seconds in enumerate([0, 4000, 4060, 8000, 8060, 12000, 12060, 16000, 16060]):
            cold = i % 2 == 1
            write = 300000 if cold else 1000
            history.append({'type':'assistant','sessionId':'synthetic-resume',
                'timestamp':(datetime(2026,10,1,tzinfo=timezone.utc)+timedelta(seconds=seconds)).isoformat(),
                'message':{'id':'resume-'+str(i),'model':'claude-sonnet-4-6','role':'assistant',
                    'content':[{'type':'text','text':'SYNTHETIC_FINISHED_RESEARCH. Next: draft chapter two.'}],
                    'usage':{'input_tokens':0,'output_tokens':100,'cache_creation_input_tokens':write,
                             'cache_read_input_tokens':300000-write,
                             'cache_creation':{'ephemeral_1h_input_tokens':write,'ephemeral_5m_input_tokens':0}}}})
        resumed.write_text(''.join(json.dumps(r)+'\n' for r in history))
        resumed_hash=hashlib.sha256(resumed.read_bytes()).hexdigest()
        diagnosis=folder/'diagnosis'
        subprocess.run([sys.executable,str(runner),'--source',str(resumed),'--all','--output',str(diagnosis)],check=True,capture_output=True,cwd=str(folder))
        analyzed=json.loads((diagnosis/'report.json').read_text())['deep']['diagnosis']['analysis']
        assert any(f['code']=='resume_1h' and '休憩前' in f['action'] for f in analyzed['findings'])
        incomplete=subprocess.run([sys.executable,str(runner),'review','finalize',str(diagnosis)],capture_output=True,cwd=str(folder))
        assert incomplete.returncode == 2
        notes=json.loads((diagnosis/'review-notes.private.json').read_text())
        for item in notes['items']:
            packet=subprocess.run([sys.executable,str(runner),'review','evidence',str(diagnosis),'--item',item['id']],check=True,capture_output=True,cwd=str(folder))
            inspected=json.loads(packet.stdout)
            assert all(e['available'] for e in inspected['excerpts'])
            item.update(status='supported_hypothesis',evidence=[e['focus'] for e in inspected['excerpts']],
                        observation='合成履歴に完了した調査から次章の執筆へ移る記録がある。',
                        interpretation='完了した調査の文脈を持ち越している候補。必要性は未確定。',
                        alternatives='次章の根拠照合に以前の調査が必要な可能性。',
                        action='合成の次章執筆へ調査の要点と出典だけを引き継ぐ。',
                        validation='同じ根拠と章構成で総費用と修正回数を比較する。')
        (diagnosis/'review-notes.private.json').write_text(json.dumps(notes),encoding='utf-8')
        subprocess.run([sys.executable,str(runner),'review','finalize',str(diagnosis)],check=True,capture_output=True,cwd=str(folder))
        assert json.loads((diagnosis/'review-status.json').read_text())['status']=='reviewed'
        assert (diagnosis/'diagnosis-reviewed.private.md').is_file()
        assert hashlib.sha256(resumed.read_bytes()).hexdigest()==resumed_hash
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
    print("PASS: portable skill, first-pass resume diagnosis, required evidence review/finalization, privacy, read-only input, registry, standalone agent and failure exit codes")
    subprocess.run([sys.executable,str(ROOT/'scripts/smoke_sync.py')],check=True)


if __name__ == "__main__":
    main()
