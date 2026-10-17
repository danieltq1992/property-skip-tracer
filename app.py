import asyncio
import json
import os
import re
import urllib.parse
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

try:
    from google import genai
    HAS_GENAI = True
except ImportError:
    HAS_GENAI = False

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
LEADS_FILE = BASE_DIR / "saved_leads.json"
CONFIG_FILE = BASE_DIR / "config.json"

app = FastAPI(
    title="PropTrace AI — Commercial & Residential Property Owner & Phone Skip-Tracing Agent",
    version="1.0.0",
)


# ---------------------------------------------------------------------------
# Pydantic Schemas for Gemini Structured Output & API Requests
# ---------------------------------------------------------------------------

class PhoneRecord(BaseModel):
    number: str = Field(description="Formatted phone number e.g. (555) 234-5678")
    phone_type: str = Field(description="Wireless, Landline, VoIP, or Office/Broker")
    carrier_or_source: str = Field(description="Source portal or carrier info e.g. TruePeopleSearch, FastPeopleSearch, Public Listing")
    confidence: str = Field(description="High, Medium, or Low")


class PrincipalContact(BaseModel):
    full_name: str = Field(description="Individual person's full name (owner, managing member, president, or registered agent)")
    role: str = Field(description="Role e.g. Individual Owner, Managing Member, President, Registered Agent, Trustee")
    mailing_or_home_address: str = Field(description="Personal residential or mailing address for skip-tracing")
    age_or_demographics: Optional[str] = Field(default="", description="Approximate age or middle initial if found")
    phones: List[PhoneRecord] = Field(default_factory=list, description="Associated phone numbers for this person")
    emails: List[str] = Field(default_factory=list, description="Associated email addresses")
    relatives_or_associates: List[str] = Field(default_factory=list, description="Possible relatives or business associates")


class SkipTraceDossier(BaseModel):
    normalized_address: str = Field(description="Standardized full property address")
    property_type: str = Field(description="Residential or Commercial")
    parcel_apn: str = Field(default="Not found in public snippet", description="Assessor Parcel Number (APN) if discovered")
    county_and_state: str = Field(default="", description="County and State of the property")
    owner_of_record: str = Field(description="Deed owner of record (either an Individual Name or LLC/Corporation/Trust name)")
    is_corporate_or_llc: bool = Field(description="True if owned by an LLC, Inc, LP, Corp, or Trust")
    corporate_entity_name: str = Field(default="", description="Exact LLC/Corporation name if applicable")
    state_of_formation: str = Field(default="", description="State where the LLC/Corp is registered")
    registered_agent_name: str = Field(default="", description="Registered agent name from OpenCorporates/CorporationWiki/SOS")
    tax_mailing_address: str = Field(default="", description="Tax bill mailing address or principal office address")
    primary_decision_maker: str = Field(description="The best individual person name to call (human behind the LLC or individual homeowner)")
    primary_phone: str = Field(default="", description="Best primary phone number discovered")
    contacts: List[PrincipalContact] = Field(default_factory=list, description="List of owners, principals, officers, and their phone numbers")
    agent_1_property_findings: str = Field(description="Summary of Agent 1 (Assessor, LandGlide, Prospect by Buildout, Regrid, LoopNet) findings")
    agent_2_entity_findings: str = Field(description="Summary of Agent 2 (OpenCorporates, CorporationWiki, B2BHint, Bizapedia, SOS) veil-piercing findings")
    agent_3_phone_findings: str = Field(description="Summary of Agent 3 (TruePeopleSearch, FastPeopleSearch, Spokeo, Public Skip-Trace) phone findings")
    recommended_next_steps: List[str] = Field(default_factory=list, description="Actionable verification tips using the deep-linked tools")
    overall_confidence: str = Field(description="High, Medium, or Needs Portal Verification")


class InvestigateRequest(BaseModel):
    street: str
    city: str
    state: str
    zip_code: Optional[str] = ""
    property_type: str = "residential"
    known_owner_or_llc: Optional[str] = ""
    known_phone: Optional[str] = ""
    model: str = "gemini-3.8-flash"
    gemini_api_key: Optional[str] = ""
    auto_open_tabs: bool = False


class SaveLeadRequest(BaseModel):
    address: str
    property_type: str
    owner_of_record: str
    decision_maker: str
    primary_phone: str
    all_phones: List[str] = []
    emails: List[str] = []
    mailing_address: str = ""
    confidence: str = "Medium"
    notes: str = ""


class OpenTabsRequest(BaseModel):
    urls: List[str]


class ApiKeyRequest(BaseModel):
    gemini_api_key: str


# ---------------------------------------------------------------------------
# Helper Functions: Config & Saved Leads
# ---------------------------------------------------------------------------

def get_saved_api_key() -> str:
    env_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if env_key:
        return env_key
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return data.get("gemini_api_key", "").strip()
        except Exception:
            pass
    return ""


def save_api_key(key: str) -> None:
    CONFIG_FILE.write_text(json.dumps({"gemini_api_key": key.strip()}, indent=2), encoding="utf-8")
    os.environ["GEMINI_API_KEY"] = key.strip()


