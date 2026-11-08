"""SMB3 failure modes using Samba and unprivileged userspace smbclient."""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import time
import unittest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
LABEL = 'sqlite-csi.smb-protocol'


class Fixture:
    def __init__(self, image):
        self.image = image
        self.name = 'smb-protocol-' + secrets.token_hex(4)
        self.root = REPO / '.e2e' / self.name
        self.root.mkdir(parents=True, mode=0o700)
        self.work = self.root / 'client'
        self.work.mkdir(mode=0o700)
        self.artifacts = REPO / '.e2e/artifacts' / self.name
        self.artifacts.mkdir(parents=True)
        self.containers = []
        self.network = self.server = None
        self.observations = []

    def run(self, *args, check=True, timeout=30):
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        if check and result.returncode:
            raise RuntimeError(args[0] + ': ' + result.stderr[-2000:])
        return result if not check else result.stdout.strip()

    def owned(self, identity, network=False):
        command = ('docker', 'network', 'inspect') if network else ('docker', 'inspect')
        item = json.loads(self.run(*command, identity))[0]
        labels = item['Labels'] if network else item['Config']['Labels']
        if item['Id'] != identity or labels.get(LABEL) != self.name:
            raise RuntimeError('Refusing a foreign Docker object')
        return item

    def create(self, *args):
        identity = self.run('docker', 'create', '--pull=never', '--network', self.network,
                            '--label', LABEL + '=' + self.name, *args)
        self.containers.append(identity)
        return identity

    def remove(self, identity):
        self.owned(identity)
        self.run('docker', 'rm', '-f', identity)
        self.containers.remove(identity)

    def wait_port(self, identity, port):
        deadline = time.monotonic() + 20
        while True:
            item = self.owned(identity)
            if not item['State']['Running']:
                raise RuntimeError('Test endpoint exited before becoming ready')
            address = item['NetworkSettings']['Networks'][self.name]['IPAddress']
            try:
                with socket.create_connection((address, port), timeout=0.5):
                    return address
            except OSError:
                if time.monotonic() > deadline:
                    raise RuntimeError('Test endpoint did not become ready')
                time.sleep(0.1)

    def setup(self):
        secret = secrets.token_hex(32)
        for name, password in [('auth', secret), ('bad-auth', secrets.token_hex(32))]:
            p = self.work / name
            p.write_text('username = smbtest\npassword = ' + password + '\n')
            p.chmod(0o600)
        password = self.root / 'password'
        password.write_text(secret + '\n')
        password.chmod(0o600)
        export = self.root / 'export'
        export.mkdir(mode=0o777)
        export.chmod(0o777)
        occupied = []
        ids = self.run('docker', 'network', 'ls', '-q').splitlines()
        for item in json.loads(self.run('docker', 'network', 'inspect', *ids)):
            occupied.extend(ipaddress.ip_network(c['Subnet']) for c in (item['IPAM'].get('Config') or []) if c.get('Subnet'))
        for route in json.loads(self.run('ip', '-j', '-4', 'route', 'show')):
            if route.get('dst') not in (None, 'default'):
                occupied.append(ipaddress.ip_network(route['dst'], strict=False))
        subnet = next(ipaddress.ip_network(f'10.253.{n}.0/24') for n in range(1, 255)
                      if not any(ipaddress.ip_network(f'10.253.{n}.0/24').overlaps(p) for p in occupied if p.version == 4))
        self.network = self.run('docker', 'network', 'create', '--subnet', str(subnet), '--label', LABEL + '=' + self.name, self.name)
        subnet = ipaddress.ip_network(self.owned(self.network, True)['IPAM']['Config'][0]['Subnet'])
        self.address = str(subnet.network_address + 2)
        self.server = self.create('--ip', self.address, '--restart=no', '--mount',
            'type=bind,src=' + str(export) + ',dst=/export', '--mount',
            'type=bind,src=' + str(password) + ',dst=/run/password,readonly', self.image)
        self.run('docker', 'start', self.server)
        self.wait_port(self.server, 445)
        self.image_id = self.run('docker', 'image', 'inspect', '--format', '{{.Id}}', self.image)
        self.packages = self.run('docker', 'exec', self.server, 'dpkg-query', '-W', 'samba', 'smbclient')
        self.faults = {}
        for mode in ('reset', 'silence'):
            identity = self.create('--cap-drop=ALL', '--read-only', '--mount',
                'type=bind,src=' + str(HERE / 'fault_endpoint.py') + ',dst=/fault.py,readonly',
                '--entrypoint=python3', self.image, '/fault.py', mode)
            self.run('docker', 'start', identity)
            self.faults[mode] = (identity, self.wait_port(identity, 1445))
        self.payload = hashlib.sha256(b'smb protocol fixture').digest() * 2048
        (self.work / 'input.bin').write_bytes(self.payload)
        assert self.client('quit')['code'] == 0, 'Authenticated baseline failed'

    def client(self, command, *, share='rw', auth='auth', address=None, port=445):
        address = address or self.address
        identity = self.create('--cap-drop=ALL', '--read-only', '--user', f'{os.getuid()}:{os.getgid()}',
            '--env', 'LC_ALL=C', '--tmpfs', '/tmp:rw,mode=1777', '--mount',
            'type=bind,src=' + str(self.work) + ',dst=/work', '--entrypoint=smbclient', self.image,
            '//' + address + '/' + share, '-I', address, '-p', str(port), '-A', '/work/' + auth,
            '-m', 'SMB3', '-t', '2', '--option=cache directory=/tmp', '-c', command)
        started = time.monotonic()
        try:
            result = self.run('docker', 'start', '-a', identity, check=False, timeout=30)
            info = self.owned(identity)
            state = info['State']
            if (state['Status'] != 'exited' or state['OOMKilled'] or state['Error']
                    or result.returncode != state['ExitCode']):
                raise RuntimeError('Docker failed to run client: ' + result.stderr[-2000:])
            output = result.stdout + result.stderr
            observation = {'code': state['ExitCode'], 'statuses': sorted(set(re.findall(r'NT_STATUS_[A-Z0-9_]+', output))),
                           'seconds': round(time.monotonic() - started, 3), 'output': output[-2500:]}
            self.observations.append(observation)
            return observation
        finally:
            self.remove(identity)

    def close(self):
        errors = []
        try:
            for identity in list(reversed(self.containers)):
                try:
                    self.remove(identity)
                except Exception as error:
                    errors.append(str(error))
            if self.network:
                try:
                    self.owned(self.network, True)
                    self.run('docker', 'network', 'rm', self.network)
                    self.network = None
                except Exception as error:
                    errors.append(str(error))
        finally:
            for path in (self.work / 'auth', self.work / 'bad-auth', self.root / 'password'):
                path.unlink(missing_ok=True)
        if errors:
            raise RuntimeError('Cleanup failed: ' + '; '.join(errors))


