import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('volume_status', Path(__file__).with_name('volume-status.py'))
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)


def cm(**fields):
    volume = {'id': 'pvc-example', 'owner': 'node-a', 'epoch': 7, 'prepared': True, 'clean': False}
    volume.update(fields)
    return {'metadata': {'name': 'pvc-example'}, 'data': {'volume.json': json.dumps(volume)}}


class StatusTests(unittest.TestCase):
    def test_manual_default_locked(self):
        state = status.describe(cm())
        self.assertEqual(state['policy'], 'manual')
        self.assertIn('locked', state['reason'])
        self.assertIn('unknown', state['last_replication'])

    def test_legacy_state_requires_migration_even_without_owner(self):
        for fields in [{'recoveryPolicy': 'latest-replica'}, {'recoveryPolicy': 'unknown'},
                       {'recoverySeed': 'old/seed'}, {'seedDigest': 'digest'},
                       {'seedClaimedEpoch': 7}, {'replicaDirectory': 'replica-old'},
                       {'recovery': {}}, {'recovery': {'phase': 'released'}},
                       {'recovery': {'phase': 'fencing'}}, {'recovery': []}]:
            with self.subTest(fields=fields):
                state = status.describe(cm(owner='', **fields))
                self.assertTrue(state['legacy_state'])
                self.assertIn('migrate', state['reason'])
                self.assertIn('does not authorize takeover', state['reason'])

    def test_active_owner_is_not_reported_as_a_failed_writer(self):
        for policy in ('', 'manual'):
            with self.subTest(policy=policy):
                state = status.describe(cm(recoveryPolicy=policy))
                self.assertIn('normal for an active writer', state['reason'])
                self.assertIn('node health', state['reason'])
                self.assertIn('never loses ownership automatically', state['reason'])
                self.assertFalse(state['legacy_state'])

    def test_unowned_state_does_not_prove_replica_health(self):
        state = status.describe(cm(owner='', recovery=None, replicaDirectory='replica'))
        self.assertFalse(state['legacy_state'])
        self.assertIn('does not verify replica health', state['reason'])

    def test_clean_and_deleted_states(self):
        self.assertIn('normal detach', status.describe(cm(clean=True))['reason'])
        self.assertIn('deleted', status.describe(cm(deleted=True))['reason'])

    def test_identity_mismatch_rejected(self):
        obj = cm()
        obj['metadata']['name'] = 'other'
        with self.assertRaises(ValueError):
            status.describe(obj)


if __name__ == '__main__':
    unittest.main()
