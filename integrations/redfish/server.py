"""Independent Redfish fencing authority. Never run on a machine it fences."""
import argparse
import base64
import datetime
import hmac
import json
import logging
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Redirects are forbidden')


def read_secret(path):
    value = Path(path).read_text().strip()
    if not value or '\n' in value or '\r' in value:
        raise ValueError('Invalid secret file')
    return value


class Authority:
    def __init__(self, config):
        if config.get('manualPowerOnOnly') is not True or not config.get('nodes'):
            raise ValueError('Explicit manual-power-on guarantee and node mappings required')
        self.config = config
        self.lock = threading.Lock()
        seen = set()
        for name, node in config['nodes'].items():
            url = urllib.parse.urlsplit(node['endpoint'])
            if (not name or not node['nodeUID'] or url.scheme != 'https' or not url.netloc
                    or url.username or url.password or url.query or url.fragment
                    or url.path not in ('', '/') or not node['uuid']
                    or node['uuid'] in seen
                    or not re.fullmatch(r'/redfish/v1/Systems/[A-Za-z0-9_.-]+', node['systemPath'])):
                raise ValueError('Invalid or duplicate node mapping')
            seen.add(node['uuid'])

    def request(self, node, method, path, body=None):
        # Trust only configured paths on this exact BMC; ignore action URLs from responses.
        context = ssl.create_default_context(cafile=node['caFile'])
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}),
                                            urllib.request.HTTPSHandler(context=context))
        auth = node['username']+':'+read_secret(node['passwordFile'])
        request = urllib.request.Request(node['endpoint'].rstrip('/')+path,
                    data=json.dumps(body).encode() if body is not None else None, method=method,
                    headers={'Authorization':'Basic '+base64.b64encode(auth.encode()).decode(),
                             'Content-Type':'application/json'})
        with opener.open(request, timeout=5) as response:
            data = response.read(65537)
            if len(data) > 65536:
                raise ValueError('BMC response too large')
            return json.loads(data) if data else {}

    def inspect(self, node):
        state = self.request(node, 'GET', node['systemPath'])
        if state.get('UUID') != node['uuid'] or state.get('PowerRestorePolicy') != 'AlwaysOff':
            raise ValueError('BMC identity or power-restore policy mismatch')
        return state

    def fence(self, request):
        if (set(request) != {'nodeName','nodeUID','machineID','operationID','challenge'}
                or not all(isinstance(v, str) for v in request.values())
                or not re.fullmatch(r'r-[a-f0-9]{32}', request['operationID'])
                or not re.fullmatch(r'[a-f0-9]{64}', request['challenge'])):
            raise ValueError('Invalid request')
        node = self.config['nodes'][request['nodeName']]
        if request['nodeUID'] != node['nodeUID'] or request['machineID'] != 'redfish:'+node['uuid']:
            raise ValueError('Node identity mismatch')
        with self.lock:
            # Re-query on every retry. Never treat a cached successful operation as proof.
            state = self.inspect(node)
            if state.get('PowerState') != 'Off':
                action = state.get('Actions', {}).get('#ComputerSystem.Reset', {})
                if 'ForceOff' not in action.get('ResetType@Redfish.AllowableValues', []):
                    raise ValueError('BMC does not advertise ForceOff')
                self.request(node, 'POST', node['systemPath']+'/Actions/ComputerSystem.Reset',
                             {'ResetType':'ForceOff'})
            deadline = time.monotonic()+25
            while True:
                if self.inspect(node).get('PowerState') == 'Off':
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError('Power off not confirmed')
                time.sleep(0.25)
            # Restart inhibition is conditional on exclusive manual power control,
            # not a claim that AlwaysOff blocks API commands, WOL or watchdogs.
            return dict(request, state='off', restartInhibited=True,
                        observedAt=datetime.datetime.now(datetime.timezone.utc).isoformat())


def make_server(authority, address, certificate, key):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log headers or request bodies.

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
            try:
                self.connection.settimeout(5)
                if not hmac.compare_digest(self.headers.get('Authorization',''),
                                          'Bearer '+read_secret(authority.config['tokenFile'])):
                    return self.reply(401, {})
                size = int(self.headers.get('Content-Length','0'))
                if not 0 < size <= 4096 or self.headers.get('Transfer-Encoding'):
                    return self.reply(400, {})
                request = json.loads(self.rfile.read(size))
                proof = authority.fence(request)
                logging.info('Confirmed fence node=%s operation=%s', request['nodeName'], request['operationID'])
                return self.reply(200, proof)
            except Exception as error:
                logging.warning('Fence refused: %s', type(error).__name__)
                return self.reply(503, {})

    server = ThreadingHTTPServer(address, Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(certificate, key)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--certificate', required=True)
    parser.add_argument('--key', required=True)
    parser.add_argument('--port', type=int, default=9443)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    authority = Authority(json.loads(Path(args.config).read_text()))
    make_server(authority, ('0.0.0.0', args.port), args.certificate, args.key).serve_forever()


if __name__ == '__main__':
    main()
