"""Allowlisted numeric synchronization. Never transmit transcripts or evidence maps."""
import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .audit import audit, discover, iso
from .collection import local_source


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('同期先からの転送を拒否しました。Web画面から設定を再取得してください。')


def device_home():
    if sys.platform == 'win32':
        return Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData/Local'))) / 'ClaudeHistoryAudit'
    return Path.home() / '.claude-history-audit' / 'device'


def read_device(path):
    path = Path(path).expanduser()
    if path.stat().st_size > 32768:
        raise ValueError('端末設定が大きすぎます。')
    d = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(d, dict) or d.get('version') != 1:
        raise ValueError('端末設定が不正です。')
    url = urllib.parse.urlsplit(d.get('origin', ''))
    if url.scheme != 'https' or not url.netloc or url.username or url.password or url.path not in ('', '/') or url.query or url.fragment:
        raise ValueError('同期先にはHTTPSのサイトURLが必要です。')
    try:
        uuid.UUID(d['device_id'])
    except (ValueError, KeyError, TypeError):
        raise ValueError('端末IDが不正です。') from None
    for key in ('token', 'sync_salt'):
        if not isinstance(d.get(key), str) or not re.fullmatch('[a-f0-9]{64}', d[key]):
            raise ValueError('端末認証の設定が不正です。Web画面から設定を再取得してください。')
    if not isinstance(d.get('platform_access'), str) or not d['platform_access'] or len(d['platform_access']) > 16000 or any(ord(c) < 33 for c in d['platform_access']):
        raise ValueError('サイト接続設定が不正です。')
    d['origin'] = d['origin'].rstrip('/')
    return d


def payloads(config, sources=None, now=None):
    now = now or datetime.now(timezone.utc)
    sources = sources or [local_source()]
    files, skipped, errors = discover(sources)
    if errors:
        raise ValueError('履歴の保存先にアクセスできません。--source で実際の保存先を指定してください。')
    if not files:
        records = []
        summary = dict(generated_at=iso(now), first_record=None, last_record=None,
                       files=0, sessions=0, requests=0, incomplete=0, malformed=0,
                       missing_timestamps=0, oversized=0, unreadable=0, source_errors=0,
                       skipped_symlinks=skipped, tools=0, tool_errors=0, repeated_reads=0,
                       compactions=0, large_context=0)
    else:
        report, _ = audit(sources, until=now, now=now, identity_key=bytes.fromhex(config['sync_salt']))
        records = []
        for r in report['requests']:
            u = r['usage']
            records.append({'id': r['id'].split('-', 1)[1], 'session': r['session'].split('-', 1)[1],
                'timestamp': r['timestamp'], 'model': r['model'], 'input': u['input_tokens'],
                'output': u['output_tokens'], 'cache_write': u['cache_creation_input_tokens'],
                'cache_read': u['cache_read_input_tokens'], 'complete': r['complete']})
        c, m = report['coverage'], report['metrics']
        summary = dict(generated_at=iso(now), first_record=c['first_record'], last_record=c['last_record'],
            files=c['files_found'], sessions=c['sessions'], requests=c['unique_requests'],
            incomplete=c['unique_requests']-c['complete_usage_requests'], malformed=c.get('malformed_lines', 0),
            missing_timestamps=c.get('missing_timestamps', 0), oversized=c.get('oversized_lines', 0),
            unreadable=c.get('unreadable_files', 0), source_errors=c.get('source_errors', 0),
            skipped_symlinks=c.get('skipped_symlinks', 0), tools=m['tool_calls'], tool_errors=m['tool_errors'],
            repeated_reads=m['repeated_reads'], compactions=sum(s['compactions'] for s in report['sessions']),
            large_context=sum(sum(r['usage'][k] for k in ('input_tokens','cache_creation_input_tokens','cache_read_input_tokens')) >= 50000 for r in report['requests']))
        if summary['unreadable'] or summary['source_errors']:
            raise ValueError('読み取れない履歴があります。部分的な結果で同期済みにはしません。')
    run_id = str(uuid.uuid4())
    parts = max(1, math.ceil(len(records)/500))
    return [{'version': 1, 'run_id': run_id, 'part': i, 'parts': parts, 'summary': summary,
             'records': records[i*500:(i+1)*500]} for i in range(parts)]


