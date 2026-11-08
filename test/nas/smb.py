"""Disposable Samba-backed variant of the shared NAS recovery suite."""
import hashlib
import os
import secrets
import socket
import urllib.request

from run import NasSuite, main

CHART = 'https://raw.githubusercontent.com/kubernetes-csi/csi-driver-smb/v1.19.1/charts/v1.19.1/csi-driver-smb-1.19.1.tgz'
CHART_SHA256 = '55b855a16a5eabb63d7d08bf7632bb520836cf387d942847e0770b2e40600cc1'
MOUNT_OPTIONS = ['vers=3.0', 'dir_mode=0777', 'file_mode=0777', 'noperm', 'cache=strict', 'noserverino']


class SmbSuite(NasSuite):
    def configure_backing(self):
        password = secrets.token_hex(32)
        credential_file = self.root/'smb-password'
        fd = os.open(credential_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as credential:
            credential.write(password+'\n')
        self.nas_id = self.run('docker','run','-d','--name',self.name+'-nas','--network',self.name,
            '--label','sqlite-csi.nas-test='+self.name,'--restart=no',
            '--mount','type=bind,src='+str(self.replicas)+',dst=/export',
            '--mount','type=bind,src='+str(credential_file)+',dst=/run/smb-password,readonly',self.nas_image)
        self.nas_ip = self.nas()['NetworkSettings']['Networks'][self.name]['IPAddress']
        def listening():
            try:
                with socket.create_connection((self.nas_ip,445),timeout=1): return True
            except OSError: return False
        self.wait('independent NAS SMB listener ready',listening,30)
        archive = self.root/'smb-chart.tgz'
        with urllib.request.urlopen(CHART,timeout=30) as response:
            payload = response.read(2*1024*1024)
        assert hashlib.sha256(payload).hexdigest() == CHART_SHA256
        archive.write_bytes(payload)
        self.run('helm','upgrade','--install','smb-test',str(archive),
            '--kubeconfig',str(self.kubeconfig),'--kube-context','kind-'+self.name,
            '-n','kube-system','--set','controller.replicas=1','--set','windows.enabled=false','--wait','--timeout','240s')
        self.apply({'apiVersion':'v1','kind':'Secret','metadata':{'name':'smb-credentials','namespace':'sqlite-csi-system'},
                    'stringData':{'username':'sqlite','password':password}})
        self.apply({'apiVersion':'storage.k8s.io/v1','kind':'StorageClass','metadata':{'name':'test-nas'},
            'provisioner':'smb.csi.k8s.io','parameters':{'source':'//'+self.nas_ip+'/replicas',
                'csi.storage.k8s.io/provisioner-secret-name':'smb-credentials',
                'csi.storage.k8s.io/provisioner-secret-namespace':'sqlite-csi-system',
                'csi.storage.k8s.io/node-stage-secret-name':'smb-credentials',
                'csi.storage.k8s.io/node-stage-secret-namespace':'sqlite-csi-system'},
            'reclaimPolicy':'Retain','volumeBindingMode':'Immediate','mountOptions':MOUNT_OPTIONS})
        self.apply({'apiVersion':'v1','kind':'PersistentVolumeClaim',
            'metadata':{'name':'sqlite-csi-replicas','namespace':'sqlite-csi-system'},
            'spec':{'storageClassName':'test-nas','accessModes':['ReadWriteMany'],
                    'resources':{'requests':{'storage':'5Gi'}}}})

    def close(self):
        try:
            super().close()
        finally:
            (self.root/'smb-password').unlink(missing_ok=True)

    def verify_backend(self):
        claim = self.get('pvc','sqlite-csi-replicas','-n','sqlite-csi-system')
        pv = self.get('pv',claim['spec']['volumeName'])
        assert pv['spec']['csi']['driver']=='smb.csi.k8s.io'
        assert pv['spec']['csi']['volumeAttributes']['source']=='//'+self.nas_ip+'/replicas'
        for node in self.workers:
            agent = self.wait('SMB-backed node agent Ready: '+node,lambda:next((p for p in self.pods(
                'sqlite-csi-system','app=sqlite-csi-node') if p['spec']['nodeName']==node and self.ready(p)),None))
            mount = self.k('exec','-n','sqlite-csi-system',agent['metadata']['name'],'-c','driver',
                           '--','findmnt','-n','-o','FSTYPE,SOURCE','--target','/replicas')
            self.check_mount(mount)

    def check_mount(self, mount):
        parts = mount.split()
        source = '//'+self.nas_ip+'/replicas'
        assert len(parts)==2 and parts[0]=='cifs', mount
        # findmnt can express a subdirectory as either /share/path or /share[/path].
        assert (parts[1]==source or parts[1].startswith(source+'/') or
                (parts[1].startswith(source+'[/') and parts[1].endswith(']'))), mount

    def write_topology(self):
        import json
        (self.artifacts/'topology.json').write_text(json.dumps({'backend':'SMB3',
            'csiDriver':'smb.csi.k8s.io','chartVersion':'1.19.1','chartSHA256':CHART_SHA256,
            'kubernetesNodes':3,'separateNas':True,'workerHostDirectoryBypass':False,
            'mountOptions':MOUNT_OPTIONS,
            'nasPackages':self.run('docker','exec',self.nas_id,'dpkg-query','-W','samba')},indent=2))


if __name__=='__main__':
    main(SmbSuite, 'real-smb3', 'sqlite-csi-smb:e2e')
