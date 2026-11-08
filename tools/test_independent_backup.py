"""Exercise independent SQLite online backup and fresh isolated restore."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest


class IndependentBackupTest(unittest.TestCase):
    def test_restore_without_live_database_or_replica(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live, backup, restored = (root / name for name in ('live.db', 'independent.db', 'restored.db'))
            with closing(sqlite3.connect(live)) as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT NOT NULL)')
                db.executemany('INSERT INTO records VALUES (?, ?)', [(1, 'alpha'), (2, 'beta')])
                db.commit()
                with closing(sqlite3.connect(backup)) as target:
                    db.backup(target)
            live.unlink()  # No replica or live database participates in the restore.
            with closing(sqlite3.connect(f'{backup.as_uri()}?mode=ro', uri=True)) as source:
                with closing(sqlite3.connect(restored)) as target:
                    source.backup(target)
            with closing(sqlite3.connect(f'{restored.as_uri()}?mode=ro', uri=True)) as db:
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
                self.assertEqual(db.execute('SELECT * FROM records ORDER BY id').fetchall(), [(1, 'alpha'), (2, 'beta')])


if __name__ == '__main__':
    unittest.main()
