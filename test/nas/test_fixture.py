"""Cheap fault-target guard tests, before starting any Docker/Kubernetes fixture."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from run import NasSuite
from smb import SmbSuite


class FaultTargetTests(unittest.TestCase):
    def fixture(self):
        fixture = object.__new__(NasSuite)
        fixture.name, fixture.nas_id, fixture.network_id = 'owned-test', 'expected-id', 'test-network'
        machine = {'Id':'expected-id','Config':{'Labels':{'sqlite-csi.nas-test':'owned-test'}},
                   'HostConfig':{'RestartPolicy':{'Name':'no'}},'NetworkSettings':{'Networks':{}}}
        return fixture, machine

    def test_disconnect_never_mutates_an_unowned_or_restartable_machine(self):
        for fault in ('id','label','restart'):
            with self.subTest(fault=fault):
                fixture, machine = self.fixture()
                if fault=='id': machine['Id']='other-id'
                if fault=='label': machine['Config']['Labels']['sqlite-csi.nas-test']='other-run'
                if fault=='restart': machine['HostConfig']['RestartPolicy']['Name']='always'
                fixture.run = Mock(return_value=json.dumps([machine]))
                with self.assertRaises(AssertionError): fixture.disconnect_nas()
                self.assertEqual(fixture.run.call_args_list[0].args,('docker','inspect','expected-id'))
                self.assertEqual(fixture.run.call_count,1,'Fault injection escaped ownership validation')

    def test_cleanup_never_removes_an_unowned_machine(self):
        fixture, machine = self.fixture()
        machine['Config']['Labels']={}
        fixture.run = Mock(return_value=json.dumps([machine]))
        with self.assertRaises(AssertionError): fixture.close_backend()
        self.assertEqual(fixture.run.call_count,1,'Cleanup removed a container outside this run')

    def test_replica_fault_rejects_unowned_server_and_invalid_volume(self):
        fixture, machine = self.fixture()
        fixture.volume_id = 'v-'+'a'*32
        machine['Config']['Labels'] = {}
        fixture.run = Mock(return_value=json.dumps([machine]))
        with self.assertRaises(AssertionError): fixture.replica_fault('corrupt')
        self.assertEqual(fixture.run.call_count,1)
        fixture.nas = Mock()
        fixture.volume_id = '../outside'
        with self.assertRaises(AssertionError): fixture.replica_fault('corrupt')
        self.assertEqual(fixture.run.call_count,1)

    def test_live_write_fault_uses_only_owned_publisher_protocol_mount(self):
        fixture, _ = self.fixture()
        fixture.nas = Mock()
        fixture.volume_id = 'v-'+'a'*32
        fixture.workers = ['owned-worker']
        fixture.k = Mock()
        client = {'metadata': {'name': 'publisher'}, 'spec': {'nodeName': 'owned-worker'}}
        for action in ('block-write', 'repair'):
            fixture.replica_fault(action, client)
            args = fixture.k.call_args.args
            self.assertEqual(args[:8], ('exec', '-n', 'sqlite-csi-system', 'publisher', '-c', 'driver', '--', 'python3'))
            self.assertEqual(args[-2:], (fixture.volume_id, action))
        fixture.k.reset_mock()
        client['spec']['nodeName'] = 'foreign-worker'
        with self.assertRaises(AssertionError): fixture.replica_fault('block-write', client)
        fixture.k.assert_not_called()

    def test_replica_fault_mutates_only_selected_history_and_repairs_it(self):
        fixture, _ = self.fixture()
        fixture.nas = Mock()
        fixture.image = 'test-image'
        fixture.volume_id = 'v-'+'a'*32
        with tempfile.TemporaryDirectory() as root:
            fixture.replicas = Path(root)
            volume = fixture.replicas/'pvc' / fixture.volume_id
            archive = volume/'replica'
            archive.mkdir(parents=True)
            (archive/'snapshot').write_bytes(b'original')
            decoy = fixture.replicas/'unrelated'
            decoy.write_bytes(b'untouched')
            def run(*args):
                script = args[args.index('-c')+1].replace("Path('/replicas')", 'Path('+repr(root)+')')
                subprocess.run(['python3','-c',script,args[-2],args[-1]],check=True,capture_output=True)
            fixture.run = run
            for fault in ('corrupt','incomplete','block-write'):
                fixture.replica_fault(fault)
                self.assertEqual(decoy.read_bytes(),b'untouched')
                if fault=='corrupt': self.assertNotEqual((archive/'snapshot').read_bytes(),b'original')
                elif fault=='incomplete': self.assertEqual(list(archive.iterdir()),[])
                else: self.assertTrue(archive.is_file())
                fixture.replica_fault('repair')
                self.assertEqual((archive/'snapshot').read_bytes(),b'original')


class SmbFixtureTests(FaultTargetTests):
    def fixture(self):
        fixture, machine = super().fixture()
        fixture.__class__ = SmbSuite
        return fixture, machine

    def test_password_removed_when_backend_cleanup_fails(self):
        fixture, _ = self.fixture()
        with tempfile.TemporaryDirectory() as root:
            fixture.root = Path(root)
            credential = fixture.root/'smb-password'
            credential.write_text('disposable-test-secret')
            with patch.object(NasSuite, 'close', side_effect=RuntimeError('cleanup failed')):
                with self.assertRaisesRegex(RuntimeError, 'cleanup failed'):
                    fixture.close()
            self.assertFalse(credential.exists())

    def test_mount_accepts_cifs_share_and_subdirectory(self):
        fixture, _ = self.fixture()
        fixture.nas_ip = '172.18.0.3'
        for source in ('//172.18.0.3/replicas', '//172.18.0.3/replicas/pvc-123', '//172.18.0.3/replicas[/pvc-123]'):
            with self.subTest(source=source):
                fixture.check_mount('cifs '+source)

    def test_mount_rejects_host_directory_wrong_server_and_similar_share(self):
        fixture, _ = self.fixture()
        fixture.nas_ip = '172.18.0.3'
        for mount in ('ext4 /dev/sda1[/shared]', 'cifs //172.18.0.4/replicas',
                      'cifs //172.18.0.3/replicas-other', 'nfs4 172.18.0.3:/replicas'):
            with self.subTest(mount=mount), self.assertRaises(AssertionError):
                fixture.check_mount(mount)

    def test_backend_rejects_wrong_driver(self):
        fixture, _ = self.fixture()
        fixture.nas_ip = '172.18.0.3'
        fixture.get = Mock(side_effect=[{'spec':{'volumeName':'pv'}},
            {'spec':{'csi':{'driver':'nfs.csi.k8s.io',
                            'volumeAttributes':{'source':'//172.18.0.3/replicas'}}}}])
        with self.assertRaises(AssertionError):
            fixture.verify_backend()


if __name__=='__main__': unittest.main()
