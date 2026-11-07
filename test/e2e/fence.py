"""Test-only independent fencing authority for this run's Docker workers."""
import datetime
import hmac
import json
import re
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FenceServer:
    def __init__(self, mappings, token, replica_root, certificate, key):
        self.mappings = mappings
        self.token = token
        self.replica_root = replica_root
        self.deny = True
        self.hide_volume = None
        self.events = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, data):
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                if self.path != '/v1/fence':
                    return self.reply(404, {})
                if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer '+owner.token):
                    return self.reply(401, {})
                if owner.deny:
                    owner.events.append('denied')
                    return self.reply(503, {})
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    assert 0 < size <= 4096
                    request = json.loads(self.rfile.read(size))
                    mapping = owner.mappings[request['nodeName']]
                    assert request['nodeUID'] == mapping['nodeUID']
                    assert request['machineID'] == mapping['machineID']
                    assert re.fullmatch(r'r-[a-f0-9]{32}', request['operationID'])
                    assert re.fullmatch(r'[a-f0-9]{64}', request['challenge'])
                    machine = json.loads(subprocess.check_output(['docker', 'inspect', mapping['containerID']], text=True))[0]
                    assert machine['Id'] == mapping['containerID']
                    assert machine['HostConfig']['RestartPolicy']['Name'] == 'no'
                    if machine['State']['Running']:
                        subprocess.run(['docker', 'kill', '--signal=KILL', mapping['containerID']],
                                       check=True, capture_output=True)
                    machine = json.loads(subprocess.check_output(['docker', 'inspect', mapping['containerID']], text=True))[0]
                    assert not machine['State']['Running']
                    if owner.hide_volume:
                        assert re.fullmatch(r'v-[a-f0-9]{32}', owner.hide_volume)
                        archive = owner.replica_root/owner.hide_volume/'replica'
                        if archive.exists():
                            archive.rename(archive.with_name('.held-replica'))
                    owner.events.append('confirmed-off:'+request['nodeName'])
                    return self.reply(200, dict(request, state='off', restartInhibited=True,
                                               observedAt=datetime.datetime.now(datetime.timezone.utc).isoformat()))
                except Exception as error:
                    owner.events.append('error:'+type(error).__name__)
                    return self.reply(500, {})

        self.server = ThreadingHTTPServer(('0.0.0.0', 0), Handler)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(str(certificate), str(key))
        self.server.socket = tls.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)
