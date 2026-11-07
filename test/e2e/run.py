"""Run storage scenarios in a uniquely owned kind cluster, never an active context."""
import argparse
import ipaddress
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

from fence import FenceServer

REPO = Path(__file__).resolve().parents[2]


class Suite:
    def __init__(self, image):
        self.name = 'sqlite-roam-e2e-'+secrets.token_hex(4)
        self.root = REPO/'.e2e'/self.name
        self.root.mkdir(parents=True, mode=0o700)
        self.artifacts = REPO/'.e2e/artifacts'/self.name
        self.artifacts.mkdir(parents=True)
        self.kubeconfig = self.root/'kubeconfig'
        self.env = dict(os.environ, KUBECONFIG=str(self.kubeconfig),
                        KIND_EXPERIMENTAL_DOCKER_NETWORK=self.name)
        self.image = image
        self.network_id = None
        self.created = False
        self.fencer = None
        self.steps = []
        self.replicas = self.root/'replicas'
        self.replicas.mkdir()
        self.replicas.chmod(0o777)  # disposable shared storage, writable by UID 1000

    def run(self, *args, data=None, timeout=360):
        result = subprocess.run(args, input=data, text=True, env=self.env, capture_output=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(f'{args[0]} {args[1] if len(args)>1 else ""}: {result.stderr[-3000:]}')
        return result.stdout.strip()

    def k(self, *args, data=None):
        return self.run('kubectl', '--kubeconfig', str(self.kubeconfig), '--context', 'kind-'+self.name,
                        '--request-timeout=20s', *args, data=data)

    def get(self, *args):
        return json.loads(self.k('get', *args, '-o', 'json'))

    def apply(self, obj):
        return self.k('apply', '-f', '-', data=json.dumps(obj))

    def wait(self, description, predicate, timeout=240):
        end = time.monotonic()+timeout
        last_error = ''
        while time.monotonic() < end:
            try:
                value = predicate()
                if value:
                    self.steps.append(description)
                    print('PASS '+description, flush=True)
                    return value
            except (RuntimeError, KeyError, AssertionError, ValueError, StopIteration) as error:
                last_error = str(error)
            time.sleep(2)
        raise RuntimeError('Timed out: '+description+' '+last_error)

    def pods(self, namespace, label):
        return self.get('pods', '-n', namespace, '-l', label)['items']

    @staticmethod
    def ready(pod):
        return not pod['metadata'].get('deletionTimestamp') and any(
            c['type']=='Ready' and c['status']=='True' for c in pod.get('status', {}).get('conditions', []))

    def app(self):
        return next((p for p in self.pods('sqlite-test', 'app=sqlite-test') if self.ready(p)), None)

    def controller(self):
        return next((p for p in self.pods('sqlite-roam-system', 'app=sqlite-roam-controller') if self.ready(p)), None)

    def volume(self):
        return json.loads(self.get('configmap', self.volume_id, '-n', 'sqlite-roam-system')['data']['volume.json'])

    def inspect(self, node):
        assert node in self.mappings, 'Refusing a Docker operation on a node outside this run'
        machine = json.loads(self.run('docker', 'inspect', node))[0]
        assert machine['Id'] == self.mappings[node]['containerID'], 'Worker identity changed'
        return machine

    def request(self, pod, marker=None, path='/records', local=False):
        body = json.dumps({'value': marker}).encode() if marker else None
        script = ('import urllib.request,json; r=urllib.request.Request("http://127.0.0.1:8080'+path+'",'
                  'headers={"Content-Type":"application/json"},data='+repr(body)+');'
                  'print(urllib.request.urlopen(r,timeout=10).read().decode())')
        if local:
            node = pod['spec']['nodeName']
            machine = self.inspect(node)
            containers = json.loads(self.run('docker', 'exec', machine['Id'], 'crictl',
                                            '--runtime-endpoint=unix:///run/containerd/containerd.sock', 'ps', '-o', 'json'))['containers']
            container = next(c['id'] for c in containers if c['metadata']['name']=='app' and
                             c['labels'].get('io.kubernetes.pod.uid')==pod['metadata']['uid'])
            result = self.run('docker', 'exec', machine['Id'], 'crictl',
                              '--runtime-endpoint=unix:///run/containerd/containerd.sock', 'exec', container, 'python3', '-c', script)
        else:
            result = self.k('exec', '-n', 'sqlite-test', pod['metadata']['name'], '-c', 'app', '--', 'python3', '-c', script)
        return json.loads(result)

    def replica_records(self):
        directory = self.volume().get('replicaDirectory', 'replica')
        script = '''import json,sqlite3,tempfile,subprocess
with tempfile.TemporaryDirectory() as d:
 p=d+'/database.db'
 subprocess.run(['litestream','restore','-o',p,'file:///replicas/'''+self.volume_id+'/'+directory+''''],check=True,capture_output=True)
 db=sqlite3.connect('file:'+p+'?mode=ro',uri=True)
 assert db.execute('pragma integrity_check').fetchone()[0]=='ok'
 print(json.dumps([r[0] for r in db.execute('select value from records order by id')]))
'''
        pod = self.controller()
        assert pod
        return json.loads(self.k('exec', '-n', 'sqlite-roam-system', pod['metadata']['name'], '-c', 'driver', '--', 'python3', '-c', script))

    def setup(self):
        existing = self.run('kind', 'get', 'clusters').splitlines()
        assert self.name not in existing
        # Reserve an explicit subnet so a stopped worker can return on its exact
        # original IP. Avoid existing Docker networks and host non-default routes.
        occupied = []
        ids = self.run('docker', 'network', 'ls', '-q').splitlines()
        for network in json.loads(self.run('docker', 'network', 'inspect', *ids)):
            occupied.extend(ipaddress.ip_network(c['Subnet']) for c in (network['IPAM'].get('Config') or []) if c.get('Subnet'))
        for route in json.loads(self.run('ip', '-j', '-4', 'route', 'show')):
            if route.get('dst') not in (None, 'default'):
                occupied.append(ipaddress.ip_network(route['dst'], strict=False))
        subnet = next(ipaddress.ip_network(f'10.250.{n}.0/24') for n in range(1,255)
                      if not any(ipaddress.ip_network(f'10.250.{n}.0/24').overlaps(p) for p in occupied if p.version==4))
        self.network_id = self.run('docker', 'network', 'create', '--subnet', str(subnet),
                                   '--label', 'sqlite-roam.e2e='+self.name, self.name)
        network = json.loads(self.run('docker', 'network', 'inspect', self.network_id))[0]
        self.gateway = network['IPAM']['Config'][0]['Gateway']
        config = {'kind':'Cluster', 'apiVersion':'kind.x-k8s.io/v1alpha4', 'networking':{'ipFamily':'ipv4'},
                  'nodes':[{'role':role,'extraMounts':[{'hostPath':str(self.replicas),'containerPath':'/shared-replicas'}]}
                           for role in ('control-plane','worker','worker')]}
        config_path = self.root/'kind.json'
        config_path.write_text(json.dumps(config))
        self.created = True
        node_image = os.environ.get('KIND_NODE_IMAGE', 'kindest/node:v1.36.4@sha256:099e049362a1526b2db71494e1947aae99bd16290d7c895f2b7ea312e3cbfaed')
        print('Creating disposable cluster '+self.name, flush=True)
        self.run('kind', 'create', 'cluster', '--name', self.name, '--kubeconfig', str(self.kubeconfig),
                 '--config', str(config_path), '--image', node_image, '--wait', '180s', timeout=600)
        assert self.k('config', 'current-context') == 'kind-'+self.name
        nodes = self.get('nodes')['items']
        expected = {self.name+'-control-plane',self.name+'-worker',self.name+'-worker2'}
        assert {n['metadata']['name'] for n in nodes} == expected
        self.mappings = {}
        for node in nodes:
            name = node['metadata']['name']
            if name.endswith('control-plane'):
                continue
            machine = json.loads(self.run('docker', 'inspect', name))[0]
            assert machine['Config']['Labels']['io.x-k8s.kind.cluster'] == self.name
            self.run('docker', 'update', '--restart=no', machine['Id'])
            self.mappings[name] = {'containerID':machine['Id'], 'machineID':'docker:'+machine['Id'],
                                   'nodeUID':node['metadata']['uid']}
        self.workers = sorted(self.mappings)
        self.k('label', 'node', *self.workers, 'sqlite-roam.csi/enabled=true')
        self.k('cordon', self.workers[1])
        self.run('kind', 'load', 'docker-image', self.image, '--name', self.name, timeout=360)
        certificate, key = self.root/'tls.crt', self.root/'tls.key'
        self.run('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                 '-keyout', str(key), '-out', str(certificate), '-subj', '/CN=sqlite-roam-e2e',
                 '-addext', 'subjectAltName=IP:'+self.gateway)
        token = secrets.token_hex(32)
        token_path = self.root/'token'
        token_path.write_text(token)
        token_path.chmod(0o600)
        self.fencer = FenceServer(self.mappings, token, self.replicas, certificate, key)
        config = {'endpoint':f'https://{self.gateway}:{self.fencer.port}', 'caFile':'/fencing/ca.pem',
                  'tokenFile':'/fencing/token', 'manualPowerOnOnly':True,'graceSeconds':5,
                  'nodes':{name:{'nodeUID':m['nodeUID'],'machineID':m['machineID']} for name,m in self.mappings.items()}}
        for namespace in ('sqlite-roam-system','sqlite-test'):
            self.apply({'apiVersion':'v1','kind':'Namespace','metadata':{'name':namespace,
                        'labels':{'pod-security.kubernetes.io/enforce':'privileged'}}})
        self.apply({'apiVersion':'v1','kind':'PersistentVolume','metadata':{'name':'sqlite-test-backing'},
                    'spec':{'capacity':{'storage':'5Gi'},'accessModes':['ReadWriteMany'],'storageClassName':'',
                            'persistentVolumeReclaimPolicy':'Retain','hostPath':{'path':'/shared-replicas','type':'Directory'},
                            'claimRef':{'namespace':'sqlite-roam-system','name':'sqlite-roam-replicas'}}})
        self.apply({'apiVersion':'v1','kind':'PersistentVolumeClaim',
                    'metadata':{'name':'sqlite-roam-replicas','namespace':'sqlite-roam-system'},
                    'spec':{'volumeName':'sqlite-test-backing','storageClassName':'','accessModes':['ReadWriteMany'],
                            'resources':{'requests':{'storage':'5Gi'}}}})
        self.k('create','secret','generic','test-fencing','-n','sqlite-roam-system',
               '--from-file=ca.pem='+str(certificate),'--from-file=token='+str(token_path),
               '--from-literal=config.json='+json.dumps(config))
        self.run('helm','upgrade','--install','sqlite-roam',str(REPO/'charts/sqlite-roam'),
                 '--kubeconfig',str(self.kubeconfig),'--kube-context','kind-'+self.name,
                 '-n','sqlite-roam-system','--set','image='+self.image,'--set','failover.enabled=true',
                 '--set','failover.configSecret=test-fencing','--wait','--timeout','300s')
        self.apply({'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'sqlite-app','namespace':'sqlite-test'},
                    'data':{'app.py':(REPO/'test/e2e/app.py').read_text()}})
        self.apply({'apiVersion':'v1','kind':'PersistentVolumeClaim','metadata':{'name':'sqlite-test','namespace':'sqlite-test'},
                    'spec':{'storageClassName':'sqlite-roam-ha','accessModes':['ReadWriteOncePod'],
                            'resources':{'requests':{'storage':'1Gi'}}}})
        self.apply({'apiVersion':'apps/v1','kind':'Deployment','metadata':{'name':'sqlite-test','namespace':'sqlite-test'},
                    'spec':{'replicas':1,'strategy':{'type':'Recreate'},'selector':{'matchLabels':{'app':'sqlite-test'}},
                            'template':{'metadata':{'labels':{'app':'sqlite-test'}},'spec':{
                                'automountServiceAccountToken':False,'securityContext':{'runAsUser':1000,'runAsGroup':1000},
                                'nodeSelector':{'sqlite-roam.csi/enabled':'true'},
                                'containers':[{'name':'app','image':self.image,'command':['python3','/app/app.py'],
                                    'env':[{'name':'DB_PATH','value':'/data/database.db'},{'name':'ROLE','value':'active'},
                                           {'name':'NODE','valueFrom':{'fieldRef':{'fieldPath':'spec.nodeName'}}}],
                                    'readinessProbe':{'httpGet':{'path':'/records','port':8080},'periodSeconds':2},
                                    'resources':{'requests':{'cpu':'5m','memory':'24Mi'},'limits':{'memory':'96Mi'}},
                                    'volumeMounts':[{'name':'data','mountPath':'/data'},{'name':'app','mountPath':'/app','readOnly':True}]}],
                                'volumes':[{'name':'data','persistentVolumeClaim':{'claimName':'sqlite-test'}},
                                           {'name':'app','configMap':{'name':'sqlite-app'}}]}}}})
        self.wait('initial writer and real CSI mount ready',self.app)
        self.k('uncordon',self.workers[1])

    def scenarios(self):
        source_pod = self.app()
        source = source_pod['spec']['nodeName']
        assert source == self.workers[0]
        assert self.controller()['spec']['nodeName'] == source
        source_ip = self.inspect(source)['NetworkSettings']['Networks'][self.name]['IPAddress']
        pv_name = self.get('pvc','sqlite-test','-n','sqlite-test')['spec']['volumeName']
        pv = self.get('pv',pv_name)
        self.volume_id = pv['spec']['csi']['volumeHandle']
        before = self.volume()
        assert not before['initialized'], 'Scenario must start with a never-cleanly-released writer'
        expected = ['durable-'+str(i) for i in range(10)]
        for marker in expected:
            self.request(source_pod,marker)
        self.wait('all baseline commits restored from shared replica',lambda:self.replica_records()==expected)
        second_spec = json.loads(json.dumps(source_pod['spec']))
        second_spec.pop('nodeName',None)
        self.apply({'apiVersion':'v1','kind':'Pod','metadata':{'name':'second-writer','namespace':'sqlite-test'},'spec':second_spec})
        self.wait('second writer blocked by ReadWriteOncePod',lambda:any(c['type']=='PodScheduled' and c['status']=='False'
                  and 'ReadWriteOncePod' in c.get('message','') for c in self.get('pod','second-writer','-n','sqlite-test').get('status',{}).get('conditions',[])))
        self.k('delete','pod','second-writer','-n','sqlite-test','--wait=true')
        self.fencer.hide_volume = self.volume_id
        self.run('docker','network','disconnect',self.network_id,self.inspect(source)['Id'])
        self.wait('controller rescheduled away from partitioned writer',lambda:(p:=self.controller()) and p['spec']['nodeName']!=source)
        self.wait('denied fencing blocks ownership release',lambda:self.volume().get('recovery',{}).get('phase')=='fencing' and bool(self.fencer.events))
        assert self.volume()['owner']==source and self.volume()['epoch']==before['epoch']
        assert self.inspect(source)['State']['Running']
        assert not any(t['key']=='node.kubernetes.io/out-of-service' for t in self.get('node',source)['spec'].get('taints',[]))
        self.request(source_pod,'durable-during-partition',local=True)
        expected.append('durable-during-partition')
        self.wait('partitioned writer still commits and publishes while fence is denied',lambda:self.replica_records()==expected)
        self.fencer.deny = False
        self.wait('independent fence actually terminates old worker',lambda:not self.inspect(source)['State']['Running'])
        held = self.replicas/self.volume_id/'.held-replica'
        self.wait('missing replica leaves fenced owner locked',lambda:held.exists() and self.volume().get('recovery',{}).get('phase')=='fenced')
        time.sleep(8)
        assert self.volume()['owner']==source and self.volume()['epoch']==before['epoch']
        assert not self.app(), 'Missing replica created an empty replacement'
        self.fencer.hide_volume = None
        held.rename(held.with_name('replica'))
        replacement = self.wait('replacement writer ready on other worker',self.app,300)
        assert replacement['spec']['nodeName']!=source
        assert self.get('pv',pv_name)['metadata']['uid']==pv['metadata']['uid']
        assert self.volume()['epoch']>before['epoch']
        assert self.volume()['replicaDirectory'].startswith('replica-r-')
        assert [r[1] for r in self.request(replacement)['records']]==expected
        assert self.request(replacement,path='/integrity')['integrity']==['ok']
        self.wait('recovered data replicated into separate history',lambda:self.replica_records()==expected)
        self.run('docker','network','connect','--ip',source_ip,self.network_id,self.inspect(source)['Id'])
        self.run('docker','start',self.inspect(source)['Id'])
        self.wait('old worker returns Ready under quarantine',lambda:any(c['type']=='Ready' and c['status']=='True'
                  for c in self.get('node',source)['status']['conditions']))
        node = self.get('node',source)
        assert node['spec']['unschedulable']
        assert any(t['key']=='node.kubernetes.io/out-of-service' for t in node['spec']['taints'])
        self.audit_returned(source,source_pod['metadata']['uid'])
        self.request(replacement,'durable-after-return')
        expected.append('durable-after-return')
        self.wait('active replica progresses after stale worker returns',lambda:self.replica_records()==expected)
        self.k('taint','node',source,'node.kubernetes.io/out-of-service-')
        self.k('uncordon',source)
        self.k('cordon',replacement['spec']['nodeName'])
        self.k('delete','pod',replacement['metadata']['name'],'-n','sqlite-test','--wait=false')
        moved = self.wait('clean handover onto returned worker',lambda:(p:=self.app()) and p['spec']['nodeName']==source and p,300)
        assert [r[1] for r in self.request(moved)['records']]==expected
        assert self.request(moved,path='/integrity')['integrity']==['ok']
        self.wait('clean rejoin preserves records in a fresh restore',lambda:self.replica_records()==expected)

    def audit_returned(self,node,old_uid):
        def no_old_container():
            data = json.loads(self.run('docker','exec',self.inspect(node)['Id'],'crictl',
                '--runtime-endpoint=unix:///run/containerd/containerd.sock','ps','-o','json'))
            return not any(c['labels'].get('io.kubernetes.pod.uid')==old_uid for c in data['containers'])
        self.wait('old application container is gone',no_old_container)
        agent = self.wait('returning node agent Ready',lambda:next((p for p in self.pods('sqlite-roam-system','app=sqlite-roam-node')
                               if p['spec']['nodeName']==node and self.ready(p)),None))
        script = '''import pathlib
matches=[]
for p in pathlib.Path('/proc').glob('[0-9]*/cmdline'):
 try:
  args=p.read_bytes().split(b'\\0')
  if args and args[0].rsplit(b'/',1)[-1]==b'litestream' and b'replicate' in args: matches.append(p.parent.name)
 except OSError: pass
assert not matches, 'stale publisher still running'
print('ok')
'''
        self.wait('returning worker has no stale publisher',lambda:self.k('exec','-n','sqlite-roam-system',agent['metadata']['name'],
                  '-c','driver','--','python3','-c',script)=='ok')

    def collect(self, error):
        result = {'result':'FAIL' if error else 'PASS','cluster':self.name,'steps':self.steps,
                  'error':str(error) if error else None,'fencingEvents':self.fencer.events if self.fencer else []}
        (self.artifacts/'result.json').write_text(json.dumps(result,indent=2))
        if not self.kubeconfig.exists():return
        for name,args in [('nodes',('get','nodes','-o','wide')),('pods',('get','pods','-A','-o','wide')),
                          ('events',('get','events','-A','--sort-by=.lastTimestamp'))]:
            try:(self.artifacts/(name+'.txt')).write_text(self.k(*args))
            except Exception:pass
        for namespace in ('sqlite-roam-system','sqlite-test'):
            try:pods=self.get('pods','-n',namespace)['items']
            except Exception:continue
            for pod in pods:
                for container in pod['spec']['containers']:
                    try:
                        log=self.k('logs','-n',namespace,pod['metadata']['name'],'-c',container['name'],'--tail=500')
                        (self.artifacts/(pod['metadata']['name']+'-'+container['name']+'.log')).write_text(log)
                    except Exception:pass

    def close(self):
        if self.fencer:self.fencer.close()
        if self.created:
            assert self.name.startswith('sqlite-roam-e2e-')
            self.run('kind','delete','cluster','--name',self.name,'--kubeconfig',str(self.kubeconfig))
        if self.network_id:
            networks=[json.loads(line) for line in self.run('docker','network','ls','--format','json').splitlines()]
            if any(n['ID']==self.network_id or self.network_id.startswith(n['ID']) for n in networks):
                network=json.loads(self.run('docker','network','inspect',self.network_id))[0]
                assert network['Labels'].get('sqlite-roam.e2e')==self.name
                self.run('docker','network','rm',self.network_id)
        for name in ('token','tls.key','kubeconfig'):
            (self.root/name).unlink(missing_ok=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--image',default='sqlite-roam:e2e')
    suite=Suite(parser.parse_args().image)
    error=None
    try:
        suite.setup()
        suite.scenarios()
    except Exception as caught:
        error=caught
        print('FAIL '+str(error),file=sys.stderr,flush=True)
    finally:
        suite.collect(error)
        suite.close()
    if error:raise SystemExit(1)
    print('PASS all storage scenarios; artifacts at '+str(suite.artifacts),flush=True)


if __name__=='__main__':
    main()