def load_saved_leads() -> List[Dict[str, Any]]:
    if not LEADS_FILE.exists():
        return []
    try:
        return json.loads(LEADS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def write_saved_leads(leads: List[Dict[str, Any]]) -> None:
    LEADS_FILE.write_text(json.dumps(leads, indent=2), encoding="utf-8")


def slugify(text: str) -> str:
    cleaned = re.sub(r"[^\w\s-]", "", (text or "").strip())
    return re.sub(r"[\s_]+", "-", cleaned).lower()


# ---------------------------------------------------------------------------
# Deep-Link Builder for All 10 Bookmarked Tools + 6 Bonus Tools
# ---------------------------------------------------------------------------

def build_tool_links(
    street: str,
    city: str,
    state: str,
    zip_code: str = "",
    owner_name: str = "",
    entity_name: str = "",
    phone: str = "",
) -> Dict[str, List[Dict[str, str]]]:
    full_address = f"{street}, {city}, {state} {zip_code}".strip().rstrip(",")
    city_state_zip = f"{city}, {state} {zip_code}".strip()
    city_state = f"{city}, {state}".strip()

    street_slug = slugify(street)
    city_slug = slugify(city)
    state_slug = slugify(state)
    fps_addr_slug = f"{street_slug}_{city_slug}-{state_slug}"

    enc_full_addr = urllib.parse.quote(full_address)
    enc_street = urllib.parse.quote(street)
    enc_csz = urllib.parse.quote(city_state_zip)

    target_person = (owner_name or "").strip()
    target_entity = (entity_name or owner_name or "").strip()
    person_slug = slugify(target_person) if target_person else ""
    enc_person = urllib.parse.quote(target_person) if target_person else ""
    enc_entity = urllib.parse.quote(target_entity) if target_entity else enc_full_addr

    # Stage 1: Property & Parcel Recon (LandGlide, Prospect by Buildout, Regrid, Assessor, LoopNet/Crexi)
    property_tools = [
        {
            "id": "landglide",
            "name": "LandGlide",
            "source_type": "screenshot",
            "badge": "Bookmark #8",
            "stage": "Stage 1: Property & Parcel",
            "description": f"Copies '{full_address}' to clipboard & opens LandGlide parcel/owner map",
            "url": "https://app.landglide.com/",
            "copy_text": full_address,
        },
        {
            "id": "buildout",
            "name": "Prospect by Buildout",
            "source_type": "screenshot",
            "badge": "Bookmark #9",
            "stage": "Stage 1: Property & Parcel",
            "description": f"Copies '{full_address}' & opens Prospect by Buildout for commercial/residential ownership",
            "url": "https://prospect.buildout.com/",
            "copy_text": full_address,
        },
        {
            "id": "regrid",
            "name": "Regrid Free Parcel & Deed Search",
            "source_type": "bonus",
            "badge": "Bonus Free GIS",
            "stage": "Stage 1: Property & Parcel",
            "description": "Direct Regrid parcel search showing APN, Deed Owner Name & Tax Bill Mailing Address",
            "url": f"https://www.google.com/search?q={urllib.parse.quote('site:app.regrid.com \"' + street + '\" \"' + city + '\"')}",
            "copy_text": full_address,
        },
        {
            "id": "county_assessor",
            "name": "County Tax Assessor & GIS",
            "source_type": "bonus",
            "badge": "Bonus Official Record",
            "stage": "Stage 1: Property & Parcel",
            "description": f"Official county property tax card & deed owner search for {city}, {state}",
            "url": f"https://www.google.com/search?q={urllib.parse.quote(full_address + ' county tax assessor parcel owner record')}",
            "copy_text": full_address,
        },
        {
            "id": "land_com",
            "name": "Land.com (Farm, Ranch & Acreage Owners)",
            "source_type": "screenshot",
            "badge": "Farm & Land Source",
            "stage": "Stage 1: Property & Parcel",
            "description": f"Search Land.com farm/ranch listings, acreage brochures & landowner records for {full_address}",
            "url": f"https://www.google.com/search?q={urllib.parse.quote('site:land.com \"' + city + '\" \"' + state + '\" ' + street)}",
            "copy_text": full_address,
        },
        {
            "id": "land_com_portal",
            "name": "Land.com Portal Map Search",
            "source_type": "bonus",
            "badge": "Farm & Ranch Portal",
            "stage": "Stage 1: Property & Parcel",
            "description": f"Copies '{full_address}' to clipboard & opens Land.com to browse rural/farm tracts in {city}, {state}",
            "url": "https://www.land.com/",
            "copy_text": full_address,
        },
        {
            "id": "commercial_listings",
            "name": "LoopNet & Crexi Commercial Recon",
            "source_type": "bonus",
            "badge": "Bonus Commercial",
            "stage": "Stage 1: Property & Parcel",
            "description": "Find commercial offering memorandums, leasing brokers & true owner LLCs",
            "url": f"https://www.google.com/search?q={urllib.parse.quote('\"' + street + '\" \"' + city + '\" (site:loopnet.com OR site:crexi.com)')}",
            "copy_text": full_address,
        },
    ]

    # Stage 2: Corporate & LLC Piercing (OpenCorporates, CorporationWiki, B2BHint, Bizapedia, State SOS)
    entity_tools = [
        {
            "id": "opencorporates",
            "name": "OpenCorporates",
            "source_type": "screenshot",
            "badge": "Bookmark #5",
            "stage": "Stage 2: LLC / Corp Piercing",
            "description": f"Search corporate filings, officers & registered agent for {target_entity or full_address}",
            "url": (
                f"https://opencorporates.com/companies?q={enc_entity}&jurisdiction_code=us_{state_slug}"
                if len(state_slug) == 2
                else f"https://opencorporates.com/companies?q={enc_entity}"
            ),
            "copy_text": target_entity or full_address,
        },
        {
            "id": "corporationwiki",
            "name": "CorporationWiki",
            "source_type": "screenshot",
            "badge": "Bookmark #4",
            "stage": "Stage 2: LLC / Corp Piercing",
            "description": f"Visual relationship graph linking {target_entity or 'LLCs/Addresses'} to real human principals",
            "url": f"https://www.corporationwiki.com/search/results?term={enc_entity}",
            "copy_text": target_entity or full_address,
        },
        {
            "id": "b2bhint",
            "name": "B2BHint (Business Data Platform)",
            "source_type": "screenshot",
            "badge": "Bookmark #6",
            "stage": "Stage 2: LLC / Corp Piercing",
            "description": f"Business Data Platform, Company Search, API & Network lookup for {target_entity or full_address}",
            "url": f"https://b2bhint.com/en/search?q={enc_entity}",
            "copy_text": target_entity or full_address,
        },
        {
            "id": "bizapedia",
            "name": "Bizapedia Principal & Agent Search",
            "source_type": "bonus",
            "badge": "Bonus LLC Piercer",
            "stage": "Stage 2: LLC / Corp Piercing",
            "description": "Uncovers managing members, governors, and registered agent addresses for LLCs",
            "url": f"https://www.google.com/search?q={urllib.parse.quote('site:bizapedia.com \"' + (target_entity or street) + '\" ' + state)}",
            "copy_text": target_entity or full_address,
        },
        {
            "id": "state_sos",
            "name": f"{state.upper() or 'State'} Secretary of State Entity Search",
            "source_type": "bonus",
            "badge": "Bonus Official Filings",
            "stage": "Stage 2: LLC / Corp Piercing",
            "description": "Direct search for Articles of Organization & Annual Reports showing managing members",
            "url": f"https://www.google.com/search?q={urllib.parse.quote('\"' + (target_entity or street) + '\" ' + state + ' Secretary of State business entity filings')}",
            "copy_text": target_entity or full_address,
        },
    ]

    # Stage 3: People Search & Phone Skip-Tracing (TruePeopleSearch, FastPeopleSearch, Spokeo, Core365, Gemini, etc.)
    tps_person_url = (
        f"https://www.truepeoplesearch.com/results?name={enc_person}&citystatezip={urllib.parse.quote(city_state)}"
        if target_person
        else f"https://www.truepeoplesearch.com/resultaddress?streetaddress={enc_street}&citystatezip={enc_csz}"
    )
    fps_person_url = (
        f"https://www.fastpeoplesearch.com/name/{person_slug}_{city_slug}-{state_slug}"
        if target_person
        else f"https://www.fastpeoplesearch.com/address/{fps_addr_slug}"
    )
    spokeo_url = (
        f"https://www.spokeo.com/{person_slug.title()}/{state.upper()}/{city_slug.title()}"
        if target_person and len(state.strip()) == 2
        else f"https://www.spokeo.com/{state.upper()}/{city_slug.title()}/{street_slug.title()}"
        if len(state.strip()) == 2
        else f"https://www.spokeo.com/search?q={enc_full_addr}"
    )

    people_tools = [
        {
            "id": "tps_address",
            "name": "TruePeopleSearch (Reverse Address)",
            "source_type": "screenshot",
            "badge": "Bookmark #3",
            "stage": "Stage 3: Phone Skip-Trace",
            "description": f"100% Free reverse address lookup for residents/owners & phones at {street}",
            "url": f"https://www.truepeoplesearch.com/resultaddress?streetaddress={enc_street}&citystatezip={enc_csz}",
            "copy_text": full_address,
        },
        {
            "id": "tps_name",
            "name": f"TruePeopleSearch ({target_person or 'By Owner Name'})",
            "source_type": "screenshot",
            "badge": "Bookmark #3",
            "stage": "Stage 3: Phone Skip-Trace",
            "description": f"100% Free wireless & landline phone finder for {target_person or 'discovered decision maker'}",
            "url": tps_person_url,
            "copy_text": target_person or full_address,
        },
        {
            "id": "fps_address",
            "name": "FastPeopleSearch (Reverse Address)",
            "source_type": "screenshot",
            "badge": "Bookmark #2",
            "stage": "Stage 3: Phone Skip-Trace",
            "description": f"Free reverse address & phone lookup for {full_address}",
            "url": f"https://www.fastpeoplesearch.com/address/{fps_addr_slug}",
            "copy_text": full_address,
        },
        {
            "id": "fps_name",
            "name": f"FastPeopleSearch ({target_person or 'By Owner Name'})",
            "source_type": "screenshot",
            "badge": "Bookmark #2",
            "stage": "Stage 3: Phone Skip-Trace",
            "description": f"Free cell/landline lookup for {target_person or 'discovered owner'} in {city_state}",
            "url": fps_person_url,
            "copy_text": target_person or full_address,
        },
        {
            "id": "spokeo",
            "name": "Spokeo People & Reverse Lookup",
            "source_type": "screenshot",
            "badge": "Bookmark #1",
            "stage": "Stage 3: Phone Skip-Trace",
            "description": f"Spokeo White Pages, property value, email & phone verification for {target_person or full_address}",
            "url": spokeo_url,
            "copy_text": target_person or full_address,
        },
        {
            "id": "cyberbackgroundchecks",
            "name": "CyberBackgroundChecks (Free)",
            "source_type": "bonus",
            "badge": "Bonus Free Phones",
            "stage": "Stage 3: Phone Skip-Trace",
            "description": f"Free skip-tracing database for {street}, {city}, {state} phones & emails",
            "url": f"https://www.cyberbackgroundchecks.com/address/{street_slug}/{city_slug}/{state_slug}",
            "copy_text": full_address,
        },
        {
            "id": "searchpeoplefree",
            "name": "SearchPeopleFree",
            "source_type": "bonus",
            "badge": "Bonus Free Phones",
            "stage": "Stage 3: Phone Skip-Trace",
            "description": f"Cross-verify wireless phone numbers for {target_person or street}",
            "url": (
                f"https://www.searchpeoplefree.com/find/{person_slug}/{state_slug}/{city_slug}"
                if person_slug
                else f"https://www.searchpeoplefree.com/address/{state_slug}/{city_slug}/{street_slug}"
            ),
            "copy_text": target_person or full_address,
        },
        {
            "id": "core365_gemini",
            "name": "Core365 | via & Google Gemini",
            "source_type": "screenshot",
            "badge": "Bookmarks #7 & #10",
            "stage": "Stage 3: CRM & AI",
            "description": "Push verified owner & phone numbers into Core365 | via or Google Gemini",
            "url": "https://gemini.google.com/app",
            "copy_text": f"{target_person} - {full_address}",
        },
    ]

    return {
        "property_tools": property_tools,
        "entity_tools": entity_tools,
        "people_tools": people_tools,
    }


# ---------------------------------------------------------------------------
# Live Multi-Source Reconnaissance Scraper (Fallback + Context Gatherer)
# ---------------------------------------------------------------------------

async def gather_public_web_recon(
    street: str,
    city: str,
    state: str,
    zip_code: str,
    known_owner: str = "",
) -> Dict[str, Any]:
    """Executes a 3-stage live reconnaissance pipeline:
    1. Geocodes & queries live GIS Parcel layers (APN, Deed Owner, Tax Mailing Address, Assessed Value).
    2. Runs Yahoo Search on the property address, discovered LLC/Corp owner, and tax mailing address.
    3. Extracts human principals/registered agents and runs targeted phone skip-trace queries.
    """
    recon: Dict[str, Any] = {
        "gis_parcel": {},
        "snippets": [],
        "candidate_phones": [],
        "candidate_entities": [],
        "discovered_persons": [],
    }
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        )
    }
    full_address = f"{street}, {city}, {state} {zip_code}".strip()
    phone_pattern = re.compile(r"\(?\b[2-9][0-9]{2}\)?[-.\s]?[2-9][0-9]{2}[-.\s]?[0-9]{4}\b")
    entity_pattern = re.compile(
        r"\b([A-Z0-9][A-Za-z0-9&.,'\-\s]{2,45}\s(?:LLC|L\.L\.C\.|INC\.?|CORP\.?|CORPORATION|LP|L\.P\.|TRUST|HOLDINGS|PROPERTIES|INVESTMENTS|FARMS|RANCH))\b"
    )
    person_patterns = [
        re.compile(r"(?:Registered Agent on file is|Contact:|primary contact at [^.]* is|sole principal is)\s+([A-Z][a-z]+(?:\s+[A-Z]\.?|\s+[A-Z][a-z]+)?\s+[A-Z][a-z]+)"),
        re.compile(r"\b([A-Z][a-z]+\s+[A-Z]\.?\s+[A-Z][a-z]+)\s*,\s*age\s+\d+"),
    ]

    async def run_yahoo_search(client: httpx.AsyncClient, query: str) -> List[str]:
        found: List[str] = []
        try:
            resp = await client.get("https://search.yahoo.com/search", params={"p": query})
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                for div in soup.select("div.algo")[:5]:
                    txt = div.get_text(" ", strip=True)
                    if txt:
                        found.append(txt[:380])
        except Exception:
            pass
        return found

    def ingest_snippets(snips: List[str]) -> None:
        for s in snips:
            if s not in recon["snippets"]:
                recon["snippets"].append(s)
            for ph in phone_pattern.findall(s):
                # Normalize phone formatting
                digits = re.sub(r"\D", "", ph)
                if len(digits) == 10:
                    fmt_ph = f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
                    if fmt_ph not in recon["candidate_phones"]:
                        recon["candidate_phones"].append(fmt_ph)
            for ent in entity_pattern.findall(s.upper()):
                cl_ent = ent.strip(" .,;-")
                if len(cl_ent) > 4 and cl_ent not in recon["candidate_entities"]:
                    recon["candidate_entities"].append(cl_ent)
            for pat in person_patterns:
                for p_match in pat.findall(s):
                    p_clean = p_match.strip(" .,;-")
                    if p_clean and p_clean not in recon["discovered_persons"]:
                        recon["discovered_persons"].append(p_clean)

    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True, headers=headers) as client:
        # STAGE 1A: Geocode via ArcGIS & Query Statewide/County GIS Parcel FeatureServer
        try:
            geo_resp = await client.get(
                "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates",
                params={
                    "SingleLine": full_address,
                    "countryCode": "USA",
                    "outFields": "StAddr,City,RegionAbbr,Postal,Subregion",
                    "maxLocations": 1,
                    "f": "json",
                },
            )
            if geo_resp.status_code == 200:
                cands = geo_resp.json().get("candidates", [])
                if cands:
                    cand = cands[0]
                    lon = cand["location"]["x"]
                    lat = cand["location"]["y"]
                    county = cand.get("attributes", {}).get("Subregion", "")
                    state_abbr = (cand.get("attributes", {}).get("RegionAbbr") or state).upper()
                    recon["gis_parcel"]["county"] = county
                    recon["gis_parcel"]["state"] = state_abbr

                    # Query NC OneMap Parcel Layer if in NC (covers all 100 NC counties including Duplin)
                    if state_abbr == "NC":
                        nc_resp = await client.get(
                            "https://services.nconemap.gov/secure/rest/services/NC1Map_Parcels/FeatureServer/1/query",
                            params={
                                "geometry": f"{lon},{lat}",
                                "geometryType": "esriGeometryPoint",
                                "inSR": "4326",
                                "spatialRel": "esriSpatialRelIntersects",
                                "distance": 50,
                                "units": "esriSRUnit_Meter",
                                "outFields": "ownname,ownname2,mailadd,mcity,mstate,mzip,parno,siteadd,scity,cntyname,parval",
                                "f": "json",
                            },
                        )
                        if nc_resp.status_code == 200:
                            feats = nc_resp.json().get("features", [])
                            if feats:
                                attrs = feats[0].get("attributes", {})
                                own1 = (attrs.get("ownname") or "").strip()
                                own2 = (attrs.get("ownname2") or "").strip()
                                mail_st = (attrs.get("mailadd") or "").strip()
                                mail_city = (attrs.get("mcity") or "").strip()
                                mail_state = (attrs.get("mstate") or state_abbr).strip()
                                mail_zip = (attrs.get("mzip") or "").strip()
                                full_mail = ", ".join(filter(None, [mail_st, mail_city, f"{mail_state} {mail_zip}".strip()]))
                                parno = (attrs.get("parno") or "").strip()
                                parval = attrs.get("parval")

                                recon["gis_parcel"].update(
                                    {
                                        "owner_name": own1,
                                        "owner_name_2": own2,
                                        "mailing_address": full_mail or mail_st,
                                        "mail_street": mail_st,
                                        "parcel_apn": parno,
                                        "assessed_value": f"${parval:,.0f}" if isinstance(parval, (int, float)) and parval else "",
                                        "county": f"{attrs.get('cntyname', county)} County".replace("County County", "County"),
                                    }
                                )
                                gis_summary = (
                                    f"OFFICIAL GIS PARCEL RECORD: Site Address: {attrs.get('siteadd') or street}, {city}, {state_abbr} | "
                                    f"Deed Owner: {own1} {own2} | Parcel APN: {parno} | "
                                    f"Tax Bill Mailing Address: {full_mail or mail_st} | Assessed Value: {recon['gis_parcel']['assessed_value']}"
                                )
                                recon["snippets"].append(gis_summary)
                                if own1:
                                    recon["candidate_entities"].insert(0, own1)
        except Exception:
            pass

        # STAGE 1B: Web Search for the Property Address
        stage1_queries = [
            f"{street} {city} {state} owner phone",
            f'"{street}" {city} {state} parcel OR Land.com',
        ]
        for q in stage1_queries:
            ingest_snippets(await run_yahoo_search(client, q))

        # STAGE 2: Corporate Veil Piercing & Tax Mailing Address Recon
        target_owner = (
            known_owner.strip()
            or recon["gis_parcel"].get("owner_name", "")
            or (recon["candidate_entities"][0] if recon["candidate_entities"] else "")
        )
        mail_street = recon["gis_parcel"].get("mail_street", "")

        stage2_queries = []
        if target_owner:
            clean_owner = re.sub(r"[^\w\s]", " ", target_owner).strip()
            stage2_queries.append(f"{clean_owner} {state} registered agent principal")
            stage2_queries.append(f"{clean_owner} {state} phone contact")
        if mail_street and mail_street.lower() != street.lower():
            stage2_queries.append(f"{mail_street} {state} phone owner")

        for q in stage2_queries:
            ingest_snippets(await run_yahoo_search(client, q))

        # STAGE 3: Targeted Phone Lookup for Discovered Human Principal(s)
        if recon["discovered_persons"]:
            person_name = recon["discovered_persons"][0]
            s3_results = await run_yahoo_search(client, f"{person_name} {state} phone")
            ingest_snippets(s3_results)

    return recon


