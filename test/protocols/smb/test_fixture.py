"""Fast guards for destructive cleanup and failure classification; no Docker."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location('smb_protocol_fixture', Path(__file__).with_name('run.py'))
protocol = importlib.util.module_from_spec(spec)
spec.loader.exec_module(protocol)


class FixtureTests(unittest.TestCase):
    def fixture(self):
        f = object.__new__(protocol.Fixture)
        f.name = 'owned-run'
        f.containers = ['owned-id']
        f.network = None
        return f

    def test_foreign_container_is_never_removed(self):
        f = self.fixture()
        f.run = Mock(return_value=json.dumps([{'Id': 'owned-id', 'Config': {'Labels': {protocol.LABEL: 'foreign-run'}}}]))
        with self.assertRaises(RuntimeError):
            f.remove('owned-id')
        self.assertEqual(f.run.call_count, 1)
        self.assertEqual(f.containers, ['owned-id'])

    def test_network_id_and_label_must_both_match(self):
        f = self.fixture()
        f.run = Mock(return_value=json.dumps([{'Id': 'different-id', 'Labels': {protocol.LABEL: f.name}}]))
        with self.assertRaises(RuntimeError):
            f.owned('owned-id', network=True)
        self.assertEqual(f.run.call_args.args, ('docker', 'network', 'inspect', 'owned-id'))

    def test_cleanup_failure_still_removes_credentials(self):
        f = self.fixture()
        with tempfile.TemporaryDirectory() as temp:
            f.root = Path(temp)
            f.work = f.root / 'client'
            f.work.mkdir()
            paths = [f.work / 'auth', f.work / 'bad-auth', f.root / 'password']
            for path in paths:
                path.write_text('test secret')
            unrelated = f.root / 'preserve'
            unrelated.write_text('unrelated')
            f.remove = Mock(side_effect=RuntimeError('foreign object'))
            with self.assertRaises(RuntimeError):
                f.close()
            self.assertTrue(all(not p.exists() for p in paths))
            self.assertEqual(unrelated.read_text(), 'unrelated')

    def test_client_uses_auth_file_without_privileged_mounts(self):
        f = self.fixture()
        f.image, f.address, f.network = 'image', '127.0.0.1', 'network'
        f.work, f.observations = Path('/test-client'), []
        f.create = Mock(return_value='client-id')
        f.owned = Mock(return_value={'State': {'Status': 'exited', 'ExitCode': 1, 'OOMKilled': False, 'Error': ''}})
        f.run = Mock(return_value=subprocess.CompletedProcess([], 1, 'NT_STATUS_LOGON_FAILURE', ''))
        f.remove = Mock()
        result = f.client('ls', auth='bad-auth')
        args = f.create.call_args.args
        self.assertNotIn('--privileged', args)
        self.assertIn('--cap-drop=ALL', args)
        self.assertEqual(args[args.index('-A') + 1], '/work/bad-auth')
        self.assertEqual(result['statuses'], ['NT_STATUS_LOGON_FAILURE'])
        f.remove.assert_called_once_with('client-id')

    def test_docker_start_failure_cannot_pass_as_success(self):
        f = self.fixture()
        f.image, f.address, f.network = 'image', '127.0.0.1', 'network'
        f.work, f.observations = Path('/test-client'), []
        f.create = Mock(return_value='client-id')
        f.owned = Mock(return_value={'State': {'Status': 'created', 'ExitCode': 0, 'OOMKilled': False, 'Error': ''}})
        f.run = Mock(return_value=subprocess.CompletedProcess([], 1, '', 'start failed'))
        f.remove = Mock()
        with self.assertRaisesRegex(RuntimeError, 'Docker failed to run client'):
            f.client('quit')
        self.assertEqual(f.observations, [])
        f.remove.assert_called_once_with('client-id')

    def test_transport_timeout_cannot_pass_as_authentication_failure(self):
        f = self.fixture()
        f.client = Mock(return_value={'code': 1, 'statuses': ['NT_STATUS_IO_TIMEOUT']})
        test = next(t for t in protocol.cases(f) if t._testMethodName == 'test_20_bad_credentials')
        result = unittest.TestResult()
        test.run(result)
        self.assertEqual(len(result.failures), 1)


if __name__ == '__main__':
    unittest.main()
