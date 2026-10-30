"""Owner-first research. Retrieval records are the only source of factual claims."""
import json
import re
import asyncio
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Protocol

import httpx
from pydantic import BaseModel

GEO = 'https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates'
PARCEL = 'https://services.nconemap.gov/secure/rest/services/NC1Map_Parcels/FeatureServer/1/query'
US_STATES = set('AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY'.split())
SOURCE_SLOTS = asyncio.Semaphore(3)
SOURCE_COOLDOWNS = {}
RATE_LIMIT_STORE = None


async def public_get(client, url, **kwargs):
    """Honor throttling without repeatedly sending denied requests."""
    host = httpx.URL(url).host
    async with SOURCE_SLOTS:
        if (SOURCE_COOLDOWNS.get(host, 0) > time.monotonic()
                or RATE_LIMIT_STORE and RATE_LIMIT_STORE.deadline(host) > time.time()):
            raise ValueError('Source rate limit cooldown active')
        response = await client.get(url, **kwargs)
        if response.status_code in (429, 503):
            raw = response.headers.get('Retry-After', '60')
            try:
                seconds = float(raw)
            except ValueError:
                try:
                    seconds = (parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds()
                except (ValueError, TypeError):
                    seconds = 60
            SOURCE_COOLDOWNS[host] = time.monotonic() + max(1, seconds)
            if RATE_LIMIT_STORE:
                RATE_LIMIT_STORE.hold(host, max(1, seconds))
        response.raise_for_status()
        return response


def now():
    return datetime.now(timezone.utc).isoformat()


def street_key(value):
    # Conservative matching: ambiguous abbreviations require manual review.
    value = re.sub(r'\b(suite|ste|unit|apt)\b', 'unit', value.lower())
    aliases = {'street':'st','avenue':'ave','road':'rd','boulevard':'blvd','drive':'dr',
               'lane':'ln','court':'ct','highway':'hwy','north':'n','south':'s','east':'e','west':'w'}
    value = re.sub(r'\b[a-z]+\b', lambda m: aliases.get(m[0], m[0]), value)
    return re.sub(r'[^a-z0-9-]', '', value)


def property_key(row):
    # ZIP is optional and must not create duplicates for the same subject address.
    return '|'.join(street_key(row.get(k, '')) for k in ('street', 'city', 'state'))


def validation_error(row):
    missing = [k for k in ('street', 'city', 'state') if not str(row.get(k, '')).strip()]
    if missing:
        return 'Missing ' + ', '.join(missing) + '; supply the subject property address.'
    if row['state'].upper() not in US_STATES:
        return 'State must be a two-letter US abbreviation.'
    return ''


def evidence(kind, source, url='', **facts):
    return dict(kind=kind, source=source, source_url=url, retrieved_at=now(), **facts)


class CountySources:
    """Public geocoding and NC parcel adapter. No people-search scraping."""
    async def retrieve(self, row):
        records = []
        async with httpx.AsyncClient(timeout=15) as client:
            address = f"{row['street']}, {row['city']}, {row['state']} {row.get('zip_code', '')}"
            try:
                response = await public_get(client, GEO, params=dict(SingleLine=address, countryCode='USA',
                    outFields='StAddr,City,RegionAbbr,Postal,Addr_type', maxLocations=3, f='json'))
                response.raise_for_status()
                candidates = response.json().get('candidates', [])
                matches = [c for c in candidates if c.get('score', 0) >= 95
                    and street_key(c.get('attributes', {}).get('StAddr', '')) == street_key(row['street'])
                    and street_key(c.get('attributes', {}).get('City', '')) == street_key(row['city'])
                    and c.get('attributes', {}).get('RegionAbbr', '').upper() == row['state'].upper()
                    and (not row.get('zip_code') or c.get('attributes', {}).get('Postal', '')[:5] == row['zip_code'][:5])]
                if len(matches) != 1:
                    records.append(evidence('retrieval_status', 'ArcGIS geocoder', str(response.url),
                        status='manual_review', detail='No unique exact street/city/state match; parcel lookup withheld.'))
                    return records
                candidate = matches[0]
                records.append(evidence('geocode', 'ArcGIS geocoder', str(response.url),
                    status='matched', address=candidate.get('address'), coordinates=candidate['location']))
                if row['state'].upper() != 'NC':
                    records.append(evidence('retrieval_status', 'County parcel adapter', status='not_configured',
                        detail='No authorized county parcel adapter configured for this state. Use the county assessor/deed portal.'))
                    return records
                point = candidate['location']
                response = await public_get(client, PARCEL, params=dict(geometry=f"{point['x']},{point['y']}",
                    geometryType='esriGeometryPoint', inSR=4326, spatialRel='esriSpatialRelIntersects',
                    outFields='ownname,ownname2,mailadd,mcity,mstate,mzip,parno,siteadd,scity,cntyname', f='json'))
                response.raise_for_status()
                payload = response.json()
                if payload.get('error'):
                    raise ValueError('Parcel service reported an error')
                features = [f['attributes'] for f in payload.get('features', [])]
                exact = [f for f in features if street_key(f.get('siteadd') or '') == street_key(row['street'])
                    and street_key(f.get('scity') or '') == street_key(row['city'])
                    and (not row.get('parcel_apn_hint') or str(f.get('parno')) == row['parcel_apn_hint'])]
                if len(exact) != 1:
                    records.append(evidence('retrieval_status', 'NC OneMap', str(response.url), status='manual_review',
                        detail='Intersecting parcel did not uniquely match the subject street/city/APN. No owner selected.'))
                else:
                    f = exact[0]
                    records.append(evidence('official_parcel', 'NC OneMap county-contributed parcel record', str(response.url),
                        address_match=True, owner=f.get('ownname') or '', co_owner=f.get('ownname2') or '',
                        apn=f.get('parno') or '', county=f.get('cntyname') or '',
                        mailing_address=', '.join(str(f.get(k) or '') for k in ('mailadd','mcity','mstate','mzip')),
                        raw_record=f, verification_status='official_record_requires_currency_check'))
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                # Never log request contents or API secrets in failures.
                records.append(evidence('retrieval_status', 'Public source retrieval', status='failed',
                    detail='Source unavailable, timed out, or returned invalid data. Retry or verify manually.'))
        return records


class PublicSources(CountySources):
    def __init__(self, config=None, reviewed_records=None):
        self.config = config or {}
        self.reviewed_records = reviewed_records or []

    async def retrieve(self, row):
        from providers import LicensedProviders
        records = await super().retrieve(row)
        records += self.reviewed_records
        providers = LicensedProviders(self.config)
        if not any(r['kind'] in ('official_parcel','reviewed_parcel') and r.get('address_match') for r in records):
            # A unique geocode must precede any licensed parcel selection.
            if any(r['kind'] == 'geocode' and r.get('status') == 'matched' for r in records):
                records += await providers.parcels(row)
        dossier = assemble(row, records)
        if dossier['is_corporate_or_llc']:
            records += await providers.companies(dossier['owner_of_record'])
        return records


class Advisor(Protocol):
    async def advise(self, records: list) -> dict: ...


class Advice(BaseModel):
    next_steps: list[str]


class GeminiAdvisor:
    def __init__(self, api_key, model):
        self.api_key, self.model = api_key, model

    async def advise(self, records):
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=self.api_key, http_options=types.HttpOptions(timeout=20000))
        try:
            async with client.aio as session:
                response = await session.models.generate_content(model=self.model,
                    contents='Recommend verification steps only. No ownership or phone assertions. Evidence: ' + json.dumps(records),
                    config=types.GenerateContentConfig(response_mime_type='application/json', response_schema=Advice))
                parsed = Advice.model_validate_json(response.text)
                return dict(status='completed', hypotheses=parsed.next_steps)
        finally:
            client.close()


