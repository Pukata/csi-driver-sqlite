"""Real NAS integration tests; faults target only this run's Docker objects."""
import argparse
import copy
import hashlib
import importlib.util
import json
import os
import re
import socket
import sys
import time
import unittest
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'e2e'))
_spec = importlib.util.spec_from_file_location('sqlite_csi_e2e',Path(__file__).resolve().parents[1]/'e2e/run.py')
_e2e = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_e2e)
Suite, REPO = _e2e.Suite, _e2e.REPO

NFS_CHART = 'https://raw.githubusercontent.com/kubernetes-csi/csi-driver-nfs/v4.13.5/charts/v4.13.5/csi-driver-nfs-4.13.5.tgz'
NFS_CHART_SHA256 = 'aace8754451ecbd87e090ec29e6cbffcf88dbd9980cecb165da8b9d0880f7fac'


class NasSuite(Suite):
    def __init__(self, image, nas_image):
        super().__init__(image)
        self.nas_image = nas_image
        self.nas_id = None
        self.nas_ip = None
        self.disconnected = False

    def node_mounts(self):
        return []  # There must be no host-directory bypass around NFS.

    def nas(self):
        machine = json.loads(self.run('docker','inspect',self.nas_id))[0]
        assert machine['Id'] == self.nas_id
        assert machine['Config']['Labels'].get('sqlite-csi.nas-test') == self.name
        assert machine['HostConfig']['RestartPolicy']['Name'] == 'no'
        return machine

    def configure_backing(self):
        state = self.root/'nas-state'
        state.mkdir()
        self.nas_id = self.run('docker','run','-d','--name',self.name+'-nas','--network',self.name,
            '--label','sqlite-csi.nas-test='+self.name,'--restart=no','--privileged',
            '--mount','type=bind,src='+str(self.replicas)+',dst=/export',
            '--mount','type=bind,src='+str(state)+',dst=/var/lib/nfs/ganesha',self.nas_image)
        self.nas_ip = self.nas()['NetworkSettings']['Networks'][self.name]['IPAddress']
        def listening():
            try:
                with socket.create_connection((self.nas_ip,2049),timeout=1): return True
            except OSError: return False
        self.wait('independent NAS NFS listener ready',listening,30)
        archive = self.root/'nfs-chart.tgz'
        with urllib.request.urlopen(NFS_CHART,timeout=30) as response:
            payload = response.read(2*1024*1024)
        assert hashlib.sha256(payload).hexdigest() == NFS_CHART_SHA256
        archive.write_bytes(payload)
        self.run('helm','upgrade','--install','nfs-test',str(archive),
            '--kubeconfig',str(self.kubeconfig),'--kube-context','kind-'+self.name,
            '-n','kube-system','--set','controller.replicas=1','--wait','--timeout','240s')
        self.apply({'apiVersion':'storage.k8s.io/v1','kind':'StorageClass','metadata':{'name':'test-nas'},
            'provisioner':'nfs.csi.k8s.io','parameters':{'server':self.nas_ip,'share':'/replicas','mountPermissions':'0777'},
            'reclaimPolicy':'Retain','volumeBindingMode':'Immediate',
            'mountOptions':['nfsvers=4.1','hard','timeo=10','retrans=2','actimeo=0']})
        self.apply({'apiVersion':'v1','kind':'PersistentVolumeClaim',
            'metadata':{'name':'sqlite-csi-replicas','namespace':'sqlite-csi-system'},
            'spec':{'storageClassName':'test-nas','accessModes':['ReadWriteMany'],
                    'resources':{'requests':{'storage':'5Gi'}}}})

    def verify_backend(self):
        claim = self.get('pvc','sqlite-csi-replicas','-n','sqlite-csi-system')
        pv = self.get('pv',claim['spec']['volumeName'])
        assert pv['spec']['csi']['driver']=='nfs.csi.k8s.io'
        assert pv['spec']['csi']['volumeAttributes']['server']==self.nas_ip
        for node in self.workers:
            agent = self.wait('NFS-backed node agent Ready: '+node,lambda:next((p for p in self.pods(
                'sqlite-csi-system','app=sqlite-csi-node') if p['spec']['nodeName']==node and self.ready(p)),None))
            mount = self.k('exec','-n','sqlite-csi-system',agent['metadata']['name'],'-c','driver',
                          '--','findmnt','-n','-o','FSTYPE,SOURCE','--target','/replicas')
            assert mount.split()[0]=='nfs4' and self.nas_ip+':' in mount, mount

    def verify_topology(self):
        self.verify_backend()
        self.k('patch','deployment','sqlite-csi-controller','-n','sqlite-csi-system','--type=merge',
            '-p',json.dumps({'spec':{'template':{'spec':{'nodeSelector':{
                'kubernetes.io/hostname':self.workers[1]}}}}}))
        self.wait('controller placed on surviving worker before faults',lambda:(p:=self.controller()) and p['spec']['nodeName']==self.workers[1])
        self.template = copy.deepcopy(self.get('deployment','sqlite-test','-n','sqlite-test')['spec'])
        self.template['template']['spec']['nodeSelector'].pop('kubernetes.io/hostname',None)
        self.set_volume_id()
        self.remove_application()
        self.write_topology()

    def write_topology(self):
        (self.artifacts/'topology.json').write_text(json.dumps({'backend':'NFSv4.1',
            'csiDriver':'nfs.csi.k8s.io','chartVersion':'4.13.5','chartSHA256':NFS_CHART_SHA256,
            'kubernetesNodes':3,'separateNas':True,'workerHostDirectoryBypass':False,
            'mountOptions':['nfsvers=4.1','hard','timeo=10','retrans=2','actimeo=0'],
            'nasPackages':self.run('docker','exec',self.nas_id,'dpkg-query','-W','nfs-ganesha','nfs-ganesha-vfs')},indent=2))

    def set_volume_id(self):
        claim = self.get('pvc','sqlite-test','-n','sqlite-test')
        self.pv_name = claim['spec']['volumeName']
        self.pv_uid = self.get('pv',self.pv_name)['metadata']['uid']
        self.volume_id = self.get('pv',self.pv_name)['spec']['csi']['volumeHandle']

    def create_application(self):
        # Choose the initial worker without pinning the replacement to it.
        self.k('cordon',self.workers[1])
        try:
            self._create_application()
            assert self.app()['spec']['nodeName']==self.workers[0]
        finally:
            self.k('uncordon',self.workers[1])

    def _create_application(self):
        self.apply({'apiVersion':'v1','kind':'PersistentVolumeClaim','metadata':{'name':'sqlite-test','namespace':'sqlite-test'},
            'spec':{'storageClassName':'sqlite-csi-ha','accessModes':['ReadWriteOncePod'],
                    'resources':{'requests':{'storage':'1Gi'}}}})
        self.apply({'apiVersion':'apps/v1','kind':'Deployment','metadata':{'name':'sqlite-test','namespace':'sqlite-test'},
                    'spec':copy.deepcopy(self.template)})
        self.wait('fresh case writer Ready',self.app)
        self.set_volume_id()

    def remove_application(self):
        self.k('scale','deployment','sqlite-test','-n','sqlite-test','--replicas=0')
        self.wait('case writer fully unmounted',lambda:not self.pods('sqlite-test','app=sqlite-test'))
        self.wait('case volume cleanly released',lambda:not self.volume().get('owner'))
        self.k('delete','deployment','sqlite-test','-n','sqlite-test','--wait=true')
        self.k('delete','pvc','sqlite-test','-n','sqlite-test','--wait=true')

    def disconnect_nas(self):
        self.nas()
        self.run('docker','network','disconnect',self.network_id,self.nas_id)
        self.disconnected = True
        assert self.name not in self.nas()['NetworkSettings']['Networks']

    def restore_nas(self):
        if not self.nas_id: return
        machine = self.nas()
        if self.disconnected:
            self.run('docker','network','connect','--ip',self.nas_ip,self.network_id,self.nas_id)
            self.disconnected = False
        if not machine['State']['Running']:
            self.run('docker','start',self.nas_id)

    def close_backend(self):
        if self.nas_id:
            self.nas()
            self.run('docker','rm','--force',self.nas_id)
            self.nas_id = None

    def close(self):
        self.restore_nas()  # Keep NFS alive until kind clients have been removed.
        super().close()

    def collect(self,error):
        try:
            if self.nas_id:
                (self.artifacts/'nas.log').write_text(self.run('docker','logs','--tail','200',self.nas_id))
        except Exception: pass
        try:
            for pod in self.get('pods','-n','kube-system')['items']:
                if not any(plugin in c['image'] for c in pod['spec']['containers'] for plugin in ('nfsplugin','smbplugin')): continue
                for container in pod['spec']['containers']:
                    try:
                        log = self.k('logs','-n','kube-system',pod['metadata']['name'],'-c',container['name'],'--tail=200')
                        (self.artifacts/(pod['metadata']['name']+'-'+container['name']+'.log')).write_text(log)
                    except Exception: pass
        except Exception: pass
        super().collect(error)

    def observe_locked(self, before, old_pod_uid, seconds=8):
        """A bounded negative observation complements unit tests of the invariant."""
        end = time.monotonic()+seconds
        samples = 0
        while time.monotonic()<end:
            current = self.volume()
            assert current.get('owner')==before['owner'] and current['epoch']==before['epoch']
            ready = [p for p in self.pods('sqlite-test','app=sqlite-test') if self.ready(p)]
            assert not current['clean'], 'Unverified volume marked clean while NAS unavailable'
            assert all(p['metadata']['uid']==old_pod_uid for p in ready), 'Unsafe replacement while NAS unavailable'
            assert self.get('pv',self.pv_name)['metadata']['uid']==self.pv_uid
            samples += 1
            time.sleep(0.5)
        assert samples>=2

    def replica_fault(self, action, client=None):
        """Modify only this run's volume on its disposable server's export.

        NAS CSI provisioners create a subdirectory under the disposable export.
        Locate the unique volume there and retain the original history for repair.
        """
        self.nas()
        assert re.fullmatch(r'v-[a-f0-9]{32}', self.volume_id)
        assert action in ('corrupt', 'incomplete', 'block-write', 'repair')
        script = '''import pathlib,shutil,sys
root=pathlib.Path('/replicas')
matches=[p for p in root.rglob(sys.argv[1]) if p.is_dir() and not p.is_symlink()]
assert len(matches)==1, 'volume directory must be unique'
volume=matches[0]
assert root.resolve() in volume.resolve().parents
archive=volume/'replica'
held=volume/'.nas-test-held-replica'
assert not archive.is_symlink() and not held.is_symlink()
action=sys.argv[2]
if action=='repair':
 assert held.is_dir() and (archive.is_dir() or archive.is_file())
 if archive.is_dir(): shutil.rmtree(archive)
 else: archive.unlink()
 held.rename(archive)
else:
 assert archive.is_dir() and not held.exists()
 assert not any(p.is_symlink() for p in archive.rglob('*'))
 archive.rename(held)
 if action=='block-write':
  archive.write_bytes(b'NAS test write obstruction')
 elif action=='incomplete':
  archive.mkdir()
 else:
  shutil.copytree(held,archive)
  files=[p for p in archive.rglob('*') if p.is_file()]
  assert files, 'replica history contains no files'
  for p in files: p.write_bytes(b'corrupt replica data')
'''
        if client is not None:
            # Inject live write faults through the publisher's protocol mount.
            # Mutating the server's backing directory bypasses NFS lookup/cache
            # invalidation and can leave the client writing to the old inode.
            assert action in ('block-write', 'repair')
            assert client['spec']['nodeName'] in self.workers
            self.k('exec', '-n', 'sqlite-csi-system', client['metadata']['name'],
                   '-c', 'driver', '--', 'python3', '-c', script, self.volume_id, action)
            return
        # Samba's forced server user differs from the controller's UID. Root in
        # this throwaway helper can modify either server's files; guards above
        # and in the script restrict all mutations to this volume's history.
        self.run('docker','run','--rm','--pull=never','--network=none','--user=0:0',
                 '--read-only','--mount','type=bind,src='+str(self.replicas)+',dst=/replicas',
                 '--entrypoint=python3',self.image,'-c',script,self.volume_id,action)


