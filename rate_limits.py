"""Persist source Retry-After deadlines across app restarts."""
import sqlite3
import time
from contextlib import closing


class RateLimitStore:
    def __init__(self, path):
        self.path = str(path)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS source_cooldowns(host TEXT PRIMARY KEY, until REAL)')

    def deadline(self, host):
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute('SELECT until FROM source_cooldowns WHERE host=?', (host,)).fetchone()
        return row[0] if row else 0

    def hold(self, host, seconds):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT INTO source_cooldowns VALUES(?,?) ON CONFLICT(host) DO UPDATE SET until=MAX(until,excluded.until)', (host,time.time()+seconds))
