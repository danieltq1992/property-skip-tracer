"""SQLite-backed row queue. Run one application process; enforced by a file lock."""
import asyncio
import io
import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

import openpyxl
from research import now, property_key, validation_error


def safe_cell(value):
    value = str(value) if value is not None else ''
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value


class JobStore:
    def __init__(self, path):
        self.path = str(path)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, status TEXT, created_at TEXT);
                CREATE TABLE IF NOT EXISTS rows(id INTEGER PRIMARY KEY, job_id TEXT, position INTEGER,
                  property_key TEXT, payload TEXT, status TEXT, error TEXT DEFAULT '', result TEXT,
                  attempts INTEGER DEFAULT 0, UNIQUE(job_id, position));
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA journal_mode=WAL')
                yield db
        finally:
            db.close()

    def create(self, rows):
        job_id = uuid.uuid4().hex
        seen = set()
        with self.db() as db:
            db.execute('INSERT INTO jobs VALUES(?,?,?)', (job_id, 'queued', now()))
            for position, row in enumerate(rows):
                key = property_key(row)
                error = validation_error(row)
                status = 'invalid' if error else 'duplicate' if key in seen else 'queued'
                if status == 'duplicate':
                    error = 'Duplicate subject property in this batch.'
                db.execute('INSERT INTO rows(job_id,position,property_key,payload,status,error) VALUES(?,?,?,?,?,?)',
                    (job_id, position, key, json.dumps(row), status, error))
                if not error:
                    seen.add(key)
        return job_id

    def recover(self):
        with self.db() as db:
            db.execute("UPDATE rows SET status='queued' WHERE status='running'")
            db.execute("UPDATE jobs SET status='paused' WHERE status IN ('queued','running','cancelling')")

    def list(self):
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM jobs ORDER BY created_at DESC')]

    def get(self, job_id):
        with self.db() as db:
            job = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not job:
                raise KeyError(job_id)
            rows = [dict(r) for r in db.execute('SELECT * FROM rows WHERE job_id=? ORDER BY position', (job_id,))]
        for row in rows:
            row['payload'] = json.loads(row['payload'])
            row['result'] = json.loads(row['result']) if row['result'] else None
        counts = {s: sum(r['status'] == s for r in rows) for s in
            ('queued','running','succeeded','needs_review','failed','invalid','duplicate')}
        return dict(job, rows=rows, counts=counts, total=len(rows))

    def action(self, job_id, action):
        self.get(job_id)
        with self.db() as db:
            if action == 'cancel':
                db.execute("UPDATE jobs SET status=CASE WHEN status='running' THEN 'cancelling' ELSE 'paused' END WHERE id=?", (job_id,))
            elif action in ('resume', 'retry'):
                if action == 'retry':
                    db.execute("UPDATE rows SET status='queued',error='' WHERE job_id=? AND status='failed'", (job_id,))
                db.execute("UPDATE jobs SET status='queued' WHERE id=? AND status NOT IN ('running','cancelling')", (job_id,))
            else:
                raise ValueError('Unknown action')

    async def step(self, investigate, save):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            job = db.execute("SELECT * FROM jobs WHERE status IN ('queued','running') ORDER BY created_at LIMIT 1").fetchone()
            if not job:
                return False
            row = db.execute("SELECT * FROM rows WHERE job_id=? AND status='queued' ORDER BY position LIMIT 1", (job['id'],)).fetchone()
            if not row:
                db.execute("UPDATE jobs SET status='completed' WHERE id=?", (job['id'],))
                return True
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (job['id'],))
            db.execute("UPDATE rows SET status='running',attempts=attempts+1 WHERE id=?", (row['id'],))
        result, error, status = None, '', 'failed'
        try:
            result = await asyncio.wait_for(investigate(json.loads(row['payload'])), timeout=75)
            if any(r.get('status') == 'failed' for r in result['evidence']):
                raise RuntimeError('Source retrieval failed. Retry when the service is available.')
            # A completed research attempt can legitimately remain unresolved.
            status = 'needs_review' if not result['owner_of_record'] or result['conflicts'] else 'succeeded'
            save(result, json.loads(row['payload']))
        except asyncio.CancelledError:
            raise  # recovery returns the active row to queued after process interruption
        except Exception:
            error = 'Research or local save failed. Check service availability and local file integrity, then retry.'
        with self.db() as db:
            db.execute('UPDATE rows SET status=?,error=?,result=? WHERE id=?',
                (status, error, json.dumps(result) if result else None, row['id']))
            current = db.execute('SELECT status FROM jobs WHERE id=?', (job['id'],)).fetchone()[0]
            remaining = db.execute("SELECT COUNT(*) FROM rows WHERE job_id=? AND status='queued'", (job['id'],)).fetchone()[0]
            db.execute('UPDATE jobs SET status=? WHERE id=?',
                ('paused' if current == 'cancelling' else 'running' if remaining else 'completed', job['id']))
        return True

    def export(self, job_id):
        job = self.get(job_id)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'All batch rows'
        ws.append(['Sheet row','Street','City','State','ZIP','Owner hint','Phone hint','APN hint','Tax mailing hint','Type','Notes',
            'Status','Error','Owner record','Primary phone','Confidence','Evidence JSON'])
        for r in job['rows']:
            p, d = r['payload'], r['result'] or {}
            values = [p.get(k,'') for k in ('row_number','street','city','state','zip_code','known_owner_or_llc','known_phone',
                'parcel_apn_hint','mailing_address_hint','property_type','notes')]
            values += [r['status'], r['error'], d.get('owner_of_record',''), d.get('primary_phone',''),
                d.get('overall_confidence',''), json.dumps(d.get('evidence',[]))]
            ws.append([safe_cell(v) for v in values])
        ws.freeze_panes = 'A2'
        ws.auto_filter.ref = ws.dimensions
        stream = io.BytesIO()
        wb.save(stream)
        return stream.getvalue()


class WorkerLock:
    def __init__(self, path):
        self.path = Path(path)

    def acquire(self):
        self.file = self.path.open('a+b')
        self.file.seek(0)
        self.file.write(b'0')
        self.file.flush()
        self.file.seek(0)
        try:
            if __import__('os').name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError('Another PropTrace worker is already using this job store.') from None

    def release(self):
        self.file.close()
