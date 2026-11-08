"""Check that focused safety tests reject representative source mutations.

Runs Go unit tests only, in temporary source copies; never touches the checkout
or a cluster. A compiler/setup failure is not counted as a killed mutation.
"""
import pathlib
import shutil
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
MUTATIONS = [
    ('competing owner accepted', 'controller.go',
     'if v.Owner != "" && v.Owner != node {', 'if false {',
     'TestOwnershipStateMachine'),
    ('epoch increment omitted', 'controller.go', 'v.Epoch++', '// epoch increment omitted',
     'TestOwnershipStateMachine'),
    ('fencing proof mismatch ignored', 'recovery.go',
     'if proof.NodeUID != node.UID || proof.OperationID != record.ID || proof.MachineID == "" || proof.ObservedAt.IsZero() {',
     'if false {', 'TestMismatchedPowerProofNeverRestoresOrUnlocks'),
]


def main():
    for description, filename, original, mutant, test in MUTATIONS:
        with tempfile.TemporaryDirectory(prefix='sqlite-csi-mutation-') as directory:
            root = pathlib.Path(directory)
            for path in list(ROOT.glob('*.go')) + [ROOT / 'go.mod', ROOT / 'go.sum']:
                shutil.copy2(path, root / path.name)
            path = root / filename
            source = path.read_text()
            if source.count(original) != 1:
                raise RuntimeError(f'{description}: mutation target is missing or ambiguous')
            path.write_text(source.replace(original, mutant, 1))
            result = subprocess.run(['go', 'test', '-count=1', '-run', '^' + test + '$', './...'],
                                    cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    timeout=180)
            if result.returncode == 0 or '--- FAIL: ' + test not in result.stdout:
                raise RuntimeError(f'{description}: mutation not killed by {test}:\n{result.stdout}')
            print(f'PASS {test} killed {description}', flush=True)


if __name__ == '__main__':
    main()