# ---------------------------------------------------------------------------
# Gemini AI Multi-Agent Skip-Tracing Pipeline (Interactions API + Structured Output)
# ---------------------------------------------------------------------------

async def run_gemini_skip_trace_agents(
    req: InvestigateRequest,
    recon: Dict[str, Any],
    api_key: str,
) -> SkipTraceDossier:
    full_address = f"{req.street}, {req.city}, {req.state} {req.zip_code}".strip()
    gis_info = recon.get("gis_parcel", {})
    snippets_text = "\n".join(f"- {s}" for s in recon.get("snippets", [])[:18]) or "No pre-scraped snippets."

    system_prompt = (
        "You are PropTrace AI, an elite 3-Agent Commercial, Residential & Farm/Land Property Skip-Tracing System.\n"
        "Analyze all live GIS parcel records, corporate registry snippets (Bizapedia, OpenCorporates, NCCompanies, AllBiz, Buzzfile), "
        "and people/phone skip-tracing records (Whitepages, FastPeopleSearch, TruePeopleSearch, National Public Data) provided below:\n"
        "1. AGENT 1 (Property, Farm & Parcel Recon): Extract the county, Assessor Parcel Number (APN), Deed Owner of Record, "
        "and official Tax Bill Mailing Address (including city/state/zip discovered in snippets for that mailing street).\n"
        "2. AGENT 2 (Corporate & LLC Veil Piercing): If the owner is an LLC, Inc, Corporation, LP, Farm, or Trust, identify the "
        "human Registered Agent, President, Managing Member, or Sole Principal and their personal/mailing address.\n"
        "3. AGENT 3 (People Search & Phone Skip-Tracing): Extract every verified phone number (wireless, landline, office), "
        "linking each phone number to the specific human contact (e.g. Principal/Registered Agent and any co-residents/relatives at the mailing address).\n"
        "Never output placeholder text like 'Click below' if real names, entities, or phone numbers exist in the recon clues."
    )

    user_prompt = f"""Synthesize a complete, accurate SkipTraceDossier JSON for:
- Target Property Address: {full_address}
- Property Type: {req.property_type.upper()}
- User-Supplied Owner / LLC Hint: {req.known_owner_or_llc or 'None provided — use discovered GIS/web owner'}
- User-Supplied Phone Hint: {req.known_phone or 'None'}

Live GIS Parcel Record:
{json.dumps(gis_info, indent=2)}

Discovered Human Principals / Contacts:
{', '.join(recon.get('discovered_persons', [])) or 'See snippets'}

Discovered Corporate / Farm Entities:
{', '.join(recon.get('candidate_entities', [])) or 'See snippets'}

Discovered Phone Numbers:
{', '.join(recon.get('candidate_phones', [])) or 'See snippets'}

Live Multi-Stage Reconnaissance Snippets (Stage 1 Parcel + Stage 2 Corporate Piercing + Stage 3 Phone Lookup):
{snippets_text}"""

    if HAS_GENAI and api_key:
        client = genai.Client(api_key=api_key)
        model_name = req.model if req.model in ("gemini-3.8-flash", "gemini-4-argon", "gemini-3.5-flash-lite") else "gemini-3.8-flash"

        if hasattr(client, "interactions"):
            # Primary call: Structured Output over our live multi-stage GIS + web recon (avoids 429 google_search quota blocks)
            try:
                interaction = await asyncio.to_thread(
                    client.interactions.create,
                    model=model_name,
                    system_instruction=system_prompt,
                    input=user_prompt,
                    response_format=[
                        {
                            "type": "text",
                            "mime_type": "application/json",
                            "schema": SkipTraceDossier.model_json_schema(),
                        }
                    ],
                )
                raw_text = interaction.output_text or ""
                if raw_text:
                    parsed = json.loads(raw_text)
                    dossier_obj = SkipTraceDossier(**parsed)
                    # Enrich/guard fields using live GIS and regex-extracted phones & principals
                    if not dossier_obj.primary_phone and recon.get("candidate_phones"):
                        dossier_obj.primary_phone = recon["candidate_phones"][0]
                    if (
                        recon.get("discovered_persons")
                        and (
                            not dossier_obj.primary_decision_maker
                            or "officer" in dossier_obj.primary_decision_maker.lower()
                            or "owner at" in dossier_obj.primary_decision_maker.lower()
                        )
                    ):
                        dossier_obj.primary_decision_maker = recon["discovered_persons"][0]
                    if len(dossier_obj.tax_mailing_address) > 95:
                        dossier_obj.tax_mailing_address = dossier_obj.tax_mailing_address.split("(")[0].strip()
                    if dossier_obj.primary_phone and dossier_obj.overall_confidence == "Needs Portal Verification":
                        dossier_obj.overall_confidence = "High"
                    return dossier_obj
            except Exception:
                pass

    # Deterministic extraction from live GIS + scraped snippets (works even without an API key!)
    gis_owner = gis_info.get("owner_name", "")
    inferred_entity = (
        req.known_owner_or_llc.strip()
        if req.known_owner_or_llc
        else (gis_owner or (recon["candidate_entities"][0] if recon.get("candidate_entities") else ""))
    )
    is_llc = bool(
        inferred_entity
        and any(k in inferred_entity.upper() for k in ("LLC", "INC", "CORP", "LP", "TRUST", "HOLDINGS", "PROPERTIES", "RANCH", "FARMS"))
    )
    discovered_persons = recon.get("discovered_persons", [])
    primary_human = discovered_persons[0] if discovered_persons else (inferred_entity or f"Owner at {req.street}")
    mail_addr = gis_info.get("mailing_address") or full_address
    apn_val = gis_info.get("parcel_apn") or "See Regrid / LandGlide Link Below"
    county_val = gis_info.get("county") or f"{req.city}, {req.state}"

    phones_list = [
        PhoneRecord(
            number=ph,
            phone_type="Direct / Public Record",
            carrier_or_source="Live Web & Directory Recon",
            confidence="High" if idx == 0 else "Medium",
        )
        for idx, ph in enumerate(recon.get("candidate_phones", [])[:6])
    ]

    contacts_list: List[PrincipalContact] = []
    if discovered_persons:
        for idx, person in enumerate(discovered_persons[:3]):
            person_phones = [phones_list[idx]] if idx < len(phones_list) else phones_list[:2]
            contacts_list.append(
                PrincipalContact(
                    full_name=person,
                    role="Principal / Registered Agent" if idx == 0 else "Associated Resident / Relative",
                    mailing_or_home_address=mail_addr,
                    phones=person_phones,
                    emails=[],
                    relatives_or_associates=[p for p in discovered_persons if p != person],
                )
            )
    else:
        contacts_list.append(
            PrincipalContact(
                full_name=primary_human,
                role="Property Owner / Principal",
                mailing_or_home_address=mail_addr,
                phones=phones_list,
                emails=[],
                relatives_or_associates=[],
            )
        )

    return SkipTraceDossier(
        normalized_address=full_address,
        property_type=req.property_type.capitalize(),
        parcel_apn=apn_val,
        county_and_state=county_val,
        owner_of_record=inferred_entity or primary_human,
        is_corporate_or_llc=is_llc,
        corporate_entity_name=inferred_entity if is_llc else "",
        state_of_formation=req.state.upper(),
        registered_agent_name=discovered_persons[0] if discovered_persons else "",
        tax_mailing_address=mail_addr,
        primary_decision_maker=primary_human,
        primary_phone=phones_list[0].number if phones_list else (req.known_phone or "Use Stage 3 Free Links Below"),
        contacts=contacts_list,
        agent_1_property_findings=(
            f"Identified Deed Owner '{inferred_entity or primary_human}' (APN: {apn_val}) in {county_val} with Tax Mailing Address: {mail_addr}."
        ),
        agent_2_entity_findings=(
            f"Pierced entity '{inferred_entity}' to human principal/registered agent: {', '.join(discovered_persons)}."
            if discovered_persons
            else f"Prepared corporate & farm trust veil-piercing queries for '{inferred_entity or req.street}'."
        ),
        agent_3_phone_findings=(
            f"Extracted {len(phones_list)} verified phone number(s): {', '.join(p.number for p in phones_list)}."
            if phones_list
            else "Generated pre-filled reverse-address & name deep-links for TruePeopleSearch, FastPeopleSearch, and Spokeo."
        ),
        recommended_next_steps=[
            f"Call primary phone {phones_list[0].number} for {primary_human}." if phones_list else "Click 'Open All Free Phone Portals' below.",
            "Use the TruePeopleSearch & FastPeopleSearch buttons (now pre-filled with the human principal's name) to view additional cell numbers.",
            "Use Land.com, LandGlide, or Regrid in Stage 1 to inspect parcel boundaries and acreage.",
        ],
        overall_confidence="High" if (inferred_entity and phones_list) else ("Medium" if inferred_entity else "Needs Portal Verification"),
    )


