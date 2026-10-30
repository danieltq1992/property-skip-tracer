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


async def public_get(client, url, **kwargs):
    """Honor throttling without repeatedly sending denied requests."""
    host = httpx.URL(url).host
    async with SOURCE_SLOTS:
        if SOURCE_COOLDOWNS.get(host, 0) > time.monotonic():
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
        response.raise_for_status()
        return response


def now():
    return datetime.now(timezone.utc).isoformat()


def street_key(value):
    # Conservative matching: ambiguous abbreviations require manual review.
    value = re.sub(r'\b(suite|ste|unit|apt)\b', 'unit', value.lower())
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


class PublicSources:
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
    parcels = [r for r in records if r['kind'] == 'official_parcel' and r.get('address_match') and r.get('owner')]
    owners = {r['owner'].casefold() for r in parcels}
    conflicts = []
    if len(owners) > 1:
        conflicts.append('Conflicting official owner records; manual review required.')
    owner = parcels[0]['owner'] if len(owners) == 1 else ''
    web_owners = {r['owner'].casefold() for r in records if r.get('owner') and r['kind'] in ('search_snippet', 'people_search')}
    if len(web_owners | owners) > 1:
        conflicts.append('Owner names differ across sources. Search snippets do not establish legal ownership.')
    corporate = bool(re.search(r'\b(LLC|INC|CORP|LP|LLP|TRUST|PARTNERSHIP|LTD|ASSOCIATION)\b', owner, re.I))
    principals = [dict(r, verification_status='candidate_requires_ownership_review') for r in records
        if r['kind'] == 'corporate_filing' and r.get('entity', '').casefold() == owner.casefold()]
    # Officers and agents are evidence of roles, never proof of beneficial ownership.
    contacts = []
    if owner and not corporate:
        contacts.append(dict(full_name=owner, role='Legal owner in parcel record; currency requires review',
            is_primary_decision_maker=True, verification_evidence='Matched official parcel record',
            mailing_or_home_address=parcels[0].get('mailing_address',''), phones=[], emails=[]))
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
        phones.append(p)
        for contact in contacts:
            if record.get('person', '').casefold() == contact['full_name'].casefold():
                contact['phones'].append(p)
    steps = ['Confirm current deed/assessor ownership and parcel address before outreach.',
        'For entities, inspect official state filings; registered agents and attorneys are not presumed owners.',
        'Research the identified owner nationwide using authorized phone tools; corroborate person and address, not area code.',
        'Consider a secondary contact only after owner research is exhausted and repair authority is documented.']
    return dict(normalized_address=f"{row['street']}, {row['city']}, {row['state']} {row.get('zip_code','')}".strip(),
        property_type=row.get('property_type','residential'), owner_of_record=owner,
        owner_status='official_parcel_record' if owner else 'unresolved',
        is_corporate_or_llc=corporate, corporate_entity_name=owner if corporate else '', state_of_formation='',
        parcel_apn=parcels[0].get('apn','') if owner else '', county_and_state=parcels[0].get('county','') if owner else '',
        tax_mailing_address=parcels[0].get('mailing_address','') if owner else '',
        primary_decision_maker=owner if owner and not corporate else '', primary_phone='', primary_phone_type='No verified primary phone',
        decision_maker_role_and_authority='Legal owner in parcel record' if owner and not corporate else 'Human ownership unresolved',
        decision_maker_verification_proof='Official parcel evidence requires a current deed/assessor check.' if owner else 'No matched official ownership evidence retrieved.',
        ownership_structure_type='Entity; principals require research' if corporate else 'Ownership record review',
        shell_layers_pierced=[], contacts=contacts, principal_candidates=principals, phone_candidates=phones,
        corporate_candidates=[r for r in records if r['kind'] in ('user_hint','search_snippet') and r.get('owner')],
        evidence=records, conflicts=conflicts, overall_confidence='Manual review required' if conflicts else 'Needs verification',
        portfolio_count=0, portfolio_total_value='', portfolio_summary='Portfolio ownership has not been verified.', other_properties=[],
        agent_1_property_findings='Matched official parcel record; verify currency.' if owner else 'Ownership unresolved. Inspect source status below.',
        agent_2_entity_findings='Corporate filings and beneficial ownership require manual authorized research.',
        agent_3_phone_findings=f'{len(phones)} phone candidate(s); no verified primary phone. Manual authorized research required.',
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