def send(config, payload, opener=None):
    request = urllib.request.Request(config['origin'] + '/api/sync',
        data=json.dumps(payload, ensure_ascii=True, allow_nan=False).encode(),
        headers={'Content-Type':'application/json', 'Authorization':'Bearer '+config['token'],
                 'X-Audit-Device':config['device_id'], 'OAI-Sites-Authorization':'Bearer '+config['platform_access'],
                 'User-Agent':'ClaudeHistoryAudit/0.2'}, method='POST')
    client = opener or urllib.request.build_opener(NoRedirect())
    try:
        with client.open(request, timeout=45) as response:
            data = response.read(65537)
            if len(data) > 65536:
                raise ValueError('同期サーバーの応答が大きすぎます。')
            result = json.loads(data)
            if not isinstance(result, dict) or result.get('ok') is not True:
                raise ValueError('同期が完了しませんでした。')
            return result
    except urllib.error.HTTPError as error:
        raise ValueError('同期できませんでした（HTTP ' + str(error.code) + '）。端末の登録状態とWeb画面を確認してください。') from None
    except urllib.error.URLError:
        raise ValueError('同期先へ接続できません。インターネット接続を確認してください。') from None


def private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=str(path.parent), prefix='.sync-')
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(value,f,ensure_ascii=False,indent=2)
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)


def synchronize(config, sources=None, status_path=None):
    chunks = payloads(config, sources)
    for chunk in chunks:
        result = send(config, chunk)
    if result.get('complete') is not True:
        raise ValueError('サーバー上で全件の受信を確認できませんでした。')
    status = {'last_success':iso(datetime.now(timezone.utc)), 'requests':chunks[0]['summary']['requests'], 'parts':len(chunks)}
    if status_path:
        private_json(status_path, status)
    return status


def windows_command(executable, bundle, config):
    return subprocess.list2cmdline([str(executable), str(bundle), 'sync', '--config', str(config)])


def install(config_path, source_paths=None):
    config = read_device(config_path)
    source_bundle = Path(sys.argv[0]).resolve()
    if source_bundle.suffix != '.pyz':
        raise ValueError('Web画面からダウンロードした audit-agent.pyz で導入してください。')
    folder = device_home()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    bundle = folder / 'audit-agent.pyz'
    if source_bundle != bundle:
        shutil.copyfile(source_bundle, bundle)
    if source_paths:
        config['sources'] = [str(Path(p).expanduser().absolute()) for p in source_paths]
    destination = folder / 'device.json'
    private_json(destination, config)
    if sys.platform == 'win32':
        executable = Path(sys.executable)
        background = executable.with_name('pythonw.exe')
        if background.exists(): executable = background
        task = 'ClaudeHistoryAudit-' + config['device_id']
        result = subprocess.run(['schtasks','/Create','/SC','MINUTE','/MO','15','/TN',task,
            '/TR',windows_command(executable,bundle,destination),'/RL','LIMITED','/IT','/F'],capture_output=True,stdin=subprocess.DEVNULL,timeout=30)
        if result.returncode:
            raise ValueError('ファイルの導入は完了しましたが、15分ごとのタスク登録に失敗しました。手動でsyncを実行できます。')
        print('15分ごとの自動同期を登録しました（このWindowsユーザーでログイン中）。')
    else:
        print('同期ツールを保存しました。Mac/Linuxは sync コマンドで更新できます。')
    return config, destination


def main(argv=None):
    parser=argparse.ArgumentParser(description='監査Web画面へ数値だけを同期する端末ツール')
    parser.add_argument('action',choices=['sync','install','status','uninstall'])
    parser.add_argument('--config',type=Path,default=device_home()/'device.json')
    parser.add_argument('--source',type=Path,action='append')
    args=parser.parse_args(argv)
    try:
        if args.action=='status':
            status=args.config.parent/'status.json'
            print(status.read_text(encoding='utf-8') if status.exists() else 'この端末の同期成功記録はまだありません。')
            return 0
        config=read_device(args.config)
        if args.action=='uninstall':
            if sys.platform=='win32':
                result=subprocess.run(['schtasks','/Delete','/TN','ClaudeHistoryAudit-'+config['device_id'],'/F'],capture_output=True)
                if result.returncode:raise ValueError('同期タスクの削除を確認できませんでした。')
            print('自動同期を停止しました。Web画面のPC一覧で接続を解除できます。')
            return 0
        config_path=args.config
        if args.action=='install':
            config,config_path=install(args.config,args.source)
        sources=args.source or [Path(p) for p in config.get('sources',[])] or None
        status=synchronize(config,sources,config_path.parent/'status.json')
        print('同期完了: '+str(status['requests'])+'応答。会話本文・パス・Claudeの認証情報は送信していません。')
        return 0
    except (OSError,ValueError,TypeError,KeyError,subprocess.TimeoutExpired) as error:
        # Local config may contain secrets. Never include its contents or HTTP bodies.
        message=str(error) if isinstance(error,ValueError) and not isinstance(error,json.JSONDecodeError) else '端末設定または履歴の読み取りに失敗しました。'
        print(message,file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