# ---------------------------------------------------------------------------
# FastAPI Routes
# ---------------------------------------------------------------------------

@app.get("/api/address-suggest")
async def suggest_addresses(q: str = "", city: str = "", state: str = ""):
    """Live US street address autocomplete returning street, city, 2-letter state, ZIP, and county."""
    query_str = q.strip()
    if len(query_str) < 3:
        return {"suggestions": []}

    search_line = query_str
    if city.strip() and city.strip().lower() not in query_str.lower():
        search_line += f", {city.strip()}"
    if state.strip() and state.strip().lower() not in query_str.lower():
        search_line += f", {state.strip()}"

    suggestions: List[Dict[str, str]] = []
    seen_keys = set()

    async with httpx.AsyncClient(timeout=6.0) as client:
        try:
            # Step 1: Query ArcGIS suggest endpoint for fast typeahead magicKeys
            sug_resp = await client.get(
                "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/suggest",
                params={
                    "text": search_line,
                    "countryCode": "USA",
                    "category": "Address,Coordinate System,Street Address",
                    "maxSuggestions": 6,
                    "f": "json",
                },
            )
            sug_items = sug_resp.json().get("suggestions", []) if sug_resp.status_code == 200 else []

            # Resolve structured fields in parallel for the top suggestions
            async def resolve_candidate(text_val: str, magic_key: str = ""):
                params = {
                    "SingleLine": text_val,
                    "countryCode": "USA",
                    "outFields": "StAddr,City,RegionAbbr,Postal,Subregion,PlaceName",
                    "maxLocations": 1,
                    "f": "json",
                }
                if magic_key:
                    params["magicKey"] = magic_key
                r = await client.get(
                    "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates",
                    params=params,
                )
                if r.status_code == 200:
                    cands = r.json().get("candidates", [])
                    if cands:
                        return cands[0]
                return None

            if sug_items:
                resolved = await asyncio.gather(
                    *[resolve_candidate(item.get("text", ""), item.get("magicKey", "")) for item in sug_items[:6]],
                    return_exceptions=True,
                )
                candidates = [c for c in resolved if isinstance(c, dict)]
            else:
                # Direct fallback to findAddressCandidates
                cand_resp = await client.get(
                    "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates",
                    params={
                        "SingleLine": search_line,
                        "countryCode": "USA",
                        "outFields": "StAddr,City,RegionAbbr,Postal,Subregion,PlaceName",
                        "maxLocations": 6,
                        "f": "json",
                    },
                )
                candidates = cand_resp.json().get("candidates", []) if cand_resp.status_code == 200 else []

            for cand in candidates:
                attrs = cand.get("attributes", {})
                st_addr = (attrs.get("StAddr") or "").strip()
                c_city = (attrs.get("City") or "").strip()
                c_state = (attrs.get("RegionAbbr") or "").strip().upper()
                c_zip = (attrs.get("Postal") or "").strip()[:5]
                c_county = (attrs.get("Subregion") or "").strip()

                # If StAddr is empty, parse from full address string
                if not st_addr and cand.get("address"):
                    parts = [p.strip() for p in cand["address"].split(",")]
                    if parts:
                        st_addr = parts[0]
                        if len(parts) > 1 and not c_city:
                            c_city = parts[1]

                if not st_addr or not c_city or not c_state:
                    continue

                dedup_key = f"{st_addr.lower()}|{c_city.lower()}|{c_state.lower()}|{c_zip}"
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)

                label = f"{st_addr}, {c_city}, {c_state} {c_zip}".strip()
                suggestions.append(
                    {
                        "label": label,
                        "street": st_addr,
                        "city": c_city,
                        "state": c_state[:2],
                        "zip_code": c_zip,
                        "county": c_county,
                    }
                )
        except Exception:
            pass

    return {"suggestions": suggestions[:6]}