def assemble(row, records):
    """A source adapter supplies typed records. AI never enters this function."""
    records = list(records)
    if row.get('known_owner_or_llc'):
        records.append(evidence('user_hint', 'User input', owner=row['known_owner_or_llc'], verification_status='unverified'))
    if row.get('known_phone'):
        records.append(evidence('phone', 'User input', number=row['known_phone'], person='',
            verification_status='unverified', detail='Person association not established'))
    parcels = [r for r in records if r['kind'] in ('official_parcel','reviewed_parcel','licensed_parcel') and r.get('address_match') and r.get('owner')]
    parcels.sort(key=lambda r: 0 if r['kind'] == 'official_parcel' else 1 if r['kind'] == 'reviewed_parcel'
        and r.get('official_source') and r.get('review_method','human') == 'human' else 2
        if r['kind'] == 'reviewed_parcel' and r.get('review_method','human') == 'human' else 3)
    owners = {r['owner'].casefold() for r in parcels}
    conflicts = []
    if len(owners) > 1:
        conflicts.append('Conflicting official owner records; manual review required.')
    owner = parcels[0]['owner'] if len(owners) == 1 else ''
    owner_status = ('browser_observed_parcel_requires_review' if parcels[0].get('review_method') == 'browser_assisted'
        else 'user_reviewed_official_source' if parcels[0]['kind'] == 'reviewed_parcel' and parcels[0].get('official_source')
        else 'user_reviewed_source' if parcels[0]['kind'] == 'reviewed_parcel'
        else 'licensed_parcel_record_requires_county_check' if parcels[0]['kind'] == 'licensed_parcel'
        else 'official_parcel_record') if owner else 'unresolved'
    web_owners = {r['owner'].casefold() for r in records if r.get('owner') and r['kind'] in ('search_snippet', 'people_search')}
    if len(web_owners | owners) > 1:
        conflicts.append('Owner names differ across sources. Search snippets do not establish legal ownership.')
    corporate = bool(re.search(r'\b(LLC|INC|CORP|LP|LLP|TRUST|PARTNERSHIP|LTD|ASSOCIATION)\b', owner, re.I))
    principals = [dict(r, verification_status='candidate_requires_ownership_review') for r in records
        if r['kind'] == 'corporate_filing' and r.get('entity', '').casefold() == owner.casefold()]
    # Officers and agents are evidence of roles, never proof of beneficial ownership.
    contacts = []
    if owner and not corporate:
        contacts.append(dict(full_name=owner, role='Owner candidate in licensed parcel data' if owner_status == 'licensed_parcel_record_requires_county_check' else 'Owner in parcel evidence; inspect provenance and currency',
            is_primary_decision_maker=True, relationship='owner_record', verification_evidence='Matched parcel record; inspect provenance and currency',
            mailing_or_home_address=parcels[0].get('mailing_address',''), phones=[], emails=[]))
    def same_entity(record):
        return re.sub(r'[^a-z0-9]', '', record.get('entity','').casefold()) == re.sub(r'[^a-z0-9]', '', owner.casefold())

    if owner and corporate:
        for r in records:
            if (r['kind'] == 'principal_relationship' and same_entity(r) and r.get('official_source')
                    and r.get('attested') and r.get('review_method', 'human') == 'human' and r.get('relationship') == 'beneficial_owner' and r.get('person')):
                contacts.append(dict(full_name=r['person'],role='Beneficial owner — user-reviewed official source',
                    relationship='beneficial_owner',is_primary_decision_maker=True,verification_evidence=r.get('excerpt',''),
                    mailing_or_home_address=r.get('identity_address',''),phones=[],emails=[]))
    exhausted = any(r['kind'] == 'owner_research_exhausted' and r.get('attested') and same_entity(r) for r in records)
    secondary = [r for r in records if r['kind'] == 'repair_authority' and same_entity(r)
        and r.get('attested') and r.get('review_method', 'human') == 'human' and r.get('official_source') and r.get('person')]
    owner_names = {c['full_name'].casefold() for c in contacts}
    confirmed_owner_phone = any(r['kind'] == 'phone' and r.get('attested') and r.get('review_method', 'human') == 'human' and r.get('phone_association_confirmed')
        and r.get('identity_address') and r.get('person','').casefold() in owner_names for r in records)
    if exhausted and not confirmed_owner_phone:
        for r in secondary:
            contacts.append(dict(full_name=r['person'],role='Secondary contact — documented repair authority',
                relationship='secondary_authorized',is_primary_decision_maker=False,verification_evidence=r.get('excerpt',''),
                mailing_or_home_address=r.get('identity_address',''),phones=[],emails=[]))
    phones = []
    for record in records:
        if record['kind'] != 'phone':
            continue
        p = dict(record, verification_status='unverified', confidence='Unverified',
            phone_type='Unknown', is_personal_cell=False, carrier_or_source=record['source'])
        # Independent database lineage, original records, name AND address evidence are required.
        if (record.get('person') and record.get('name_match') and record.get('address_match')
                and record.get('original_record') and len(set(record.get('independent_lineages', []))) >= 2):
            p['verification_status'] = 'corroborated_person_phone'
            p['confidence'] = 'Corroborated; review before outreach'
        if record.get('attested') and record.get('review_method', 'human') == 'human' and record.get('phone_association_confirmed') and record.get('identity_address'):
            p['verification_status'] = 'user_confirmed_person_phone'
            p['confidence'] = 'User reviewed; not independently verified by app'
        phones.append(p)
        for contact in contacts:
            if record.get('person', '').casefold() == contact['full_name'].casefold():
                contact['phones'].append(p)
    chosen_contact, chosen_phone = '', ''
    owner_contacts = [c for c in contacts if c.get('relationship') != 'secondary_authorized']
    secondary_contacts = [c for c in contacts if c.get('relationship') == 'secondary_authorized']
    for c in owner_contacts + secondary_contacts:
        confirmed = [p for p in c['phones'] if p['verification_status'] == 'user_confirmed_person_phone']
        if confirmed and not conflicts and owner_status not in ('licensed_parcel_record_requires_county_check','browser_observed_parcel_requires_review','user_reviewed_source'):
            chosen_contact, chosen_phone = c['full_name'], confirmed[0]['number']
            break
    if not chosen_contact and owner_contacts:
        chosen_contact = owner_contacts[0]['full_name']
    steps = ['Confirm current deed/assessor ownership and parcel address before outreach.',
        'For entities, inspect official state filings; registered agents and attorneys are not presumed owners.',
        'Research the identified owner nationwide using authorized phone tools; corroborate person and address, not area code.',
        'Consider a secondary contact only after owner research is exhausted and repair authority is documented.']
    return dict(normalized_address=f"{row['street']}, {row['city']}, {row['state']} {row.get('zip_code','')}".strip(),
        property_type=row.get('property_type','residential'), owner_of_record=owner,
        owner_status=owner_status,
        owner_contact_status='user_confirmed_phone' if confirmed_owner_phone else 'research_exhausted' if exhausted else 'research_required',
        secondary_candidates=secondary, subject_property=dict(row),
        is_corporate_or_llc=corporate, corporate_entity_name=owner if corporate else '', state_of_formation='',
        parcel_apn=parcels[0].get('apn','') if owner else '', county_and_state=parcels[0].get('county','') if owner else '',
        tax_mailing_address=parcels[0].get('mailing_address','') if owner else '',
        primary_decision_maker=chosen_contact, primary_phone=chosen_phone,
        primary_phone_type='User-confirmed person association — review source' if chosen_phone else 'No verified primary phone',
        decision_maker_role_and_authority=next((c['role'] for c in contacts if c['full_name'] == chosen_contact), 'Human ownership unresolved'),
        decision_maker_verification_proof='Inspect the source records and review method below; parcel currency and ownership require confirmation.' if owner else 'No matched ownership evidence retrieved.',
        ownership_structure_type='Entity; principals require research' if corporate else 'Ownership record review',
        shell_layers_pierced=[], contacts=contacts, principal_candidates=principals, phone_candidates=phones,
        corporate_candidates=[r for r in records if r['kind'] in ('user_hint','search_snippet','corporate_candidate')],
        evidence=records, conflicts=conflicts, overall_confidence='Manual review required' if conflicts else 'Needs verification',
        portfolio_count=0, portfolio_total_value='', portfolio_summary='Portfolio ownership has not been verified.', other_properties=[],
        agent_1_property_findings=f'Owner evidence: {owner_status}. Verify source and currency.' if owner else 'Ownership unresolved. Inspect source status below.',
        agent_2_entity_findings=f'{len(principals)} registry role candidate(s). Beneficial ownership requires explicit source evidence.',
        agent_3_phone_findings=f'{len(phones)} phone candidate(s). ' + ('Primary association confirmed by user research; inspect evidence.' if chosen_phone else 'No confirmed primary phone.'),
        recommended_next_steps=steps)


async def investigate(row, sources=None, advisor=None):
    error = validation_error(row)
    if error:
        raise ValueError(error)
    records = await (sources or PublicSources()).retrieve(row)
    dossier = assemble(row, records)
    dossier['ai'] = dict(status='disabled', hypotheses=[])
    if advisor:
        try:
            # Advice is isolated: no model-produced facts are merged into the dossier.
            dossier['ai'] = await advisor.advise(records)
        except Exception:
            dossier['ai'] = dict(status='failed', hypotheses=[], error='AI advice failed; source evidence is unchanged.')
    return dossier
