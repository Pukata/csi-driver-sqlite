"""Cheap fault-target guard tests, before starting any Docker/Kubernetes fixture."""
import json
import unittest
from unittest.mock import Mock

from run import NasSuite


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


if __name__=='__main__': unittest.main()
