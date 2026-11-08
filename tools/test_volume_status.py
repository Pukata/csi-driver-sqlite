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

    def test_failed_fencing_and_restore_are_not_guessed(self):
        for phase, expected in [('fencing', 'fencing authority'), ('fenced', 'replica restore')]:
            with self.subTest(phase=phase):
                state = status.describe(cm(recoveryPolicy='latest-replica', recovery={'phase': phase}))
                self.assertIn(expected, state['reason'])
                self.assertIn('controller logs', state['reason'])
                self.assertEqual(state['recovery_phase'], phase)

    def test_released_requires_replacement_restore(self):
        state = status.describe(cm(owner='', recovery={'phase': 'released'}))
        self.assertIn('restore', state['reason'])

    def test_identity_mismatch_rejected(self):
        obj = cm()
        obj['metadata']['name'] = 'other'
        with self.assertRaises(ValueError):
            status.describe(obj)


if __name__ == '__main__':
    unittest.main()
