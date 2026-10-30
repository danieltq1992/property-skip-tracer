import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

import httpx
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app
import research
from evidence_store import EvidenceStore, ReviewedRecord
from providers import LicensedProviders
from rate_limits import RateLimitStore

ROW = dict(street='12 Example St', city='Example City', state='FL', zip_code='32000')


def record(kind='parcel_record', **values):
    values.setdefault('review_method','human')
    if kind == 'owner_research_exhausted': values.setdefault('entity','Example Holdings LLC')
    return ReviewedRecord(kind=kind, source='Synthetic county / work record', source_url='https://example.invalid/record',
        observed_at='2026-10-29T12:00:00Z', excerpt='Synthetic original record used for software testing only.',
        attested=True, **values).record()


def owner():
    return record(owner='Example Holdings LLC', address_match=True, official_source=True)


class WorkflowTests(unittest.TestCase):
    def test_manual_principal_does_not_promote_agent(self):
        agent = record('principal_relationship', entity='Example Holdings LLC', person='Example Agent',
            role='Registered agent', relationship='registered_agent', official_source=True)
        d = research.assemble(ROW, [owner(), agent])
        self.assertEqual(d['contacts'], [])

    def test_confirmed_owner_phone_stays_ahead_of_secondary(self):
        principal = record('principal_relationship', entity='Example Holdings LLC', person='Example Owner',
            relationship='beneficial_owner', official_source=True)
        phone = record('phone',person='Example Owner',number='212-555-0100',identity_address='Mock identity address',phone_association_confirmed=True)
        authority = record('repair_authority',entity='Example Holdings LLC',person='Example Manager',official_source=True)
        exhausted = record('owner_research_exhausted')
        d = research.assemble(ROW,[owner(),principal,phone,authority,exhausted])
        self.assertEqual(d['primary_decision_maker'],'Example Owner')
        self.assertEqual(d['primary_phone'],'212-555-0100')
        self.assertEqual(len(d['contacts']),1)
        self.assertEqual(d['phone_candidates'][0]['verification_status'],'user_confirmed_person_phone')
        self.assertNotEqual(d['overall_confidence'],'High')

    def test_secondary_requires_exhausted_research_and_authority(self):
        authority=record('repair_authority',entity='Example Holdings LLC',person='Example Manager',official_source=True)
        phone=record('phone',person='Example Manager',number='212-555-0101',identity_address='Mock address',phone_association_confirmed=True)
        d=research.assemble(ROW,[owner(),authority,phone])
        self.assertEqual(d['primary_phone'],'')
        self.assertEqual(d['contacts'],[])
        d=research.assemble(ROW,[owner(),authority,phone,record('owner_research_exhausted')])
        self.assertEqual(d['primary_decision_maker'],'Example Manager')
        self.assertEqual(d['primary_phone'],'212-555-0101')
        self.assertEqual(d['contacts'][0]['relationship'],'secondary_authorized')

    def test_browser_agent_cannot_claim_human_phone_confirmation(self):
        with self.assertRaises(ValidationError):
            record('phone',person='Example Person',number='212-555-0100',identity_address='Mock address',
                phone_association_confirmed=True,review_method='browser_assisted')
        r=record(owner='Example LLC',address_match=True,official_source=True,review_method='browser_assisted')
        self.assertEqual(r['verification_status'],'browser_observed_source')
        self.assertEqual(research.assemble(ROW,[r])['owner_status'],'browser_observed_parcel_requires_review')

    def test_missing_attestation_and_unsupported_owner_claim_rejected(self):
        with self.assertRaises(ValidationError):
            ReviewedRecord(kind='parcel_record',source='Mock',source_url='https://example.invalid',observed_at='2026-10-29',
                excerpt='Synthetic source wording',owner='Example LLC',address_match=True)
        with self.assertRaises(ValidationError):
            record('principal_relationship',entity='Example LLC',person='Example Person',relationship='beneficial_owner')

    def test_record_storage_retraction_and_history_survive_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'research.sqlite3'
            store=EvidenceStore(path); first=owner(); store.add(ROW,first)
            store.snapshot(ROW,research.assemble(ROW,[first]))
            second=EvidenceStore(path)
            self.assertEqual(len(second.records(ROW)),1)
            self.assertTrue(second.latest(ROW))
            self.assertFalse(second.retract(dict(ROW,street='99 Wrong St'),first['record_id']))
            self.assertTrue(second.retract(ROW,first['record_id']))
            self.assertEqual(second.records(ROW),[])
            self.assertTrue(second.latest(ROW))  # history retained

    def test_persistent_rate_limit_deadline(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'limits.sqlite3'
            RateLimitStore(path).hold('example.invalid',120)
            self.assertGreater(RateLimitStore(path).deadline('example.invalid'),__import__('time').time()+100)

    def test_retrying_the_same_finding_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            store=EvidenceStore(Path(folder)/'evidence.sqlite3')
            first, second=owner(), owner()
            store.add(ROW,first); store.add(ROW,second)
            self.assertEqual(first['record_id'],second['record_id'])
            self.assertEqual(len(store.records(ROW)),1)

    def test_street_aliases_normalize_but_suites_remain_distinct(self):
        self.assertEqual(research.street_key('12 Example Street'),research.street_key('12 Example St'))
        self.assertNotEqual(research.street_key('12 Example St Suite 2'),research.street_key('12 Example St Suite 3'))

    def test_exhausted_research_for_another_owner_does_not_unlock_secondary(self):
        authority=record('repair_authority',entity='Example Holdings LLC',person='Example Manager',official_source=True)
        exhausted=record('owner_research_exhausted',entity='Different Owner LLC')
        d=research.assemble(ROW,[owner(),authority,exhausted])
        self.assertEqual(d['contacts'],[])
        self.assertEqual(d['owner_contact_status'],'research_required')

    def test_human_review_takes_priority_over_browser_observation_of_same_owner(self):
        browser_record=record(owner='Example Holdings LLC',address_match=True,official_source=True,review_method='browser_assisted')
        d=research.assemble(ROW,[browser_record,owner()])
        self.assertEqual(d['owner_status'],'user_reviewed_official_source')


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def mock(self, handler, method):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch('providers.httpx.AsyncClient',return_value=client):
            return await method()

    async def test_disabled_providers_make_no_requests(self):
        provider=LicensedProviders({})
        with patch('providers.public_get',new_callable=AsyncMock) as get:
            self.assertEqual(await provider.parcels(ROW),[])
            self.assertEqual(await provider.companies('Example LLC'),[])
            get.assert_not_called()

    async def test_regrid_nationwide_record_is_licensed_not_official(self):
        calls=[]
        def handler(request):
            calls.append(request)
            return httpx.Response(200,json={'parcels':{'features':[{'properties':{'fields':{
                'address':ROW['street'],'scity':ROW['city'],'state2':'FL','szip':'32000','owner':'Example LLC','parcelnumb':'MOCK-1'}}}]}})
        p=LicensedProviders({'regrid':{'enabled':True,'token':'test-secret'}})
        records=await self.mock(handler,lambda:p.parcels(ROW))
        self.assertEqual(records[0]['kind'],'licensed_parcel')
        self.assertEqual(calls[0].headers['Authorization'],'Bearer test-secret')
        self.assertEqual(calls[0].url.params['path'],'/us/fl')
        self.assertNotIn('test-secret',json.dumps(records))
        self.assertNotIn('token',calls[0].url.params)
        self.assertEqual(research.assemble(ROW,records)['owner_status'],'licensed_parcel_record_requires_county_check')

    async def test_regrid_ambiguous_parcels_never_choose_first(self):
        def handler(request):
            return httpx.Response(200,json={'parcels':{'features':[
                {'properties':{'fields':{'address':ROW['street'],'scity':ROW['city'],'state2':'FL','szip':'32000','owner':n}}}
                for n in ('Person A','Person B')]}})
        p=LicensedProviders({'regrid':{'enabled':True,'token':'test-secret'}})
        records=await self.mock(handler,lambda:p.parcels(ROW))
        self.assertEqual(records[0]['status'],'manual_review')

    async def test_opencorporates_roles_are_candidates_and_search_is_nationwide(self):
        calls=[]
        def handler(request):
            calls.append(request)
            if request.url.path.endswith('/search'):
                return httpx.Response(200,json={'results':{'total_count':1,'companies':[{'company':{
                    'name':'Example LLC','jurisdiction_code':'us_de','company_number':'MOCK-1'}}]}})
            return httpx.Response(200,json={'results':{'company':{'officers':[{'officer':{
                'name':'Example Attorney','position':'registered agent'}}]}}})
        p=LicensedProviders({'opencorporates':{'enabled':True,'token':'test-secret'}})
        records=await self.mock(handler,lambda:p.companies('Example LLC'))
        self.assertNotIn('jurisdiction_code',calls[0].url.params)
        self.assertEqual(calls[0].headers['X-API-TOKEN'],'test-secret')
        self.assertEqual(records[-1]['role'],'registered agent')
        d=research.assemble(ROW,[owner()]+records)
        self.assertEqual(d['primary_decision_maker'],'')
        self.assertEqual(d['contacts'],[])
        self.assertNotIn('test-secret',json.dumps(records))

    async def test_provider_auth_failure_is_reported_without_secret(self):
        def handler(request): return httpx.Response(401,json={'error':'denied'})
        p=LicensedProviders({'regrid':{'enabled':True,'token':'test-secret'}})
        records=await self.mock(handler,lambda:p.parcels(ROW))
        self.assertEqual(records[0]['status'],'failed')
        self.assertNotIn('test-secret',json.dumps(records))


class ResearchApiTests(unittest.TestCase):
    def test_manual_workflow_settings_and_notes_preservation(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with patch.object(app,'BASE_DIR',root),patch.object(app,'JOB_FILE',root/'jobs.sqlite3'),patch.object(app,'LEADS_FILE',root/'leads.json'),patch.object(app,'CONFIG_FILE',root/'config.json'):
                with TestClient(app.app) as client:
                    app.CONFIG_FILE.write_text('{"gemini_api_key":"fake-gemini","other_setting":true}')
                    response=client.post('/api/settings/sources',json={'regrid':{'enabled':False,'token':'fake-provider-secret'},'opencorporates':{'enabled':False,'token':''}})
                    self.assertEqual(response.status_code,200)
                    self.assertTrue(json.loads(app.CONFIG_FILE.read_text())['other_setting'])
                    self.assertNotIn('fake-provider-secret',client.get('/api/settings/sources').text)
                    app.persist_lead(dict(address='12 Example St, Example City, FL 32000',notes='Keep my handwritten notes'),research.property_key(ROW))
                    finding=ReviewedRecord(kind='parcel_record',review_method='human',source='Mock county',source_url='https://example.invalid',
                        observed_at='2026-10-29T12:00:00Z',excerpt='Synthetic parcel record showing Example LLC',owner='Example LLC',
                        address_match=True,official_source=True,attested=True)
                    response=client.post('/api/research/evidence',json={'subject':ROW,'record':finding.model_dump(mode='json')})
                    self.assertEqual(response.status_code,200,response.text)
                    self.assertEqual(response.json()['dossier']['owner_of_record'],'Example LLC')
                    record_id=response.json()['dossier']['evidence'][0]['record_id']
                    self.assertEqual(client.post('/api/research/save',json=ROW).status_code,200)
                    self.assertEqual(app.load_saved_leads()[0]['notes'],'Keep my handwritten notes')
                    self.assertEqual(len(app.load_saved_leads()),1)
                    self.assertEqual(client.post('/api/research/context',json=ROW).json()['dossier']['owner_of_record'],'Example LLC')
                    response=client.post(f'/api/research/evidence/{record_id}/retract',json=ROW)
                    self.assertEqual(response.json()['dossier']['owner_of_record'],'')
                    self.assertEqual(client.post('/api/research/context',json=dict(ROW,state='')).status_code,400)


if __name__ == '__main__': unittest.main()
