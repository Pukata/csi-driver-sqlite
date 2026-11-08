"""Run storage scenarios in a uniquely owned kind cluster, never an active context."""
import argparse
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]


class Suite:
    def __init__(self, image):
        self.name = 'sqlite-csi-e2e-'+secrets.token_hex(4)
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
        return next((p for p in self.pods('sqlite-csi-system', 'app=sqlite-csi-controller') if self.ready(p)), None)

    def volume(self):
        return json.loads(self.get('configmap', self.volume_id, '-n', 'sqlite-csi-system')['data']['volume.json'])

    def request(self, pod, marker=None, path='/records'):
        body = json.dumps({'value': marker}).encode() if marker else None
        script = ('import urllib.request,json; r=urllib.request.Request("http://127.0.0.1:8080'+path+'",'
                  'headers={"Content-Type":"application/json"},data='+repr(body)+');'
                  'print(urllib.request.urlopen(r,timeout=10).read().decode())')
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
        return json.loads(self.k('exec', '-n', 'sqlite-csi-system', pod['metadata']['name'], '-c', 'driver', '--', 'python3', '-c', script))

    def node_mounts(self):
        return [{'hostPath':str(self.replicas),'containerPath':'/shared-replicas'}]

    def configure_backing(self):
        self.apply({'apiVersion':'v1','kind':'PersistentVolume','metadata':{'name':'sqlite-test-backing'},
                    'spec':{'capacity':{'storage':'5Gi'},'accessModes':['ReadWriteMany'],'storageClassName':'',
                            'persistentVolumeReclaimPolicy':'Retain','hostPath':{'path':'/shared-replicas','type':'Directory'},
                            'claimRef':{'namespace':'sqlite-csi-system','name':'sqlite-csi-replicas'}}})
        self.apply({'apiVersion':'v1','kind':'PersistentVolumeClaim',
                    'metadata':{'name':'sqlite-csi-replicas','namespace':'sqlite-csi-system'},
                    'spec':{'volumeName':'sqlite-test-backing','storageClassName':'','accessModes':['ReadWriteMany'],
                            'resources':{'requests':{'storage':'5Gi'}}}})

    def close_backend(self):
        pass

    def setup(self):
        existing = self.run('kind', 'get', 'clusters').splitlines()
        assert self.name not in existing
        self.network_id = self.run('docker', 'network', 'create',
                                   '--label', 'sqlite-csi.e2e='+self.name, self.name)
        config = {'kind':'Cluster', 'apiVersion':'kind.x-k8s.io/v1alpha4', 'networking':{'ipFamily':'ipv4'},
                  'nodes':[{'role':role,'extraMounts':self.node_mounts()}
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
        self.workers = []
        for node in nodes:
            name = node['metadata']['name']
            if name.endswith('control-plane'):
                continue
            machine = json.loads(self.run('docker', 'inspect', name))[0]
            assert machine['Config']['Labels']['io.x-k8s.kind.cluster'] == self.name
            self.run('docker', 'update', '--restart=no', machine['Id'])
            self.workers.append(name)
        self.workers.sort()
        self.k('label', 'node', *self.workers, 'sqlite.csi/enabled=true')
        self.k('cordon', self.workers[1])
        self.run('kind', 'load', 'docker-image', self.image, '--name', self.name, timeout=360)
        for namespace in ('sqlite-csi-system','sqlite-test'):
            self.apply({'apiVersion':'v1','kind':'Namespace','metadata':{'name':namespace,
                        'labels':{'pod-security.kubernetes.io/enforce':'privileged'}}})
        self.configure_backing()
        self.run('helm','upgrade','--install','sqlite-csi',str(REPO/'charts/sqlite-csi'),
                 '--kubeconfig',str(self.kubeconfig),'--kube-context','kind-'+self.name,
                 '-n','sqlite-csi-system','--set','image='+self.image,'--wait','--timeout','300s')
        self.apply({'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'sqlite-app','namespace':'sqlite-test'},
                    'data':{'app.py':(REPO/'test/e2e/app.py').read_text()}})
        self.apply({'apiVersion':'v1','kind':'PersistentVolumeClaim','metadata':{'name':'sqlite-test','namespace':'sqlite-test'},
                    'spec':{'storageClassName':'sqlite-csi','accessModes':['ReadWriteOncePod'],
                            'resources':{'requests':{'storage':'1Gi'}}}})
        self.apply({'apiVersion':'apps/v1','kind':'Deployment','metadata':{'name':'sqlite-test','namespace':'sqlite-test'},
                    'spec':{'replicas':1,'strategy':{'type':'Recreate'},'selector':{'matchLabels':{'app':'sqlite-test'}},
                            'template':{'metadata':{'labels':{'app':'sqlite-test'}},'spec':{
                                'automountServiceAccountToken':False,'securityContext':{'runAsUser':1000,'runAsGroup':1000},
                                'nodeSelector':{'sqlite.csi/enabled':'true'},
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
        # Kubernetes stops the old Pod; CSI verifies the replica before releasing ownership.
        self.k('cordon',source)
        self.k('delete','pod',source_pod['metadata']['name'],'-n','sqlite-test','--wait=false')
        moved = self.wait('clean handover to other worker',
                         lambda:(p:=self.app()) and p['spec']['nodeName']!=source and p,300)
        assert self.get('pv',pv_name)['metadata']['uid']==pv['metadata']['uid']
        assert self.volume()['epoch']>before['epoch']
        assert [r[1] for r in self.request(moved)['records']]==expected
        assert self.request(moved,path='/integrity')['integrity']==['ok']
        self.request(moved,'after-clean-move')
        expected.append('after-clean-move')
        self.wait('moved writer publishes a verified replica',lambda:self.replica_records()==expected)
        self.k('uncordon',source)

    def gatus_test(self):
        """Run the checked-in real application manifest; retain actual history across a move."""
        self.k('uncordon', *self.workers)
        # Parse with kubectl, so CI exercises the exact example without a YAML dependency.
        stream = self.k('create','--dry-run=client','-f',str(REPO/'examples/gatus.yaml'),'-o','json')
        objects = []
        while stream.strip():
            stream = stream.lstrip()
            obj, end = json.JSONDecoder().raw_decode(stream)
            objects.extend(obj['items'] if obj.get('kind') == 'List' else [obj])
            stream = stream[end:]
        for obj in objects:
            obj['metadata']['namespace'] = 'sqlite-test'
            if obj['kind'] == 'Deployment':
                obj['spec']['template']['spec']['nodeSelector']['kubernetes.io/hostname'] = self.workers[0]
            self.apply(obj)
        def app():
            return next((p for p in self.pods('sqlite-test','app=gatus') if self.ready(p)),None)
        original = self.wait('Gatus real application Ready on source node',app)
        assert original['spec']['nodeName'] == self.workers[0]
        pv_name = self.get('pvc','gatus-db','-n','sqlite-test')['spec']['volumeName']
        pv = self.get('pv',pv_name)
        volume_id = pv['spec']['csi']['volumeHandle']
        def history(pod, restore=False):
            volume = json.loads(self.get('cm',volume_id,'-n','sqlite-csi-system')['data']['volume.json'])
            agent = next(p for p in self.pods('sqlite-csi-system','app=sqlite-csi-node')
                         if p['spec']['nodeName']==pod['spec']['nodeName'] and self.ready(p))
            path = f'/var/lib/sqlite-csi/{volume_id}/epoch-{volume["epoch"]}/database.db'
            script = '''import json,sqlite3,sys,tempfile,subprocess
from pathlib import Path
path=sys.argv[1]
with tempfile.TemporaryDirectory() as directory:
 if sys.argv[2]:
  path=directory+'/restored.db'
  subprocess.run(['litestream','restore','-o',path,'file://'+sys.argv[2]],check=True,capture_output=True)
 db=sqlite3.connect('file:'+path+'?mode=ro',uri=True)
 assert db.execute('pragma integrity_check').fetchone()[0]=='ok'
 print(json.dumps(db.execute('select endpoint_result_id,success,timestamp from endpoint_results order by endpoint_result_id').fetchall()))
'''
            replica = '/replicas/'+volume_id+'/'+volume.get('replicaDirectory','replica') if restore else ''
            return json.loads(self.k('exec','-n','sqlite-csi-system',agent['metadata']['name'],'-c','driver',
                                      '--','python3','-c',script,path,replica))
        baseline = self.wait('Gatus writes real monitoring history to SQLite',lambda:(rows:=history(original)) and len(rows)>=3 and rows)
        assert all(row[1] == 1 for row in baseline), 'Self-monitor checks failed'
        self.wait('Gatus history verified in a fresh Litestream restore',lambda:all(row in history(original,True) for row in baseline))
        movement_started = time.monotonic()
        self.k('patch','deployment','gatus','-n','sqlite-test','--type=merge','-p',json.dumps({
            'spec':{'template':{'spec':{'nodeSelector':{'kubernetes.io/hostname':self.workers[1]}}}}}))
        replacement = self.wait('Gatus clean handover to other node',lambda:(p:=app()) and p['spec']['nodeName']==self.workers[1] and p,300)
        assert self.get('pv',pv_name)['metadata']['uid']==pv['metadata']['uid']
        self.wait('Gatus retains original history and records new checks after move',
                  lambda:(rows:=history(replacement)) and len(rows)>len(baseline) and all(row in rows for row in baseline))
        self.wait('Gatus preserved history survives another fresh replica restore',
                  lambda:all(row in history(replacement,True) for row in baseline))
        (self.artifacts/'gatus-movement.json').write_text(json.dumps({
            'result':'PASS','application':'Gatus v5.38.0','sourceNode':original['spec']['nodeName'],
            'targetNode':replacement['spec']['nodeName'],'preservedRows':len(baseline),
            'patchToReadyAndVerifiedHistorySeconds':round(time.monotonic()-movement_started,3)},indent=2))

    def collect(self, error):
        result = {'result':'FAIL' if error else 'PASS','cluster':self.name,'steps':self.steps,
                  'error':str(error) if error else None}
        (self.artifacts/'result.json').write_text(json.dumps(result,indent=2))
        if not self.kubeconfig.exists():return
        for name,args in [('nodes',('get','nodes','-o','wide')),('pods',('get','pods','-A','-o','wide')),
                          ('events',('get','events','-A','--sort-by=.lastTimestamp'))]:
            try:(self.artifacts/(name+'.txt')).write_text(self.k(*args))
            except Exception:pass
        for namespace in ('sqlite-csi-system','sqlite-test'):
            try:pods=self.get('pods','-n',namespace)['items']
            except Exception:continue
            for pod in pods:
                for container in pod['spec']['containers']:
                    try:
                        log=self.k('logs','-n',namespace,pod['metadata']['name'],'-c',container['name'],'--tail=500')
                        (self.artifacts/(pod['metadata']['name']+'-'+container['name']+'.log')).write_text(log)
                    except Exception:pass

    def close(self):
        if self.created:
            assert self.name.startswith('sqlite-csi-e2e-')
            self.run('kind','delete','cluster','--name',self.name,'--kubeconfig',str(self.kubeconfig))
        self.close_backend()
        if self.network_id:
            networks=[json.loads(line) for line in self.run('docker','network','ls','--format','json').splitlines()]
            if any(n['ID']==self.network_id or self.network_id.startswith(n['ID']) for n in networks):
                network=json.loads(self.run('docker','network','inspect',self.network_id))[0]
                assert network['Labels'].get('sqlite-csi.e2e')==self.name
                self.run('docker','network','rm',self.network_id)
        for name in ('kubeconfig',):
            (self.root/name).unlink(missing_ok=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--image',default='sqlite-csi:e2e')
    suite=Suite(parser.parse_args().image)
    error=None
    try:
        suite.setup()
        suite.scenarios()
        suite.gatus_test()
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
