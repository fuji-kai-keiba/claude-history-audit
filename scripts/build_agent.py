#!/usr/bin/env python3
"""Build a self-contained Python zip application; never include personal settings."""
import argparse
import shutil
import tempfile
import zipapp
from pathlib import Path

p=argparse.ArgumentParser()
p.add_argument('--output',type=Path,required=True)
a=p.parse_args()
root=Path(__file__).resolve().parents[1]
a.output.parent.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory() as t:
 shutil.copytree(root/'app/claude_history_audit',Path(t)/'claude_history_audit',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
 (Path(t)/'__main__.py').write_text('from claude_history_audit.sync import main\nraise SystemExit(main())\n',encoding='utf-8')
 zipapp.create_archive(t,target=a.output,compressed=True)
print('Built portable audit agent.')
