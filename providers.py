"""Documented licensed APIs. Disabled until configured with authorized credentials."""
import re
from urllib.parse import quote

import httpx

from research import evidence, public_get, street_key

REGRID = 'https://app.regrid.com/api/v2/parcels/address'
OC = 'https://api.opencorporates.com/v0.4'


def entity_key(name):
    return re.sub(r'[^a-z0-9]', '', name.casefold())


def status(source, state, detail):
    return evidence('retrieval_status', source, status=state, detail=detail)


class LicensedProviders:
    def __init__(self, config):
        self.config = config

    async def parcels(self, row):
        settings = self.config.get('regrid', {})
        if not settings.get('enabled'):
            return []
        if not settings.get('token'):
            return [status('Regrid API', 'not_configured', 'Add an authorized API token in Source settings.')]
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await public_get(client, REGRID,
                    headers={'Authorization': 'Bearer ' + settings['token']},
                    params={'query': f"{row['street']}, {row['city']}, {row['state']} {row.get('zip_code','')}",
                        'path': '/us/' + row['state'].lower(), 'limit': 5, 'return_geometry': 'false',
                        'return_enhanced_ownership': 'false', 'return_matched_buildings': 'false',
                        'return_matched_addresses': 'false', 'return_zoning': 'false'})
                payload = response.json()
                features = payload.get('parcels', {}).get('features', [])
                if len(features) >= 5:
                    return [status('Regrid API', 'manual_review', 'Result limit reached. Narrow the parcel query; uniqueness is not established.')]
                matches = []
                for feature in features:
                    fields = feature.get('properties', {}).get('fields', {})
                    if (street_key(fields.get('address') or '') == street_key(row['street'])
                            and street_key(fields.get('scity') or '') == street_key(row['city'])
                            and str(fields.get('state2') or '').upper() == row['state'].upper()
                            and (not row.get('zip_code') or str(fields.get('szip') or '')[:5] == row['zip_code'][:5])
                            and (not row.get('parcel_apn_hint') or str(fields.get('parcelnumb') or '') == row['parcel_apn_hint'])):
                        matches.append((feature, fields))
                if len(matches) != 1:
                    return [status('Regrid API', 'manual_review', 'No unique exact parcel address/APN match. No owner selected.')]
                feature, fields = matches[0]
                return [evidence('licensed_parcel', 'Regrid API', REGRID,
                    verification_status='licensed_record_requires_county_confirmation', address_match=True,
                    owner=fields.get('owner') or '', apn=fields.get('parcelnumb') or '',
                    county=fields.get('county') or '',
                    mailing_address=', '.join(str(fields.get(k) or '') for k in ('mailadd','mail_city','mail_state2','mail_zip')),
                    record_id=fields.get('ll_uuid') or feature.get('id'), raw_record=fields)]
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            return [status('Regrid API', 'failed', 'API request failed. Check access, plan limits or service availability. Credentials were not logged.')]

    async def companies(self, owner):
        settings = self.config.get('opencorporates', {})
        if not settings.get('enabled') or not owner:
            return []
        if not settings.get('token'):
            return [status('OpenCorporates API', 'not_configured', 'Add an authorized API token in Source settings.')]
        headers = {'X-API-TOKEN': settings['token']}
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                # Nationwide search: property state is not presumed to be formation state.
                response = await public_get(client, OC + '/companies/search', headers=headers,
                    params={'q':owner, 'per_page':5})
                results = response.json()['results']
                companies = [item['company'] for item in results.get('companies', [])]
                records = [evidence('corporate_candidate', 'OpenCorporates API', c.get('opencorporates_url') or OC,
                    entity=c.get('name',''), jurisdiction=c.get('jurisdiction_code',''),
                    company_number=c.get('company_number',''), registry_url=c.get('registry_url',''),
                    verification_status='candidate_requires_entity_match') for c in companies]
                exact = [c for c in companies if entity_key(c.get('name','')) == entity_key(owner)]
                # Ambiguity or truncated search must not choose an arbitrary company.
                if len(exact) != 1 or results.get('total_count', len(companies)) > 5:
                    records.append(status('OpenCorporates API', 'manual_review', 'Confirm the entity and formation jurisdiction in the official registry. No officer selected.'))
                    return records
                company = exact[0]
                jurisdiction, number = company['jurisdiction_code'], company['company_number']
                response = await public_get(client, f'{OC}/companies/{quote(jurisdiction, safe="")}/{quote(number, safe="")}', headers=headers)
                company = response.json()['results']['company']
                for entry in company.get('officers') or []:
                    officer = entry['officer']
                    records.append(evidence('corporate_filing', 'OpenCorporates API (registry-derived)',
                        officer.get('opencorporates_url') or company.get('opencorporates_url') or OC,
                        entity=owner, person=officer.get('name',''), role=officer.get('position',''),
                        end_date=officer.get('end_date'), registry_url=company.get('registry_url',''),
                        jurisdiction=jurisdiction, company_number=number,
                        verification_status='role_reported_requires_official_filing_review'))
                return records
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            return [status('OpenCorporates API', 'failed', 'API request failed. Check authorized access and service limits. No filing checks are claimed.')]
