"""Assertions and resource measurements against the shipped Litestream/digest code."""
import hashlib
import json
import os
import platform
import signal
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path


def measured(args, timeout=120):
    started = time.monotonic()
    peak = 0
    # Files avoid subprocess PIPE deadlock while sampling a potentially large dump.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(args, stdout=stdout, stderr=stderr)
        try:
            while process.poll() is None:
                try:
                    status = Path('/proc/'+str(process.pid)+'/status').read_text()
                    for line in status.splitlines():
                        if line.startswith('VmRSS:'): peak = max(peak,int(line.split()[1])*1024)
                except FileNotFoundError: pass
                if time.monotonic()-started > timeout: raise TimeoutError('Test subprocess deadline')
                time.sleep(0.005)
            stdout.seek(0); stderr.seek(0)
            output = stdout.read().decode()
            assert process.returncode == 0, stderr.read().decode()[-3000:]
            return output.strip(), {'seconds':round(time.monotonic()-started,4),'sampledPeakRSSBytes':peak}
        finally:
            if process.poll() is None: process.kill(); process.wait()


def test_case(size_mib, digest_program):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        db = root/'database.db'
        connection = sqlite3.connect(db)
        connection.execute('pragma journal_mode=WAL')
        connection.execute('create table chunks (id integer primary key, payload blob)')
        # Fixed, incompressible fixtures; many bounded rows exercise streaming digest.
        for n in range(size_mib*256):
            payload = hashlib.shake_256(str(n).encode()).digest(4096)
            connection.execute('insert into chunks values (?,?)',(n,payload))
        connection.commit()
        config = root/'litestream.json'
        config.write_text(json.dumps({'dbs':[{'path':str(db),'replica':{'path':str(root/'replica')}}]}))
        with (root/'publisher.log').open('w') as log:
            publisher = subprocess.Popen(['litestream','replicate','-config',str(config)],stdout=log,stderr=log)
            try:
                def restore_until(count):
                    started = time.monotonic()
                    while True:
                        assert publisher.poll() is None, 'Publisher unexpectedly exited'
                        target = root/'restored.db'
                        for path in root.glob('restored.db*'): path.unlink()
                        result = subprocess.run(['litestream','restore','-o',str(target),'file://'+str(root/'replica')],
                                                capture_output=True,timeout=30)
                        if result.returncode == 0:
                            with sqlite3.connect(target) as restored:
                                if restored.execute('select count(*) from chunks').fetchone()[0] == count:
                                    assert restored.execute('pragma integrity_check').fetchone()[0]=='ok'
                                    return round(time.monotonic()-started,4)
                        assert time.monotonic()-started < 90, 'Replica failed to converge'
                        time.sleep(0.1)
                initial = restore_until(size_mib*256)
                connection.execute('insert into chunks values (?,?)',(-1,b'acknowledged-marker'))
                connection.commit()
                lag = restore_until(size_mib*256+1)
                publisher.send_signal(signal.SIGTERM)
                publisher.wait(timeout=30)
                assert publisher.returncode == 0
                original, digest = measured(['python3','-c',digest_program,str(db)])
                target = root/'handover.db'
                _, restore = measured(['litestream','restore','-o',str(target),'file://'+str(root/'replica')])
                copy, verify = measured(['python3','-c',digest_program,str(target)])
                assert original == copy, 'Restored logical database differs'
                assert digest['sampledPeakRSSBytes'] < 384*1024*1024, 'Digest exceeded regression memory budget'
                assert verify['sampledPeakRSSBytes'] < 384*1024*1024, 'Restore verification exceeded memory budget'
                return {'fixtureMiB':size_mib,'databaseBytes':db.stat().st_size,
                        'initialReplicaAndRestoreSeconds':initial,
                        'commitToVerifiedRestoreSeconds':lag,'digest':digest,'restore':restore,'verify':verify}
            finally:
                connection.close()
                if publisher.poll() is None: publisher.kill(); publisher.wait()


if __name__ == '__main__':
    source = Path('/src/node.go').read_text()
    program = source.split('const digestProgram = `',1)[1].split('`',1)[0]
    # Each case is a correctness/memory testcase. Timings are observations, not SLAs.
    cases = [test_case(size,program) for size in (1,16,64)]
    print(json.dumps({'result':'PASS','runtime':platform.python_version(),'kernel':platform.release(),
          'architecture':platform.machine(),'litestream':subprocess.check_output(['litestream','version'],text=True).strip(),
          'driverRevision':os.environ.get('DRIVER_REVISION','unknown'),
          'backend':'same-container temporary local filesystem; no network NAS',
          'samplesPerSize':1,'rssSamplingIntervalSeconds':0.005,'cases':cases},indent=2))