def cases(fixture):
    class ProtocolTests(unittest.TestCase):
        def success(self, command):
            result = fixture.client(command)
            self.assertEqual(result['code'], 0, result)
            self.assertFalse(result['statuses'], result)

        def test_10_authenticated_roundtrip(self):
            self.success('put /work/input.bin roundtrip.bin')
            self.success('get roundtrip.bin /work/output.bin')
            self.assertEqual((fixture.work / 'output.bin').read_bytes(), fixture.payload)

        def test_20_bad_credentials(self):
            result = fixture.client('ls', auth='bad-auth')
            self.assertNotEqual(result['code'], 0)
            self.assertIn('NT_STATUS_LOGON_FAILURE', result['statuses'], result)

        def test_30_missing_share(self):
            result = fixture.client('ls', share='missing-share')
            self.assertNotEqual(result['code'], 0)
            self.assertIn('NT_STATUS_BAD_NETWORK_NAME', result['statuses'], result)

        def test_40_readonly_share(self):
            result = fixture.client('put /work/input.bin rejected.bin', share='readonly')
            self.assertIn('NT_STATUS_ACCESS_DENIED', result['statuses'], result)
            absent = fixture.client('get rejected.bin /work/rejected.bin')
            self.assertIn('NT_STATUS_OBJECT_NAME_NOT_FOUND', absent['statuses'], absent)
            self.assertFalse((fixture.work / 'rejected.bin').exists())

        def test_50_connection_reset(self):
            identity, address = fixture.faults['reset']
            result = fixture.client('ls', address=address, port=1445)
            self.assertNotEqual(result['code'], 0)
            self.assertTrue(set(result['statuses']) & {'NT_STATUS_CONNECTION_DISCONNECTED', 'NT_STATUS_CONNECTION_RESET'}, result)
            logs = fixture.run('docker', 'logs', identity)
            self.assertIn('SMB_REQUEST_RECEIVED', logs)

        def test_60_negotiation_timeout(self):
            identity, address = fixture.faults['silence']
            result = fixture.client('ls', address=address, port=1445)
            self.assertNotEqual(result['code'], 0)
            timed_out = 'NT_STATUS_IO_TIMEOUT' in result['statuses'] or re.search(r'^Protocol negotiation \(with timeout \d+ ms\) timed out against server ' + re.escape(address) + r'\s*$', result['output'], re.M)
            self.assertTrue(timed_out, result)
            self.assertIn('SMB_REQUEST_RECEIVED', fixture.run('docker', 'logs', identity))

        def test_70_server_unavailable(self):
            fixture.owned(fixture.server)
            fixture.run('docker', 'stop', fixture.server)
            try:
                result = fixture.client('ls')
                self.assertNotEqual(result['code'], 0)
                allowed = {'NT_STATUS_CONNECTION_REFUSED', 'NT_STATUS_HOST_UNREACHABLE', 'NT_STATUS_IO_TIMEOUT'}
                self.assertTrue(set(result['statuses']) & allowed, result)
            finally:
                fixture.run('docker', 'start', fixture.server)
                fixture.wait_port(fixture.server, 445)

        def test_80_restart_preserves_file(self):
            self.success('put /work/input.bin durable.bin')
            fixture.owned(fixture.server)
            fixture.run('docker', 'stop', fixture.server)
            fixture.run('docker', 'start', fixture.server)
            fixture.wait_port(fixture.server, 445)
            self.success('get durable.bin /work/durable.bin')
            self.assertEqual((fixture.work / 'durable.bin').read_bytes(), fixture.payload)
    return unittest.defaultTestLoader.loadTestsFromTestCase(ProtocolTests)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='sqlite-smb-protocol:local')
    args = parser.parse_args()
    fixture = Fixture(args.image)
    error, result = None, None
    try:
        fixture.setup()
        result = unittest.TextTestRunner(verbosity=2, failfast=True).run(cases(fixture))
        if result.testsRun != 8 or not result.wasSuccessful():
            raise RuntimeError('SMB protocol failure-mode suite failed')
    except Exception as caught:
        error = caught
        print('FAIL ' + str(caught), flush=True)
    finally:
        for identity in fixture.containers:
            try:
                fixture.owned(identity)
                log = fixture.run('docker', 'logs', identity, check=False)
                (fixture.artifacts / (identity[:12] + '.log')).write_text(log.stdout + log.stderr)
            except Exception:
                pass
        try:
            fixture.close()
        except Exception as cleanup_error:
            error = error or cleanup_error
            print('FAIL cleanup: ' + str(cleanup_error), flush=True)
        digest = hashlib.sha256()
        for name in ('Dockerfile', 'server.conf', 'server.sh', 'run.py', 'fault_endpoint.py'):
            digest.update(name.encode() + b'\x00' + (HERE / name).read_bytes())
        (fixture.artifacts / 'result.json').write_text(json.dumps({'result': 'FAIL' if error else 'PASS',
            'testsRun': result.testsRun if result else 0, 'error': str(error) if error else None,
            'observations': fixture.observations, 'image': args.image,
            'imageID': getattr(fixture, 'image_id', None), 'packages': getattr(fixture, 'packages', None),
            'testSourceSHA256': digest.hexdigest(),
            'scope': 'SMB3 userspace protocol; excludes CSI, kernel mounts, SQLite, fencing'}, indent=2))
    if error:
        raise SystemExit(1)
    print('PASS 8 SMB protocol cases; artifacts: ' + str(fixture.artifacts), flush=True)


if __name__ == '__main__':
    main()
