"""Append-only user research records, independently retained from saved leads."""
import json
import hashlib
import sqlite3
import uuid
from contextlib import closing
from typing import Literal
from datetime import datetime

from pydantic import BaseModel, Field, HttpUrl, model_validator

from research import evidence, property_key


class ReviewedRecord(BaseModel):
    kind: Literal['parcel_record','principal_relationship','corporate_filing','phone','repair_authority','owner_research_exhausted']
    source: str = Field(min_length=2, max_length=150)
    source_url: HttpUrl
    observed_at: str = Field(min_length=10, max_length=40)
    excerpt: str = Field(min_length=10, max_length=12000)
    owner: str = Field(default='', max_length=250)
    entity: str = Field(default='', max_length=250)
    person: str = Field(default='', max_length=250)
    role: str = Field(default='', max_length=200)
    number: str = Field(default='', max_length=40)
    apn: str = Field(default='', max_length=150)
    mailing_address: str = Field(default='', max_length=500)
    address_match: bool = False
    official_source: bool = False
    relationship: Literal['unknown','beneficial_owner','registered_agent','officer','attorney','manager'] = 'unknown'
    attested: bool = False
    identity_address: str = Field(default='', max_length=500)
    phone_association_confirmed: bool = False
    underlying_database: str = Field(default='', max_length=150)
    review_method: Literal['human','browser_assisted']

    @model_validator(mode='after')
    def check(self):
        try:
            datetime.fromisoformat(self.observed_at.replace('Z', '+00:00'))
        except ValueError:
            raise ValueError('Enter the date/time when you inspected the record.') from None
        if not self.attested:
            raise ValueError('Confirm that you inspected this source record.')
        if self.kind == 'parcel_record' and (not self.owner or not self.address_match):
            raise ValueError('A parcel record needs an owner and a matching subject property address.')
        if self.kind in ('principal_relationship','corporate_filing','repair_authority') and (not self.entity or not self.person):
            raise ValueError('Supply both the entity and the person named in the record.')
        if self.kind == 'principal_relationship' and self.relationship == 'beneficial_owner' and not self.official_source:
            raise ValueError('Owner relationships require inspected official evidence, not a people-search hint.')
        if self.kind == 'phone' and (not self.person or len(''.join(c for c in self.number if c.isdigit())) < 10):
            raise ValueError('Supply the specific person and a valid phone candidate.')
        if self.phone_association_confirmed and (self.kind != 'phone' or not self.identity_address):
            raise ValueError('A confirmed phone association requires a corroborated identity address.')
        if self.phone_association_confirmed and self.review_method != 'human':
            raise ValueError('Browser-observed phone records remain candidates until a human reviews the association.')
        if self.kind == 'repair_authority' and not self.official_source:
            raise ValueError('Repair authority requires inspected official evidence.')
        if self.kind == 'owner_research_exhausted' and not self.entity:
            raise ValueError('Name the legal owner whose contact research was exhausted.')
        return self

    def record(self):
        data = self.model_dump(mode='json')
        kind = 'reviewed_parcel' if self.kind == 'parcel_record' else self.kind
        return evidence(kind, data.pop('source'), data.pop('source_url'), **{k:v for k,v in data.items() if k != 'kind'},
            verification_status='user_reviewed_source' if self.review_method == 'human' else 'browser_observed_source',
            ingestion_method='manual_research' if self.review_method == 'human' else 'browser_assisted', record_id=uuid.uuid4().hex)


class EvidenceStore:
    def __init__(self, path):
        self.path = str(path)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS research_evidence(id TEXT PRIMARY KEY, property_key TEXT, record TEXT, retracted INTEGER DEFAULT 0)')
            db.execute('CREATE TABLE IF NOT EXISTS research_snapshots(id INTEGER PRIMARY KEY, property_key TEXT, dossier TEXT)')

    def add(self, row, record):
        # A network retry of the same inspected finding must not create another record.
        facts = {k:v for k,v in record.items() if k not in ('record_id','retrieved_at')}
        digest = hashlib.sha256((property_key(row) + json.dumps(facts,sort_keys=True)).encode()).hexdigest()
        record['record_id'] = 'record_' + digest[:32]
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT OR IGNORE INTO research_evidence(id,property_key,record) VALUES(?,?,?)',
                (record['record_id'],property_key(row),json.dumps(record)))

    def records(self, row):
        with closing(sqlite3.connect(self.path)) as db:
            return [json.loads(r[0]) for r in db.execute('SELECT record FROM research_evidence WHERE property_key=? AND retracted=0 ORDER BY rowid', (property_key(row),))]

    def retract(self, row, record_id):
        with closing(sqlite3.connect(self.path)) as db, db:
            changed = db.execute('UPDATE research_evidence SET retracted=1 WHERE property_key=? AND id=? AND retracted=0', (property_key(row),record_id)).rowcount
        return bool(changed)

    def snapshot(self, row, dossier):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT INTO research_snapshots(property_key,dossier) VALUES(?,?)', (property_key(row),json.dumps(dossier)))

    def latest(self, row):
        with closing(sqlite3.connect(self.path)) as db:
            record = db.execute('SELECT dossier FROM research_snapshots WHERE property_key=? ORDER BY id DESC LIMIT 1', (property_key(row),)).fetchone()
        return json.loads(record[0]) if record else None