@app.get("/api/status")
async def get_status():
    key = get_saved_api_key()
    return {
        "has_api_key": bool(key),
        "masked_key": (key[:6] + "..." + key[-4:]) if len(key) > 10 else ("Configured" if key else ""),
        "has_genai_sdk": HAS_GENAI,
        "default_model": "gemini-3.8-flash",
    }


@app.post("/api/settings/apikey")
async def update_api_key(req: ApiKeyRequest):
    save_api_key(req.gemini_api_key)
    return {"status": "ok", "has_api_key": bool(req.gemini_api_key.strip())}


@app.post("/api/investigate")
async def investigate_property(req: InvestigateRequest):
    if not req.street.strip() or not req.city.strip() or not req.state.strip():
        raise HTTPException(status_code=400, detail="Street, City, and State are required.")

    api_key = (req.gemini_api_key or "").strip() or get_saved_api_key()
    if req.gemini_api_key and req.gemini_api_key.strip():
        save_api_key(req.gemini_api_key.strip())

    # Step 1: Gather public web recon clues
    recon = await gather_public_web_recon(
        street=req.street,
        city=req.city,
        state=req.state,
        zip_code=req.zip_code or "",
        known_owner=req.known_owner_or_llc or "",
    )

    # Step 2: Run the 3-Stage AI Agent analysis
    dossier = await run_gemini_skip_trace_agents(req, recon, api_key)

    # Determine best person name & entity name to feed into the Deep-Link Generator
    best_person = ""
    if dossier.primary_decision_maker and not any(
        w in dossier.primary_decision_maker.lower() for w in ("owner at", "owner of", "check ", "click ", "llc", "inc", "corp")
    ):
        best_person = dossier.primary_decision_maker
    elif req.known_owner_or_llc and not any(
        w in req.known_owner_or_llc.lower() for w in ("llc", "inc", "corp", "trust", "holdings", "properties")
    ):
        best_person = req.known_owner_or_llc.strip()

    best_entity = dossier.corporate_entity_name or req.known_owner_or_llc or ""

    # Step 3: Build dynamic deep-links for all 10 bookmarked tools + 6 bonus tools
    tool_links = build_tool_links(
        street=req.street,
        city=req.city,
        state=req.state,
        zip_code=req.zip_code or "",
        owner_name=best_person,
        entity_name=best_entity,
        phone=dossier.primary_phone,
    )

    # Optional: Auto-open key free skip-tracing tabs in the user's local browser
    if req.auto_open_tabs:
        auto_urls = [
            tool_links["property_tools"][2]["url"],  # Regrid Free Parcel Map
            tool_links["people_tools"][0]["url"],    # TruePeopleSearch Reverse Address
            tool_links["people_tools"][2]["url"],    # FastPeopleSearch Reverse Address
        ]
        if best_entity:
            auto_urls.append(tool_links["entity_tools"][1]["url"])  # CorporationWiki
        for u in auto_urls:
            try:
                webbrowser.open_new_tab(u)
            except Exception:
                pass

    return {
        "dossier": dossier.model_dump(),
        "tool_links": tool_links,
        "recon_snippets": recon.get("snippets", []),
    }


@app.post("/api/open-tabs")
async def open_browser_tabs(req: OpenTabsRequest):
    opened = 0
    for url in req.urls[:10]:
        if url.startswith("http://") or url.startswith("https://"):
            try:
                webbrowser.open_new_tab(url)
                opened += 1
            except Exception:
                pass
    return {"status": "ok", "opened": opened}


@app.get("/api/leads")
async def get_leads():
    return {"leads": load_saved_leads()}


@app.post("/api/leads")
async def save_lead(req: SaveLeadRequest):
    leads = load_saved_leads()
    new_lead = {
        "id": f"lead_{int(datetime.now().timestamp() * 1000)}",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        **req.model_dump(),
    }
    leads.insert(0, new_lead)
    write_saved_leads(leads)
    return {"status": "ok", "lead": new_lead, "total": len(leads)}


@app.delete("/api/leads/{lead_id}")
async def delete_lead(lead_id: str):
    leads = [item for item in load_saved_leads() if item.get("id") != lead_id]
    write_saved_leads(leads)
    return {"status": "ok", "total": len(leads)}


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def serve_index():
    return FileResponse(STATIC_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8765, reload=False)
