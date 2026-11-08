#!/usr/bin/env python3
"""Read one SQLite CSI volume ConfigMap and explain its persisted state."""
import argparse
import json
import subprocess
import sys


def describe(cm):
    v = json.loads(cm['data']['volume.json'])
    if v['id'] != cm['metadata']['name']:
        raise ValueError('volume ID does not match ConfigMap name')
    policy = v.get('recoveryPolicy') or 'manual'
    recovery = v.get('recovery') or {}
    phase = recovery.get('phase', 'none recorded')
    if v.get('deleted'):
        reason = 'Volume is marked deleted; investigate before attempting recovery.'
    elif v.get('owner') and v.get('prepared') and not v.get('clean'):
        if policy == 'manual':
            reason = 'Prepared owner is locked under manual policy; this is also normal for an active writer. Check workload and node health before diagnosing a failure. For unclean recovery, independently fence the old writer and follow the manual recovery runbook.'
        elif phase == 'fencing':
            reason = 'Recovery is at fencing; verify node identity and the independent fencing authority. Check controller logs for the failure.'
        elif phase == 'fenced':
            reason = 'Fencing was recorded; replica restore, verification, or quarantine may be blocked. Check controller logs; keep the old writer fenced.'
        else:
            reason = 'Prepared owner is locked; this is also normal for an active writer. Check workload and node health, grace period, identity mapping and controller logs. Unclean takeover requires independent fencing.'
    elif v.get('owner'):
        reason = 'Owner is recorded; a move depends on normal detach and clean handover.'
    elif phase == 'released':
        reason = 'Recovery released the old owner; replacement must claim and restore the verified seed.'
    else:
        reason = 'No owner recorded. This alone does not verify replica health or authorize a manual takeover.'
    return {'volume': v['id'], 'owner': v.get('owner') or '(none)', 'epoch': v.get('epoch'),
            'prepared': v.get('prepared'), 'clean': v.get('clean'), 'initialized': v.get('initialized'),
            'policy': policy, 'recovery_phase': phase, 'recovery_id': recovery.get('id') or '(none)',
            'reason': reason, 'last_replication': 'unknown (not persisted)',
            'last_verification': 'unknown (no timestamp persisted)',
            'note': 'Persisted state is a snapshot, not a fencing proof or a durability acknowledgement.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('volume', nargs='?', help='volume ID / ConfigMap name')
    parser.add_argument('-n', '--namespace', default='sqlite-csi-system', help='driver namespace (default: sqlite-csi-system)')
    parser.add_argument('--context', help='explicit kubectl context (defaults to current context)')
    parser.add_argument('--file', help='read a captured ConfigMap JSON file instead of calling kubectl')
    args = parser.parse_args()
    try:
        if args.file:
            with open(args.file, encoding='utf-8') as f:
                cm = json.load(f)
        else:
            if not args.volume:
                parser.error('volume is required unless --file is used')
            command = ['kubectl', '--request-timeout=15s']
            if args.context:
                command.extend(['--context', args.context])
            command.extend(['-n', args.namespace, 'get', 'configmap', args.volume, '-o', 'json'])
            result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=20)
            cm = json.loads(result.stdout)
        if args.volume and cm['metadata']['name'] != args.volume:
            raise ValueError('requested volume does not match ConfigMap name')
        print(json.dumps(describe(cm), indent=2))
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print('Cannot read volume state: ' + (exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
