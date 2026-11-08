"""Cheap fault-target guard tests, before starting any Docker/Kubernetes fixture."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from run import NasSuite


class FaultTargetTests(unittest.TestCase):
    def fixture(self):
        fixture = object.__new__(NasSuite)
        fixture.name, fixture.nas_id = 'owned-test', 'expected-id'
        fixture.paused = False
        machine = {'Id':'expected-id','Config':{'Labels':{'sqlite-csi.nas-test':'owned-test'}},
                   'HostConfig':{'RestartPolicy':{'Name':'no'}},'State':{'Running':True,'Paused':False}}
        return fixture, machine

    def test_pause_never_mutates_an_unowned_or_restartable_machine(self):
        for fault in ('id','label','restart'):
            with self.subTest(fault=fault):
                fixture, machine = self.fixture()
                if fault=='id': machine['Id']='other-id'
                if fault=='label': machine['Config']['Labels']['sqlite-csi.nas-test']='other-run'
                if fault=='restart': machine['HostConfig']['RestartPolicy']['Name']='always'
                fixture.run = Mock(return_value=json.dumps([machine]))
                with self.assertRaises(AssertionError): fixture.pause_nas()
                self.assertEqual(fixture.run.call_args_list[0].args,('docker','inspect','expected-id'))
                self.assertEqual(fixture.run.call_count,1,'Fault injection escaped ownership validation')

    def test_pause_and_restore_target_owned_container_and_reset_state(self):
        fixture, machine = self.fixture()
        def run(*args):
            if args == ('docker','inspect','expected-id'):
                return json.dumps([machine])
            if args == ('docker','pause','expected-id'):
                machine['State']['Paused'] = True
            elif args == ('docker','unpause','expected-id'):
                machine['State']['Paused'] = False
            else:
                self.fail('Unexpected Docker command: '+repr(args))
        fixture.run = Mock(side_effect=run)
        fixture.pause_nas()
        self.assertTrue(fixture.paused)
        fixture.restore_nas()
        self.assertFalse(fixture.paused)
        self.assertFalse(machine['State']['Paused'])
        self.assertEqual([call.args for call in fixture.run.call_args_list], [
            ('docker','inspect','expected-id'), ('docker','pause','expected-id'),
            ('docker','inspect','expected-id'), ('docker','inspect','expected-id'),
            ('docker','unpause','expected-id')])
        fixture.run.reset_mock()
        fixture.restore_nas()
        self.assertEqual([call.args for call in fixture.run.call_args_list], [('docker','inspect','expected-id')])

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
        with self.assertRaises(AssertionError): fixture.replica_fault('block-write')
        self.assertEqual(fixture.run.call_count,1)
        fixture.nas = Mock()
        fixture.volume_id = '../outside'
        with self.assertRaises(AssertionError): fixture.replica_fault('block-write')
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
            fixture.replica_fault('block-write')
            self.assertEqual(decoy.read_bytes(),b'untouched')
            self.assertTrue(archive.is_file())
            fixture.replica_fault('repair')
            self.assertEqual((archive/'snapshot').read_bytes(),b'original')



if __name__ == '__main__':
    unittest.main()
