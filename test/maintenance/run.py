"""Exercise maintenance only in the disposable, per-run kind cluster."""
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'test/e2e'))
import run as e2e


class Maintenance(e2e.Suite):
    def node_agent(self, node):
        return next((p for p in self.pods('sqlite-csi-system', 'app=sqlite-csi-node')
                     if p['spec']['nodeName'] == node and self.ready(p)), None)

    def restart(self, pod, finder, description):
        uid = pod['metadata']['uid']
        self.k('delete', 'pod', pod['metadata']['name'], '-n', 'sqlite-csi-system', '--wait=false')
        return self.wait(description, lambda: (p := finder()) and p['metadata']['uid'] != uid and p, 300)

    def locked(self, owner, epoch, allow_old=False):
        volume = self.volume()
        assert volume['owner'] == owner and volume['epoch'] == epoch, volume
        ready = [p for p in self.pods('sqlite-test', 'app=sqlite-test') if self.ready(p)]
        assert not any(not allow_old or p['spec']['nodeName'] != owner for p in ready), (
            'replacement writer became ready while recovery was blocked', ready)

    def exercise(self, target_image):
        app = self.app()
        source = app['spec']['nodeName']
        pv_name = self.get('pvc', 'sqlite-test', '-n', 'sqlite-test')['spec']['volumeName']
        pv = self.get('pv', pv_name)
        self.volume_id = pv['spec']['csi']['volumeHandle']
        before = self.volume()
        expected = ['before-maintenance']
        self.request(app, expected[0])
        self.wait('baseline fresh restore', lambda: self.replica_records() == expected)
        # Write immediately before deleting each driver pod; restore verifies publication
        # eventually resumes, including the commit made during the rollout window.
        for component in ('controller', 'node'):
            marker = 'before-restart-' + component
            self.request(app, marker)
            expected.append(marker)
            if component == 'controller':
                self.restart(self.controller(), self.controller, 'controller restarted during replication')
            else:
                self.restart(self.node_agent(source), lambda: self.node_agent(source), 'node agent restarted during replication')
            assert self.volume()['owner'] == source and self.volume()['epoch'] == before['epoch']
            assert [r[1] for r in self.request(app)['records']] == expected
            self.wait(component + ' restart preserves a fresh restore', lambda: self.replica_records() == expected)

        self.run('kind', 'load', 'docker-image', target_image, '--name', self.name, timeout=360)
        self.run('helm', 'upgrade', 'sqlite-csi', str(REPO / 'charts/sqlite-csi'),
                 '--kubeconfig', str(self.kubeconfig), '--kube-context', 'kind-' + self.name,
                 '-n', 'sqlite-csi-system', '--set', 'image=' + target_image,
                 '--set', 'failover.enabled=true', '--set', 'failover.configSecret=test-fencing',
                 '--wait', '--timeout', '300s', timeout=360)
        def runs_target(pod):
            return pod and any(c['name'] == 'driver' and c['image'] == target_image
                               for c in pod['spec']['containers']) and pod
        self.wait('upgraded controller ready', lambda: runs_target(self.controller()))
        self.wait('upgraded node agent ready', lambda: runs_target(self.node_agent(source)))
        assert self.get('pv', pv_name)['metadata']['uid'] == pv['metadata']['uid']
        assert self.volume()['owner'] == source and self.volume()['epoch'] == before['epoch']
        assert [r[1] for r in self.request(app)['records']] == expected
        self.request(app, 'after-upgrade')
        expected.append('after-upgrade')
        assert self.request(app, path='/integrity')['integrity'] == ['ok']
        self.wait('upgraded driver preserves fresh restored data', lambda: self.replica_records() == expected)

        # Fence is initially denied; restart the controller at this boundary and
        # again after fencing while the replica is deliberately unavailable.
        self.fencer.hide_volume = self.volume_id
        self.run('docker', 'network', 'disconnect', self.network_id, self.inspect(source)['Id'])
        self.wait('fencing denied', lambda: self.volume().get('recovery', {}).get('phase') == 'fencing'
                  and bool(self.fencer.events), 300)
        self.locked(source, before['epoch'], allow_old=True)
        self.restart(self.controller(), self.controller, 'controller restarted at fencing boundary')
        self.locked(source, before['epoch'], allow_old=True)
        self.fencer.deny = False
        self.wait('independent fence powers off old writer', lambda: not self.inspect(source)['State']['Running'])
        self.wait('missing replica holds fenced recovery', lambda: self.fencer.archive(self.volume_id, 'held')
                  and self.volume().get('recovery', {}).get('phase') == 'fenced')
        self.locked(source, before['epoch'])
        self.restart(self.controller(), self.controller, 'controller restarted at fenced boundary')
        target = next(n for n in self.workers if n != source)
        self.restart(self.node_agent(target), lambda: self.node_agent(target), 'node agent restarted during blocked recovery')
        self.locked(source, before['epoch'])
        with self.fencer.lock:
            self.fencer.hide_volume = None
            self.fencer.archive(self.volume_id, 'restore')
        replacement = self.wait('recovery resumes after repair', self.app, 300)
        assert replacement['spec']['nodeName'] == target
        assert self.volume()['epoch'] > before['epoch']
        assert self.get('pv', pv_name)['metadata']['uid'] == pv['metadata']['uid']
        assert [r[1] for r in self.request(replacement)['records']] == expected
        assert self.request(replacement, path='/integrity')['integrity'] == ['ok']
        self.wait('recovered records verified in a fresh restore', lambda: self.replica_records() == expected)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline-repo', type=Path, required=True,
                        help='checkout containing the baseline chart and test application')
    parser.add_argument('--baseline-image', required=True)
    parser.add_argument('--image', required=True, help='target driver image')
    args = parser.parse_args()
    baseline = args.baseline_repo.resolve()
    if not (baseline / 'charts/sqlite-csi/Chart.yaml').is_file() or not (baseline / 'test/e2e/app.py').is_file():
        parser.error('baseline repository must contain chart and test/e2e/app.py')
    suite = Maintenance(args.baseline_image)
    error = None
    try:
        e2e.REPO = baseline
        suite.setup()
        e2e.REPO = REPO
        suite.exercise(args.image)
    except Exception as caught:
        error = caught
        print('FAIL ' + str(error), file=sys.stderr, flush=True)
    finally:
        suite.collect(error)
        if getattr(suite, 'volume_id', None):
            try:
                (suite.artifacts / 'volume-state.json').write_text(json.dumps(suite.volume(), indent=2))
            except Exception:
                pass
        suite.close()
    if error:
        raise SystemExit(1)
    print('PASS maintenance scenarios; artifacts at ' + str(suite.artifacts), flush=True)


if __name__ == '__main__':
    main()
