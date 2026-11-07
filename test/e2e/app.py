"""Disposable SQLite writer/reader. Cluster-internal only; contains no real data."""
import json
import os
import signal
import sqlite3
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB = os.environ["DB_PATH"]
WRITER = os.environ["ROLE"] == "active"


@contextmanager
def connect():
    connection = sqlite3.connect(DB if WRITER else "file:" + DB + "?mode=ro", uri=not WRITER, timeout=5)
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        with connection:
            yield connection
    finally:
        connection.close()


if WRITER:
    with connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("CREATE TABLE IF NOT EXISTS records (id INTEGER PRIMARY KEY, value TEXT UNIQUE NOT NULL)")


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            with connect() as db:
                if self.path == "/integrity":
                    result = [r[0] for r in db.execute("PRAGMA integrity_check")]
                    self.send_json(200 if result == ["ok"] else 500, {"integrity": result})
                else:
                    records = list(db.execute("SELECT id, value FROM records ORDER BY id"))
                    self.send_json(200, {"role": os.environ["ROLE"], "node": os.environ["NODE"], "records": records})
        except sqlite3.Error as error:
            self.send_json(503, {"error": str(error)})

    def do_POST(self):
        if not WRITER:
            self.send_json(405, {"error": "standby is read-only"})
            return
        if self.path == "/crash":
            # Test-only failure injection: no graceful shutdown or finalizers.
            # A same-namespace signal to PID 1 can be ignored by Linux.
            os._exit(137)
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 4096:
                self.send_json(400, {"error": "body must be 1..4096 bytes"})
                return
            value = json.loads(self.rfile.read(size))["value"]
            if not isinstance(value, str) or not value:
                raise ValueError("nonempty string required")
            with connect() as db:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("INSERT INTO records(value) VALUES (?)", (value,))
            self.send_json(201, {"value": value})
        except (ValueError, KeyError, sqlite3.Error) as error:
            self.send_json(400, {"error": str(error)})


server = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
server.serve_forever()
server.server_close()
