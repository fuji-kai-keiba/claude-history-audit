import copy
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from claude_history_audit import sync
from claude_history_audit.audit import audit


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/'history';self.source.mkdir()
        self.config={'version':1,'origin':'https://audit.example.test','device_id':'aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa','token':'b'*64,'sync_salt':'c'*64,'platform_access':'SYNTHETIC-PLATFORM-CREDENTIAL'}
        self.row={'type':'assistant','sessionId':'PRIVATE_SESSION_ID','timestamp':'2026-10-01T00:00:00Z','message':{'id':'PRIVATE_MESSAGE_ID','model':'claude-sonnet-4-6','content':[{'type':'text','text':'PRIVATE_TRANSCRIPT_TEXT'}],'usage':{'input_tokens':100,'output_tokens':20,'cache_creation_input_tokens':200,'cache_read_input_tokens':1000}}}
        self.log=self.source/'private-project.jsonl';self.log.write_text(json.dumps(self.row)+'\n')
        self.now=datetime(2026,10,5,tzinfo=timezone.utc)

    def test_sync_ids_are_stable_across_devices_and_all_fields_are_allowlisted(self):
        first=sync.payloads(self.config,[self.source],now=self.now)[0]
        second=sync.payloads(dict(self.config,device_id='other-device'),[self.source],now=self.now)[0]
        self.assertEqual(first['records'],second['records'])
        self.assertNotEqual(first['run_id'],second['run_id'])
        self.assertEqual(len(first['records'][0]['id']),64)
        self.assertEqual(set(first['records'][0]),{'id','session','timestamp','model','input','output','cache_write','cache_read','complete'})
        text=json.dumps(first)
        for secret in ['PRIVATE_SESSION_ID','PRIVATE_MESSAGE_ID','PRIVATE_TRANSCRIPT_TEXT',str(self.source),'private-project','SYNTHETIC-PLATFORM-CREDENTIAL',self.config['token']]:
            self.assertNotIn(secret,text)
        other=sync.payloads(dict(self.config,sync_salt='d'*64),[self.source],now=self.now)[0]
        self.assertNotEqual(first['records'][0]['id'],other['records'][0]['id'])

    def test_streaming_duplicate_usage_is_not_added_and_input_is_unchanged(self):
        before=hashlib.sha256(self.log.read_bytes()).hexdigest()
        p=sync.payloads(self.config,[self.source],now=self.now)[0]
        self.assertEqual(p['summary']['requests'],1)
        self.assertEqual(before,hashlib.sha256(self.log.read_bytes()).hexdigest())
        with self.log.open('a') as f:f.write(json.dumps(self.row)+'\n')
        self.assertEqual(sync.payloads(self.config,[self.source],now=self.now)[0]['records'],p['records'])

    def test_batches_and_empty_history(self):
        with self.log.open('w') as f:
            for i in range(501):
                r=copy.deepcopy(self.row);r['message']['id']=str(i);f.write(json.dumps(r)+'\n')
        chunks=sync.payloads(self.config,[self.source],now=self.now)
        self.assertEqual([len(c['records']) for c in chunks],[500,1])
        self.assertEqual(chunks[0]['run_id'],chunks[1]['run_id'])
        self.assertEqual(chunks[0]['summary']['requests'],501)
        self.log.unlink()
        empty=sync.payloads(self.config,[self.source],now=self.now)
        self.assertEqual(empty[0]['summary']['requests'],0)
        self.assertIsNone(empty[0]['summary']['first_record'])
        with self.assertRaises(ValueError):sync.payloads(self.config,[self.root/'missing'])

    def test_config_rejects_insecure_or_credential_bearing_url(self):
        path=self.root/'device.json'
        for origin in ['http://audit.example.test','https://user:pass@audit.example.test','https://audit.example.test/evil','https://audit.example.test?query=x']:
            path.write_text(json.dumps(dict(self.config,origin=origin)))
            with self.assertRaises(ValueError):sync.read_device(path)
        path.write_text(json.dumps(self.config));self.assertEqual(sync.read_device(path)['origin'],self.config['origin'])

    def test_http_transport_sends_only_payload_and_rejects_redirects(self):
        payload=sync.payloads(self.config,[self.source],now=self.now)[0]
        class Opener:
            def open(self,request,timeout):
                self.request=request
                self.timeout=timeout
                return io.BytesIO(b'{"ok":true,"complete":true}')
        opener=Opener();self.assertTrue(sync.send(self.config,payload,opener)['complete'])
        self.assertEqual(opener.request.full_url,self.config['origin']+'/api/sync')
        self.assertEqual(json.loads(opener.request.data),payload)
        self.assertEqual(opener.request.get_header('Authorization'),'Bearer '+self.config['token'])
        with self.assertRaises(ValueError):sync.NoRedirect().redirect_request(None,None,302,'',{},'https://other.test')

    def test_failed_upload_does_not_mark_success_and_does_not_expose_http_body(self):
        status=self.root/'status.json'
        chunks=sync.payloads(self.config,[self.source],now=self.now)
        with patch.object(sync,'payloads',return_value=chunks),patch.object(sync,'send',side_effect=ValueError('failed')):
            with self.assertRaises(ValueError):sync.synchronize(self.config,status_path=status)
        self.assertFalse(status.exists())
        class Opener:
            def open(self,*args,**kwargs):raise urllib.error.HTTPError('https://audit.example.test',401,'bad',{},io.BytesIO(b'SECRET_BODY'))
        with self.assertRaises(ValueError) as e:sync.send(self.config,chunks[0],Opener())
        self.assertNotIn('SECRET_BODY',str(e.exception))
        with patch.object(sync,'payloads',return_value=chunks),patch.object(sync,'send',return_value={'ok':True,'complete':False}):
            with self.assertRaises(ValueError):sync.synchronize(self.config,status_path=status)
        self.assertFalse(status.exists())

    def test_success_status_has_no_authentication_data(self):
        status=self.root/'status.json'
        with patch.object(sync,'send',return_value={'ok':True,'complete':True}):sync.synchronize(self.config,[self.source],status)
        out=json.loads(status.read_text());self.assertEqual(out['requests'],1)
        self.assertEqual(set(out),{'last_success','requests','parts'})

    def test_windows_command_keeps_spaced_paths_as_arguments(self):
        result=sync.windows_command('C:\\Program Files\\Python\\pythonw.exe','C:\\User Space\\agent.pyz','C:\\User Space\\device.json')
        self.assertIn('"C:\\Program Files\\Python\\pythonw.exe"',result)
        self.assertIn('"C:\\User Space\\device.json"',result)
        self.assertNotIn(self.config['token'],result)

    def test_regular_reports_still_get_per_run_pseudonyms(self):
        first,_=audit([self.source]);second,_=audit([self.source])
        self.assertNotEqual(first['requests'][0]['id'],second['requests'][0]['id'])

    def test_windows_install_registers_current_user_task_and_saves_custom_source(self):
        config_path=self.root/'device.json';config_path.write_text(json.dumps(self.config))
        source_bundle=self.root/'download.pyz';source_bundle.write_bytes(b'SYNTHETIC_BUNDLE')
        destination=self.root/'installed'
        with patch.object(sync.sys,'platform','win32'),patch.object(sync.sys,'argv',[str(source_bundle)]),patch.object(sync,'device_home',return_value=destination),patch.object(sync.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as run:
            config,path=sync.install(config_path,[self.source])
        self.assertEqual(json.loads(path.read_text())['sources'],[str(self.source)])
        self.assertEqual((destination/'audit-agent.pyz').read_bytes(),b'SYNTHETIC_BUNDLE')
        args=run.call_args[0][0]
        self.assertEqual(args[:6],['schtasks','/Create','/SC','MINUTE','/MO','15'])
        self.assertIn('/IT',args);self.assertIn('LIMITED',args)
        self.assertNotIn(self.config['token'],' '.join(args))
        self.assertNotIn(self.config['platform_access'],' '.join(args))

    def test_scheduler_failure_is_not_reported_as_installed(self):
        config_path=self.root/'device.json';config_path.write_text(json.dumps(self.config))
        bundle=self.root/'download.pyz';bundle.write_bytes(b'SYNTHETIC_BUNDLE')
        with patch.object(sync.sys,'platform','win32'),patch.object(sync.sys,'argv',[str(bundle)]),patch.object(sync,'device_home',return_value=self.root/'installed'),patch.object(sync.subprocess,'run',return_value=SimpleNamespace(returncode=1)):
            with self.assertRaisesRegex(ValueError,'タスク登録に失敗'):
                sync.install(config_path)


if __name__=='__main__':unittest.main()