def cases(s):
    class NasTests(unittest.TestCase):
        def setUp(self):
            self.case_ok = False
            s.create_application()
            self.original = s.app()
            self.expected = [self._testMethodName+'-'+str(i) for i in range(3)]
            for marker in self.expected: s.request(self.original,marker)
            s.wait('baseline records verified through a fresh replica restore',lambda:s.replica_records()==self.expected)

        def tearDown(self):
            s.restore_nas()
            # Never reset ownership to make cleanup pass. A test failure retains diagnostics.
            if not self.case_ok: return
            s.remove_application()

        def test_10_nas_restart_preserves_replica(self):
            before = s.volume()
            nas_id = s.nas()['Id']
            s.run('docker','kill','--signal=KILL',nas_id)
            self.assertFalse(s.nas()['State']['Running'])
            s.restore_nas()
            self.assertEqual(s.nas()['Id'],nas_id)
            s.wait('NAS restart preserves published records in a fresh restore',lambda:s.replica_records()==self.expected)
            s.request(self.original,'after-nas-restart')
            self.expected.append('after-nas-restart')
            s.wait('publisher resumes after NAS restart',lambda:s.replica_records()==self.expected)
            self.assertEqual(s.volume()['owner'],before['owner'])
            self.assertEqual(s.volume()['epoch'],before['epoch'])
            self.case_ok = True

        def test_20_nas_outage_blocks_clean_move_then_recovers(self):
            before = s.volume()
            s.disconnect_nas()
            s.request(self.original,'local-write-during-nas-outage')
            self.expected.append('local-write-during-nas-outage')
            self.assertEqual([r[1] for r in s.request(self.original)['records']],self.expected)
            s.k('patch','deployment','sqlite-test','-n','sqlite-test','--type=merge','-p',json.dumps({
                'spec':{'template':{'spec':{'nodeSelector':{'kubernetes.io/hostname':s.workers[1]}}}}}))
            agent = next(p for p in s.pods('sqlite-csi-system','app=sqlite-csi-node')
                         if p['spec']['nodeName']==self.original['spec']['nodeName'])
            script = "import json;print(json.dumps(json.load(open('/var/lib/sqlite-csi/"+s.volume_id+"/session.json'))['targets']))"
            s.wait('clean unpublish attempted while NAS is disconnected',lambda:s.k('exec','-n','sqlite-csi-system',
                agent['metadata']['name'],'-c','driver','--','python3','-c',script)=='{}')
            s.observe_locked(before,self.original['metadata']['uid'])
            s.restore_nas()
            replacement = s.wait('clean handover resumes after NAS returns',lambda:(p:=s.app()) and p['spec']['nodeName']==s.workers[1] and p,300)
            self.assertEqual([r[1] for r in s.request(replacement)['records']],self.expected)
            s.wait('outage writes survive clean handover and fresh replica restore',lambda:s.replica_records()==self.expected)
            self.assertEqual(s.get('pv',s.pv_name)['metadata']['uid'],s.pv_uid)
            self.case_ok = True

        def worker_failure(self,nas_outage):
            before = s.volume()
            source = self.original['spec']['nodeName']
            source_ip = s.inspect(source)['NetworkSettings']['Networks'][s.name]['IPAddress']
            if nas_outage:
                s.disconnect_nas()
                s.request(self.original,'acknowledged-after-nas-disconnect')
                self.assertIn('acknowledged-after-nas-disconnect',[r[1] for r in s.request(self.original)['records']])
            s.fencer.deny = False
            s.run('docker','network','disconnect',s.network_id,s.inspect(source)['Id'])
            s.wait('worker independently fenced',lambda:not s.inspect(source)['State']['Running'])
            if nas_outage:
                s.wait('fenced recovery waiting for replica backend',lambda:s.volume().get('recovery',{}).get('phase')=='fenced')
                s.observe_locked(before,self.original['metadata']['uid'])
            s.restore_nas()
            replacement = s.wait('replacement restores from replica',lambda:(p:=s.app()) and p['spec']['nodeName']!=source and p,300)
            records = [r[1] for r in s.request(replacement)['records']]
            self.assertEqual(records[:len(self.expected)],self.expected)
            self.assertTrue(all(v in self.expected+['acknowledged-after-nas-disconnect'] for v in records))
            self.assertEqual(s.request(replacement,path='/integrity')['integrity'],['ok'])
            self.assertGreater(s.volume()['epoch'],before['epoch'])
            self.assertEqual(s.get('pv',s.pv_name)['metadata']['uid'],s.pv_uid)
            self.assertTrue(s.volume()['replicaDirectory'].startswith('replica-r-'))
            s.request(replacement,'after-combined-recovery')
            records.append('after-combined-recovery')
            s.wait('new writer publishes verified records after combined recovery',lambda:s.replica_records()==records)
            s.run('docker','network','connect','--ip',source_ip,s.network_id,s.inspect(source)['Id'])
            s.run('docker','start',s.inspect(source)['Id'])
            s.wait('returning worker Ready under quarantine',lambda:any(c['type']=='Ready' and c['status']=='True'
                   for c in s.get('node',source)['status']['conditions']))
            self.assertTrue(s.get('node',source)['spec']['unschedulable'])
            s.audit_returned(source,self.original['metadata']['uid'])
            s.k('taint','node',source,'node.kubernetes.io/out-of-service-')
            s.k('uncordon',source)

        def test_30_worker_failure_with_nas_available(self):
            self.worker_failure(False)
            self.case_ok = True

        def test_40_worker_failure_with_nas_unavailable(self):
            self.worker_failure(True)
            self.case_ok = True

        def damaged_history(self, fault):
            before = s.volume()
            self.assertEqual(before.get('replicaDirectory','replica'),'replica')
            source = self.original['spec']['nodeName']
            source_ip = s.inspect(source)['NetworkSettings']['Networks'][s.name]['IPAddress']
            s.disconnect_nas()
            s.fencer.deny = False
            s.run('docker','network','disconnect',s.network_id,s.inspect(source)['Id'])
            s.wait('old worker independently fenced before replica damage',lambda:not s.inspect(source)['State']['Running'])
            s.replica_fault(fault)
            s.restore_nas()
            s.wait('recovery reaches fenced phase with damaged replica',lambda:s.volume().get('recovery',{}).get('phase')=='fenced')
            def failure_count():
                controller = s.controller()
                if not controller: return 0
                log = s.k('logs','-n','sqlite-csi-system',controller['metadata']['name'],'-c','driver','--tail=500')
                return sum(s.volume_id in line and 'restore failed:' in line for line in log.splitlines())
            controller = s.controller()
            self.assertIsNotNone(controller)
            port = 445 if hasattr(s, 'check_mount') else 2049
            probe = 'import socket; socket.create_connection(('+repr(s.nas_ip)+','+str(port)+'),timeout=3).close()'
            s.wait('NAS listener reachable from controller',lambda:s.k('exec','-n','sqlite-csi-system',
                controller['metadata']['name'],'-c','driver','--','python3','-c',probe) is not None)
            previous_failures = failure_count()
            s.wait('controller attempted and rejected damaged history after reconnect',lambda:failure_count()>previous_failures)
            with self.assertRaises(RuntimeError):
                s.replica_records()
            s.observe_locked(before,self.original['metadata']['uid'])
            s.replica_fault('repair')
            replacement = s.wait('repaired history restores to a replacement',lambda:(p:=s.app()) and p['spec']['nodeName']!=source and p,300)
            self.assertEqual([r[1] for r in s.request(replacement)['records']],self.expected)
            self.assertEqual(s.request(replacement,path='/integrity')['integrity'],['ok'])
            self.assertGreater(s.volume()['epoch'],before['epoch'])
            self.assertEqual(s.get('pv',s.pv_name)['metadata']['uid'],s.pv_uid)
            s.wait('repaired recovery publishes a fresh replica',lambda:s.replica_records()==self.expected)
            s.run('docker','network','connect','--ip',source_ip,s.network_id,s.inspect(source)['Id'])
            s.run('docker','start',s.inspect(source)['Id'])
            s.wait('returning worker Ready under quarantine',lambda:any(c['type']=='Ready' and c['status']=='True'
                   for c in s.get('node',source)['status']['conditions']))
            self.assertTrue(s.get('node',source)['spec']['unschedulable'])
            s.audit_returned(source,self.original['metadata']['uid'])
            s.k('taint','node',source,'node.kubernetes.io/out-of-service-')
            s.k('uncordon',source)

        def test_50_corrupt_history_blocks_recovery_until_repaired(self):
            self.damaged_history('corrupt')
            self.case_ok = True

        def test_60_incomplete_history_blocks_recovery_until_repaired(self):
            self.damaged_history('incomplete')
            self.case_ok = True

        def test_70_failed_replica_write_blocks_clean_handover(self):
            before = s.volume()
            self.assertEqual(before.get('replicaDirectory','replica'),'replica')
            agent = next(p for p in s.pods('sqlite-csi-system','app=sqlite-csi-node')
                         if p['spec']['nodeName']==self.original['spec']['nodeName'])
            s.replica_fault('block-write', agent)
            probe = '''import errno,os,sys
assert os.geteuid()==0
try:
 fd=os.open(sys.argv[1]+'/write-probe',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 os.close(fd)
 os.unlink(sys.argv[1]+'/write-probe')
except OSError as e:
 assert e.errno==errno.ENOTDIR, repr(e)
 print('ENOTDIR')
else: raise AssertionError('replica write unexpectedly succeeded')
'''
            s.wait('mounted replica rejects writes from publisher identity',lambda:s.k('exec','-n','sqlite-csi-system',
                agent['metadata']['name'],'-c','driver','--','python3','-c',probe,
                '/replicas/'+s.volume_id+'/replica')=='ENOTDIR')
            s.request(self.original,'local-write-after-replica-write-failure')
            self.expected.append('local-write-after-replica-write-failure')
            self.assertEqual([r[1] for r in s.request(self.original)['records']],self.expected)
            def publisher_failed():
                log = s.k('exec','-n','sqlite-csi-system',agent['metadata']['name'],'-c','driver',
                    '--','cat','/var/lib/sqlite-csi/'+s.volume_id+'/litestream.log')
                return 'not a directory' in log.lower()
            s.wait('replication publisher reports failed write',publisher_failed)
            s.k('patch','deployment','sqlite-test','-n','sqlite-test','--type=merge','-p',json.dumps({
                'spec':{'template':{'spec':{'nodeSelector':{'kubernetes.io/hostname':s.workers[1]}}}}}))
            script = "import json;print(json.dumps(json.load(open('/var/lib/sqlite-csi/"+s.volume_id+"/session.json'))['targets']))"
            s.wait('clean unpublish attempted with failed replica writes',lambda:s.k('exec','-n','sqlite-csi-system',
                agent['metadata']['name'],'-c','driver','--','python3','-c',script)=='{}')
            s.observe_locked(before,self.original['metadata']['uid'])
            s.replica_fault('repair', agent)
            replacement = s.wait('clean handover resumes after replica write repair',lambda:(p:=s.app()) and p['spec']['nodeName']==s.workers[1] and p,300)
            self.assertEqual([r[1] for r in s.request(replacement)['records']],self.expected)
            self.assertEqual(s.request(replacement,path='/integrity')['integrity'],['ok'])
            s.wait('repaired publisher preserves acknowledged writes in fresh restore',lambda:s.replica_records()==self.expected)
            self.assertEqual(s.get('pv',s.pv_name)['metadata']['uid'],s.pv_uid)
            self.case_ok = True
    return unittest.defaultTestLoader.loadTestsFromTestCase(NasTests)


