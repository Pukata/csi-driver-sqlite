import copy
import json
import ssl
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from server import Authority, make_server


class FencingTests(unittest.TestCase):
    def setUp(self):
        self.config = {'manualPowerOnOnly':True, 'tokenFile':'unused', 'nodes':{'worker':{
            'nodeUID':'uid', 'uuid':'machine', 'endpoint':'https://bmc.example',
            'systemPath':'/redfish/v1/Systems/1', 'caFile':'ca.pem',
            'username':'fencer', 'passwordFile':'password'}}}
        self.request = dict(nodeName='worker',nodeUID='uid',machineID='redfish:machine',
                            operationID='r-'+'a'*32,challenge='b'*64)
        self.state = {'UUID':'machine','PowerRestorePolicy':'AlwaysOff','PowerState':'On',
            'Actions':{'#ComputerSystem.Reset':{'ResetType@Redfish.AllowableValues':['ForceOff']}}}
        self.calls = []
        self.authority = Authority(self.config)
        def bmc(node, method, path, body=None):
            self.calls.append((method,path,body))
            if method == 'POST':
                self.state['PowerState'] = 'Off'
                return {}
            return copy.deepcopy(self.state)
        self.authority.request = bmc

    def test_force_off_and_reconfirm_on_retry(self):
        proof = self.authority.fence(self.request)
        self.assertTrue(proof['restartInhibited'])
        self.assertEqual(proof['challenge'], self.request['challenge'])
        self.assertEqual(self.calls[1], ('POST','/redfish/v1/Systems/1/Actions/ComputerSystem.Reset',{'ResetType':'ForceOff'}))
        self.calls.clear()
        self.authority.fence(self.request)
        self.assertEqual([c[0] for c in self.calls], ['GET','GET'])

    def test_identity_policy_and_action_fail_closed(self):
        for field, value in [('UUID','other'),('PowerRestorePolicy','AlwaysOn'),('Actions',{})]:
            with self.subTest(field=field):
                state = copy.deepcopy(self.state)
                self.state[field] = value
                self.calls.clear()
                with self.assertRaises(ValueError): self.authority.fence(self.request)
                self.assertFalse(any(c[0]=='POST' for c in self.calls))
                self.state = state

    def test_invalid_client_identity_never_contacts_bmc(self):
        for field in ['nodeUID','machineID','nodeName','challenge','operationID']:
            request = dict(self.request, **{field:'wrong'})
            with self.subTest(field=field), self.assertRaises((ValueError,KeyError)):
                self.authority.fence(request)
        self.assertEqual(self.calls, [])

    def test_off_timeout_never_returns_proof(self):
        self.authority.request = lambda *args: copy.deepcopy(self.state)
        with patch('server.time.monotonic', side_effect=[0,30]):
            with self.assertRaises(TimeoutError): self.authority.fence(self.request)

    def test_config_requires_explicit_guarantee_and_https(self):
        for change in ['guarantee','http','credentials','duplicate']:
            config = copy.deepcopy(self.config)
            if change == 'guarantee': config['manualPowerOnOnly'] = False
            if change == 'http': config['nodes']['worker']['endpoint'] = 'http://bmc'
            if change == 'credentials': config['nodes']['worker']['endpoint'] = 'https://user:pass@bmc'
            if change == 'duplicate': config['nodes']['second'] = config['nodes']['worker']
            with self.subTest(change=change), self.assertRaises(ValueError): Authority(config)

    def test_tls_and_authentication_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            certificate, key = root/'cert.pem', root/'key.pem'
            subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
                '-keyout',str(key),'-out',str(certificate),'-subj','/CN=localhost',
                '-addext','subjectAltName=IP:127.0.0.1'],check=True,capture_output=True)
            token = root/'token'
            token.write_text('test-token')
            self.authority.config['tokenFile'] = str(token)
            server = make_server(self.authority, ('127.0.0.1',0), str(certificate),str(key))
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                url = 'https://127.0.0.1:'+str(server.server_port)+'/v1/fence'
                context = ssl.create_default_context(cafile=str(certificate))
                def send(token):
                    req = urllib.request.Request(url,data=json.dumps(self.request).encode(),
                            headers={'Authorization':'Bearer '+token})
                    return urllib.request.urlopen(req,context=context)
                with self.assertRaises(urllib.error.HTTPError) as error: send('wrong')
                self.assertEqual(error.exception.code,401)
                self.assertEqual(self.calls,[])
                with send('test-token') as response:
                    self.assertEqual(json.load(response)['state'],'off')
                token.write_text('rotated')
                with self.assertRaises(urllib.error.HTTPError): send('test-token')
                with send('rotated') as response: self.assertEqual(response.status,200)
            finally:
                server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__': unittest.main()
