#!/usr/bin/env python3
"""Run the distributed agent against an ephemeral, trusted loopback HTTPS receiver."""
import hashlib
import json
import os
import ssl
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def main():
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp)
        cert_config=root/'cert.cnf'
        cert_config.write_text('[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=ext\n[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\nbasicConstraints=critical,CA:TRUE\n')
        key=root/'key.pem';cert=root/'cert.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-config',str(cert_config)],check=True,capture_output=True)
        received=[]
        class Receiver(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                received.append((self.path,dict(self.headers),data))
                out=json.dumps({'ok':True,'complete':data['part']==data['parts']-1}).encode()
                self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(out)));self.end_headers();self.wfile.write(out)
        server=HTTPServer(('127.0.0.1',0),Receiver)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cert,key)
        server.socket=context.wrap_socket(server.socket,server_side=True)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            source=root/'history.jsonl'
            rows=[{'type':'assistant','sessionId':'PRIVATE_SESSION','timestamp':'2026-10-01T00:00:00Z','message':{'id':'PRIVATE_ID_'+str(i),'model':'claude-sonnet-4-6','content':[{'type':'text','text':'PRIVATE_TRANSCRIPT'}],'usage':{'input_tokens':100,'output_tokens':20,'cache_creation_input_tokens':200,'cache_read_input_tokens':1000}}} for i in range(501)]
            source.write_text(''.join(json.dumps(row)+'\n' for row in rows))
            before=hashlib.sha256(source.read_bytes()).hexdigest()
            config={'version':1,'origin':'https://localhost:'+str(server.server_port),'device_id':'aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa','token':'b'*64,'sync_salt':'c'*64,'platform_access':'SYNTHETIC-PLATFORM-CREDENTIAL'}
            config_path=root/'device.json';config_path.write_text(json.dumps(config))
            bundle=root/'audit-agent.pyz'
            subprocess.run([sys.executable,str(ROOT/'scripts/build_agent.py'),'--output',str(bundle)],check=True,capture_output=True)
            home=root/'home';home.mkdir()
            # Apple's system Python can use Keychain instead of SSL_CERT_FILE.
            # Add our temporary test CA to the child's default context; keep
            # certificate-chain and hostname verification enabled throughout.
            (root/'sitecustomize.py').write_text('import os, ssl\n_original=ssl._create_default_https_context\ndef _trusted_test_context(*args, **kwargs):\n    context=_original(*args, **kwargs)\n    context.load_verify_locations(os.environ["AUDIT_TEST_CA_FILE"])\n    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname\n    return context\nssl._create_default_https_context=_trusted_test_context\n')
            env=dict(os.environ,HOME=str(home),USERPROFILE=str(home),SSL_CERT_FILE=str(cert),AUDIT_TEST_CA_FILE=str(cert),PYTHONPATH=str(root),NO_PROXY='localhost,127.0.0.1',no_proxy='localhost,127.0.0.1')
            result=subprocess.run([sys.executable,str(bundle),'install','--config',str(config_path),'--source',str(source)],env=env,cwd=str(home),capture_output=True,timeout=30)
            assert result.returncode==0, 'Portable HTTPS installation/sync failed: '+result.stderr.decode()
            status=json.loads((home/'.claude-history-audit/device/status.json').read_text())
            assert status['requests']==501 and status['parts']==2 and status['last_success']
            assert len(received)==2
            assert [len(r[2]['records']) for r in received]==[500,1]
            for path,headers,data in received:
                headers={k.lower():v for k,v in headers.items()}
                assert path=='/api/sync'
                assert headers['authorization']=='Bearer '+config['token']
                assert headers['oai-sites-authorization']=='Bearer '+config['platform_access']
                encoded=json.dumps(data)
                assert not any(s in encoded for s in ['PRIVATE_SESSION','PRIVATE_ID','PRIVATE_TRANSCRIPT',str(source),config['token']])
            assert hashlib.sha256(source.read_bytes()).hexdigest()==before
            assert config['token'] not in result.stdout.decode()+result.stderr.decode()
        finally:
            server.shutdown();thread.join(timeout=5);server.server_close()
    print('PASS: real standalone agent install, trusted HTTPS, 501 records in two parts, authentication, privacy, completion and saved status')

if __name__=='__main__':main()