class Result(unittest.TextTestResult):
    def startTest(self,test):
        self.started = time.monotonic()
        super().startTest(test)

    def stopTest(self,test):
        super().stopTest(test)
        self.records.append({'name':test.id(),'seconds':round(time.monotonic()-self.started,3),
            'failure':next((reason for case,reason in self.failures+self.errors if case==test),None)})


def main(suite_class=NasSuite, backend="real-nfs4", default_nas_image="sqlite-csi-nas:e2e"):
    parser = argparse.ArgumentParser()
    parser.add_argument('--image',default='sqlite-csi:e2e')
    parser.add_argument('--nas-image',default=default_nas_image)
    args = parser.parse_args()
    suite = suite_class(args.image,args.nas_image)
    error, result = None, None
    try:
        suite.setup()
        suite.verify_topology()
        result = Result(sys.stderr,True,2)
        result.records = []
        # TextTestRunner supplies the stream wrapper expected by TextTestResult.
        runner = unittest.TextTestRunner(verbosity=2,failfast=True,resultclass=lambda *args: result)
        result.stream = runner.stream
        runner.run(cases(suite))
        if result.testsRun != 7 or not result.wasSuccessful(): raise AssertionError('NAS integration testcase failed or did not run')
    except Exception as caught:
        error = caught
        print('FAIL '+str(caught),file=sys.stderr,flush=True)
    finally:
        suite.collect(error)
        root = ET.Element('testsuite',name='nas-integration',tests=str(result.testsRun if result else 1),
                          failures=str(len(result.failures) if result else 0),errors=str(len(result.errors) if result else 1))
        for record in result.records if result else []:
            case = ET.SubElement(root,'testcase',name=record['name'],time=str(record['seconds']))
            if record['failure']: ET.SubElement(case,'failure').text=record['failure']
        if result is None: ET.SubElement(ET.SubElement(root,'testcase',name='fixture'),'error').text=str(error)
        ET.ElementTree(root).write(suite.artifacts/'junit.xml',encoding='utf-8',xml_declaration=True)
        (suite.artifacts/'cases.json').write_text(json.dumps({'result':'FAIL' if error else 'PASS',
            'tests':result.records if result else [],'backend':backend,'sourceRevision':os.environ.get('GITHUB_SHA','unknown'),
            'error':str(error) if error else None},indent=2))
        suite.close()
    if error: raise SystemExit(1)


if __name__=='__main__': main()
