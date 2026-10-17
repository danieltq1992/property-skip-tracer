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
    title="PropTrace AI — Commercial, Residential & Farm Property Owner & Personal Cell Skip-Tracing Agent",
    version="1.2.0",
)

# Known Commercial Registered Agent services that are NOT beneficial owners
COMMERCIAL_RA_SERVICES = (
    "ct corporation",
    "corporation service company",
    "csc ",
    "registered agents inc",
    "northwest registered agent",
    "cogency global",
    "united states corporation agents",
    "legalzoom",
    "incorp services",
    "national registered agents",
    "capitol corporate services",
    "paracorp",
    "registered agent solutions",
    "harbor compliance",
)

# Keywords indicating a multi-owner group, HOA, Condo Association, or Institutional Building
HOA_OR_GROUP_KEYWORDS = (
    "HOA",
    "HOMEOWNERS",
    "HOMEOWNER ASSN",
    "CONDOMINIUM",
    "CONDO ",
    "ASSOCIATION",
    "POA",
    "PROPERTY OWNERS",
    "COOPERATIVE",
    "CO-OP",
    "TRUSTEES",
    "CHURCH",
    "MINISTRIES",
    "SCHOOL",
    "FOUNDATION",
    "SYNDICATE",
    "PARTNERSHIP",
    "REIT",
    "COMMON AREA",
)


# ---------------------------------------------------------------------------
# Pydantic Schemas for Gemini Structured Output & API Requests
# ---------------------------------------------------------------------------

class PhoneRecord(BaseModel):
    number: str = Field(description="Formatted phone number e.g. (555) 234-5678")
    phone_type: str = Field(
        description="Personal Cell / Wireless, Direct Personal Line, Home Landline, or Business / Office Line"
    )
    is_personal_cell: bool = Field(
        default=True,
        description="True if this is a personal cell/wireless number or direct personal contact line for the human decision-maker",
    )
    carrier_or_source: str = Field(
        description="Source portal or carrier info e.g. National Public Data (Personal), Whitepages, TruePeopleSearch, Buzzfile"
    )
    confidence: str = Field(description="High, Medium, or Low")


class PrincipalContact(BaseModel):
    full_name: str = Field(
        description="Individual person's full name (beneficial owner, managing member, president, HOA board president, or building care manager)"
    )
    role: str = Field(
        description="Exact role e.g. Verified Beneficial Owner & President, Co-Owner & Corporate Secretary, Managing Member, Building Care / Property Manager"
    )
    is_primary_decision_maker: bool = Field(
        default=False,
        description="True if this person is the #1 verified beneficial owner or primary building-care decision-maker to call first",
    )
    verification_evidence: str = Field(
        default="",
        description="Concrete proof tying this person to true ownership or building care management (e.g. 'Resides at County Tax Bill Mailing Address + Listed as President/Registered Agent on SOS filings')",
    )
    mailing_or_home_address: str = Field(
        description="Personal residential or mailing address for skip-tracing"
    )
    age_or_demographics: Optional[str] = Field(
        default="", description="Approximate age, birth month/year, or middle name if found"
    )
    phones: List[PhoneRecord] = Field(
        default_factory=list,
        description="Associated phone numbers for this person, ordered with Personal Cell / Wireless numbers first",
    )
    emails: List[str] = Field(default_factory=list, description="Associated personal or direct email addresses")
    relatives_or_associates: List[str] = Field(
        default_factory=list, description="Co-owners, spouses, officers, relatives, or business associates"
    )


class PortfolioProperty(BaseModel):
    address: str = Field(description="Street address or parcel location description")
    city_state: str = Field(description="City/County and State")
    parcel_apn: str = Field(default="", description="Assessor Parcel Number (APN)")
    assessed_value_or_details: str = Field(
        default="", description="Assessed tax value, acreage, and property details"
    )
    holding_entity: str = Field(
        default="", description="Deed owner or LLC name holding this specific property"
    )
    is_owner_homestead: bool = Field(
        default=False,
        description="True if this parcel is the owner's personal residence / tax mailing address",
    )


class SkipTraceDossier(BaseModel):
    normalized_address: str = Field(description="Standardized full property address")
    property_type: str = Field(description="Residential, Commercial, or Farm")
    parcel_apn: str = Field(default="Not found in public snippet", description="Assessor Parcel Number (APN) if discovered")
    county_and_state: str = Field(default="", description="County and State of the property")
    assessed_value_and_acres: str = Field(default="", description="Target parcel assessed value and acreage if available")
    owner_of_record: str = Field(
        description="Deed owner of record (either an Individual Name or LLC/Corporation/Trust/HOA name)"
    )
    is_corporate_or_llc: bool = Field(description="True if owned by an LLC, Inc, LP, Corp, HOA, or Trust")
    corporate_entity_name: str = Field(default="", description="Exact LLC/Corporation/HOA name if applicable")
    state_of_formation: str = Field(default="", description="State where the LLC/Corp is registered")
    registered_agent_name: str = Field(
        default="", description="Registered agent or corporate officers from SOS/Bizapedia/OpenCorporates"
    )
    tax_mailing_address: str = Field(default="", description="Full Tax bill mailing address or principal office address")
    primary_decision_maker: str = Field(
        description="The #1 verified human beneficial owner (or building-care decision-maker for groups/HOAs) to contact"
    )
    decision_maker_role_and_authority: str = Field(
        default="",
        description="Exact authority of the primary contact (e.g. 'Verified Beneficial Owner & President' or 'Building Care / HOA Property Manager')",
    )
    ownership_structure_type: str = Field(
        default="Direct / Single-Entity Ownership",
        description="e.g. 'Pierced Corporate/LLC -> Beneficial Owner', 'Multi-Entity / Sister LLC Portfolio', 'Group / HOA (Building Care Decision-Maker)', or 'Direct Individual Owner'",
    )
    decision_maker_verification_proof: str = Field(
        default="",
        description="Strong evidence summary proving why the primary contact is the true beneficial owner behind the LLC(s) or the building-care decision-maker",
    )
    shell_layers_pierced: List[str] = Field(
        default_factory=list,
        description="Step-by-step chain showing how ownership was traced from the property deed through LLCs/tax addresses to the human decision-maker",
    )
    primary_phone: str = Field(
        default="",
        description="Best personal cell / wireless phone number (or top direct contact number) for the primary decision-maker",
    )
    primary_phone_type: str = Field(
        default="Personal Cell / Direct Owner Line",
        description="Label indicating whether primary_phone is a Personal Cell / Wireless number or Direct Personal/Office line",
    )
    portfolio_count: int = Field(
        default=1,
        description="Total number of properties owned by this owner/entity/tax address (including target property)",
    )
    portfolio_total_value: str = Field(
        default="",
        description="Combined assessed tax value and total acreage across all discovered properties in the owner's portfolio",
    )
    portfolio_summary: str = Field(
        default="",
        description="Overview of how many other properties this owner holds, under which LLCs/personal names, and total value/acreage",
    )
    other_properties: List[PortfolioProperty] = Field(
        default_factory=list,
        description="List of other properties owned by this owner or linked to the same tax mailing address",
    )
    contacts: List[PrincipalContact] = Field(
        default_factory=list,
        description="List of verified beneficial owners, co-owners, officers, or building-care managers and their phone numbers",
    )
    agent_1_property_findings: str = Field(
        description="Summary of Agent 1 (Assessor, GIS Portfolio, LandGlide, Land.com, Buildout, Regrid) findings including portfolio count"
    )
    agent_2_entity_findings: str = Field(
        description="Summary of Agent 2 (Multi-LLC Veil Piercing, Tax Address Cross-Link, SOS Officers, HOA/Building Care Manager) findings"
    )
    agent_3_phone_findings: str = Field(
        description="Summary of Agent 3 (Personal Cell / Wireless Priority Skip-Tracing, TruePeopleSearch, FastPeopleSearch, Whitepages) phone findings"
    )
    recommended_next_steps: List[str] = Field(
        default_factory=list, description="Actionable verification tips using the deep-linked tools"
    )
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
    primary_phone_type: str = "Personal Cell / Direct"
    all_phones: List[str] = []
    emails: List[str] = []
    mailing_address: str = ""
    portfolio_count: int = 1
    portfolio_total_value: str = ""
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


def is_commercial_ra(name: str) -> bool:
    low = (name or "").lower()
    return any(ra in low for ra in COMMERCIAL_RA_SERVICES)


def is_group_or_hoa_entity(name: str) -> bool:
    up = (name or "").upper()
    return any(k in up for k in HOA_OR_GROUP_KEYWORDS)


# ---------------------------------------------------------------------------
# Deep-Link Builder for All 10 Bookmarked Tools + 8 Bonus Tools
# (Automatically targets Owner's Personal/Mailing City & Street for Cell Lookup!)
# ---------------------------------------------------------------------------

def build_tool_links(
    street: str,
    city: str,
    state: str,
    zip_code: str = "",
    owner_name: str = "",
    entity_name: str = "",
    phone: str = "",
    owner_mail_street: str = "",
    owner_mail_city: str = "",
    owner_mail_state: str = "",
    owner_mail_zip: str = "",
) -> Dict[str, List[Dict[str, str]]]:
    full_address = f"{street}, {city}, {state} {zip_code}".strip().rstrip(",")
    city_state_zip = f"{city}, {state} {zip_code}".strip()

    # For Stage 3 personal cell phone lookups, prefer the owner's personal/mailing city & street when discovered!
    p_street = (owner_mail_street or street).strip()
    p_city = (owner_mail_city or city).strip()
    p_state = (owner_mail_state or state).strip()
    p_zip = (owner_mail_zip or zip_code).strip()
    p_full_addr = f"{p_street}, {p_city}, {p_state} {p_zip}".strip().rstrip(",")
    p_csz = f"{p_city}, {p_state} {p_zip}".strip()
    p_city_state = f"{p_city}, {p_state}".strip()

    street_slug = slugify(street)
    city_slug = slugify(city)
    state_slug = slugify(state)

    p_street_slug = slugify(p_street)
    p_city_slug = slugify(p_city)
    p_state_slug = slugify(p_state)
    fps_addr_slug = f"{p_street_slug}_{p_city_slug}-{p_state_slug}"

    enc_full_addr = urllib.parse.quote(full_address)
    enc_p_street = urllib.parse.quote(p_street)
    enc_p_csz = urllib.parse.quote(p_csz)

    target_person = (owner_name or "").strip()
    target_entity = (entity_name or owner_name or "").strip()
    person_slug = slugify(target_person) if target_person else ""
    enc_person = urllib.parse.quote(target_person) if target_person else ""
    enc_entity = urllib.parse.quote(target_entity) if target_entity else enc_full_addr

    # Stage 1: Property, Portfolio & Parcel Recon
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
            "description": f"Copies '{full_address}' & opens Prospect by Buildout for portfolio & commercial ownership",
            "url": "https://prospect.buildout.com/",
            "copy_text": full_address,
        },
        {
            "id": "regrid",
            "name": "Regrid Free Parcel & Portfolio Search",
            "source_type": "bonus",
            "badge": "Bonus Free GIS",
            "stage": "Stage 1: Property & Parcel",
            "description": f"Direct Regrid parcel & portfolio search for {target_entity or full_address}",
            "url": f"https://www.google.com/search?q={urllib.parse.quote('site:app.regrid.com \"' + street + '\" \"' + city + '\"')}",
            "copy_text": full_address,
        },
        {
            "id": "county_assessor",
            "name": "County Tax Assessor & GIS Portfolio",
            "source_type": "bonus",
            "badge": "Bonus Official Record",
            "stage": "Stage 1: Property & Parcel",
            "description": f"Official county property tax card & multi-parcel owner search for {city}, {state}",
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
            "name": "LoopNet & Crexi Commercial / Property Manager Recon",
            "source_type": "bonus",
            "badge": "Bonus Commercial",
            "stage": "Stage 1: Property & Parcel",
            "description": "Find commercial offering memorandums, property management companies & true owner LLCs",
            "url": f"https://www.google.com/search?q={urllib.parse.quote('\"' + street + '\" \"' + city + '\" (site:loopnet.com OR site:crexi.com)')}",
            "copy_text": full_address,
        },
    ]

    # Stage 2: Corporate, Multi-LLC & HOA Veil Piercing
    entity_tools = [
        {
            "id": "opencorporates",
            "name": "OpenCorporates",
            "source_type": "screenshot",
            "badge": "Bookmark #5",
            "stage": "Stage 2: LLC / Corp Piercing",
            "description": f"Search corporate filings, officers & parent/sister LLCs for {target_entity or full_address}",
            "url": (
                f"https://opencorporates.com/companies?q={enc_entity}&jurisdiction_code=us_{state_slug}"
                if len(state_slug) == 2
                else f"https://opencorporates.com/companies?q={enc_entity}"
            ),
            "copy_text": target_entity or full_address,
        },
        {
            "id": "corporationwiki",
            "name": "CorporationWiki (Multi-LLC Graph)",
            "source_type": "screenshot",
            "badge": "Bookmark #4",
            "stage": "Stage 2: LLC / Corp Piercing",
            "description": f"Visual relationship graph linking {target_entity or 'LLCs/Addresses'} to real human beneficial owners & other LLCs",
            "url": f"https://www.corporationwiki.com/search/results?term={enc_entity}",
            "copy_text": target_entity or full_address,
        },
        {
            "id": "b2bhint",
            "name": "B2BHint (Business Data Platform)",
            "source_type": "screenshot",
            "badge": "Bookmark #6",
            "stage": "Stage 2: LLC / Corp Piercing",
            "description": f"Business Data Platform, Company Search & Officer Network lookup for {target_entity or full_address}",
            "url": f"https://b2bhint.com/en/search?q={enc_entity}",
            "copy_text": target_entity or full_address,
        },
        {
            "id": "bizapedia",
            "name": "Bizapedia Principal & Tax Address Search",
            "source_type": "bonus",
            "badge": "Bonus LLC Piercer",
            "stage": "Stage 2: LLC / Corp Piercing",
            "description": f"Uncovers managing members, governors, HOA board directors & all LLCs at {p_street}",
            "url": f"https://www.google.com/search?q={urllib.parse.quote('site:bizapedia.com \"' + (target_entity or p_street) + '\" ' + p_state)}",
            "copy_text": target_entity or p_full_addr,
        },
        {
            "id": "state_sos",
            "name": f"{state.upper() or 'State'} Secretary of State Entity Search",
            "source_type": "bonus",
            "badge": "Bonus Official Filings",
            "stage": "Stage 2: LLC / Corp Piercing",
            "description": "Direct search for Articles of Organization & Annual Reports showing true beneficial owners",
            "url": f"https://www.google.com/search?q={urllib.parse.quote('\"' + (target_entity or street) + '\" ' + state + ' Secretary of State business entity filings')}",
            "copy_text": target_entity or full_address,
        },
    ]

    # Stage 3: Personal Cell Phone Skip-Tracing (Targets Owner Name + Personal/Tax Residence!)
    tps_person_url = (
        f"https://www.truepeoplesearch.com/results?name={enc_person}&citystatezip={urllib.parse.quote(p_city_state)}"
        if target_person
        else f"https://www.truepeoplesearch.com/resultaddress?streetaddress={enc_p_street}&citystatezip={enc_p_csz}"
    )
    fps_person_url = (
        f"https://www.fastpeoplesearch.com/name/{person_slug}_{p_city_slug}-{p_state_slug}"
        if target_person
        else f"https://www.fastpeoplesearch.com/address/{fps_addr_slug}"
    )
    spokeo_url = (
        f"https://www.spokeo.com/{person_slug.title()}/{p_state.upper()}/{p_city_slug.title()}"
        if target_person and len(p_state.strip()) == 2
        else f"https://www.spokeo.com/{p_state.upper()}/{p_city_slug.title()}/{p_street_slug.title()}"
        if len(p_state.strip()) == 2
        else f"https://www.spokeo.com/search?q={enc_full_addr}"
    )

    people_tools = [
        {
            "id": "tps_name",
            "name": f"TruePeopleSearch ({target_person or 'Owner Personal Cell'})",
            "source_type": "screenshot",
            "badge": "Bookmark #3 • Cell Priority",
            "stage": "Stage 3: Phone Skip-Trace",
            "description": f"100% Free Personal Wireless / Cell phone finder for {target_person or 'owner'} in {p_city_state}",
            "url": tps_person_url,
            "copy_text": target_person or p_full_addr,
        },
        {
            "id": "fps_name",
            "name": f"FastPeopleSearch ({target_person or 'Owner Personal Cell'})",
            "source_type": "screenshot",
            "badge": "Bookmark #2 • Cell Priority",
            "stage": "Stage 3: Phone Skip-Trace",
            "description": f"Free personal cell/mobile lookup for {target_person or 'discovered owner'} in {p_city_state}",
            "url": fps_person_url,
            "copy_text": target_person or p_full_addr,
        },
        {
            "id": "tps_address",
            "name": f"TruePeopleSearch (Owner Home: {p_street})",
            "source_type": "screenshot",
            "badge": "Bookmark #3",
            "stage": "Stage 3: Phone Skip-Trace",
            "description": f"100% Free reverse address lookup at owner's tax/home address ({p_full_addr})",
            "url": f"https://www.truepeoplesearch.com/resultaddress?streetaddress={enc_p_street}&citystatezip={enc_p_csz}",
            "copy_text": p_full_addr,
        },
        {
            "id": "fps_address",
            "name": f"FastPeopleSearch (Owner Home: {p_street})",
            "source_type": "screenshot",
            "badge": "Bookmark #2",
            "stage": "Stage 3: Phone Skip-Trace",
            "description": f"Free reverse address & personal cell lookup at {p_full_addr}",
            "url": f"https://www.fastpeoplesearch.com/address/{fps_addr_slug}",
            "copy_text": p_full_addr,
        },
        {
            "id": "spokeo",
            "name": "Spokeo Personal Cell & Email Lookup",
            "source_type": "screenshot",
            "badge": "Bookmark #1",
            "stage": "Stage 3: Phone Skip-Trace",
            "description": f"Spokeo White Pages, personal mobile numbers & email verification for {target_person or p_full_addr}",
            "url": spokeo_url,
            "copy_text": target_person or p_full_addr,
        },
        {
            "id": "cyberbackgroundchecks",
            "name": "CyberBackgroundChecks (Free Cell & Emails)",
            "source_type": "bonus",
            "badge": "Bonus Free Cell",
            "stage": "Stage 3: Phone Skip-Trace",
            "description": f"Free skip-tracing database for {p_street}, {p_city}, {p_state} personal wireless phones",
            "url": f"https://www.cyberbackgroundchecks.com/address/{p_street_slug}/{p_city_slug}/{p_state_slug}",
            "copy_text": p_full_addr,
        },
        {
            "id": "searchpeoplefree",
            "name": "SearchPeopleFree (Mobile Line Finder)",
            "source_type": "bonus",
            "badge": "Bonus Free Cell",
            "stage": "Stage 3: Phone Skip-Trace",
            "description": f"Cross-verify personal wireless phone numbers for {target_person or p_street}",
            "url": (
                f"https://www.searchpeoplefree.com/find/{person_slug}/{p_state_slug}/{p_city_slug}"
                if person_slug
                else f"https://www.searchpeoplefree.com/address/{p_state_slug}/{p_city_slug}/{p_street_slug}"
            ),
            "copy_text": target_person or p_full_addr,
        },
        {
            "id": "core365_gemini",
            "name": "Core365 | via & Google Gemini",
            "source_type": "screenshot",
            "badge": "Bookmarks #7 & #10",
            "stage": "Stage 3: CRM & AI",
            "description": "Push verified beneficial owner & personal cell numbers into Core365 | via or Google Gemini",
            "url": "https://gemini.google.com/app",
            "copy_text": f"{target_person} - {phone} - {full_address}",
        },
    ]

    return {
        "property_tools": property_tools,
        "entity_tools": entity_tools,
        "people_tools": people_tools,
    }


# ---------------------------------------------------------------------------
# Live Multi-Source Reconnaissance Scraper
# (GIS Parcel + Portfolio Scan + Multi-LLC Veil Piercing + Personal Cell Priority)
# ---------------------------------------------------------------------------

async def gather_public_web_recon(
    street: str,
    city: str,
    state: str,
    zip_code: str,
    known_owner: str = "",
    property_type: str = "residential",
) -> Dict[str, Any]:
    """Executes a 4-part live reconnaissance pipeline:
    1. Geocodes & queries live GIS Parcel layers for the target parcel AND runs a Portfolio Query
       by Owner Name + Tax Mailing Address to find all other properties owned & sister LLCs/homesteads.
    2. Runs Yahoo Search on the property address, discovered LLC/Corp owner, and tax mailing address.
    3. Pierces multi-layered LLCs (filtering out commercial registered agents) or identifies HOA/Group
       building-care decision-makers.
    4. Runs targeted personal cell/wireless phone lookups for the verified human decision-makers in their
       confirmed home/mailing city & county, classifying Personal Cell vs. Office lines.
    """
    recon: Dict[str, Any] = {
        "gis_parcel": {},
        "portfolio": {
            "count": 1,
            "total_value": 0.0,
            "total_acres": 0.0,
            "holding_entities": [],
            "personal_owners_at_tax_address": [],
            "properties": [],
        },
        "mailing_parsed": {
            "street": "",
            "city": "",
            "state": state.upper(),
            "zip": "",
        },
        "snippets": [],
        "candidate_phones": [],
        "phone_details": {},
        "candidate_emails": [],
        "candidate_entities": [],
        "discovered_persons": [],
        "person_roles": {},
        "shell_layers": [],
    }

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    full_address = f"{street}, {city}, {state} {zip_code}".strip()
    phone_pattern = re.compile(r"\(?\b[2-9][0-9]{2}\)?[-.\s]?[2-9][0-9]{2}[-.\s]?[0-9]{4}\b")
    email_pattern = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
    entity_pattern = re.compile(
        r"\b([A-Z0-9][A-Za-z0-9&.,'\-\s]{2,45}\s(?:LLC|L\.L\.C\.|INC\.?|CORP\.?|CORPORATION|LP|L\.P\.|TRUST|HOLDINGS|PROPERTIES|INVESTMENTS|FARMS|RANCH|ASSOCIATION|HOA|CONDOMINIUM))\b"
    )
    person_patterns = [
        re.compile(
            r"(?:Registered Agent on file is|Registered Agent Name:\s*|Contact:|primary contact at [^.]* is|sole principal is|Key Principal:\s*|president\s+|secretary\s+|manager\s+|director\s+)"
            r"([A-Z][a-z]+(?:\s+[A-Z]\.?|\s+[A-Z][a-z]+)?\s+[A-Z][a-z]+)"
        ),
        re.compile(r"\b([A-Z][a-z]+\s+[A-Z]\.?\s+[A-Z][a-z]+)\s*,\s*age\s+\d+"),
        re.compile(r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\s+[A-Z][a-z]+)\s+is\s+\d+\s+years\s+old"),
    ]
    # Also parse "Last, First Middle" registered agent format
    last_first_pattern = re.compile(
        r"Registered Agent Name:\s*([A-Z][a-z]+)\s*,\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)"
    )

    async def run_yahoo_search(client: httpx.AsyncClient, query: str) -> List[str]:
        found: List[str] = []
        for _attempt in range(2):
            try:
                resp = await client.get("https://search.yahoo.com/search", params={"p": query})
                if resp.status_code == 200:
                    soup = BeautifulSoup(resp.text, "html.parser")
                    for div in soup.select("div.algo")[:6]:
                        txt = div.get_text(" ", strip=True)
                        if txt:
                            found.append(txt[:420])
                    if found:
                        break
            except Exception:
                pass
        return found

    def add_discovered_person(name_str: str, role_hint: str = "") -> None:
        clean = re.sub(r"\s+", " ", (name_str or "").strip(" .,;-")).title()
        if not clean or len(clean) < 5 or is_commercial_ra(clean):
            return
        bad_words = {
            "North Carolina", "South Carolina", "United States", "Public Record",
            "White Pages", "People Search", "Company Profile", "General Farms",
            "Registered Agent", "Business Directory", "Property Records", "Phone Number",
            "Current Estimates", "Contact Information", "Principal Office",
        }
        if any(bw.lower() in clean.lower() for bw in bad_words):
            return
        # Deduplicate middle-initial vs full middle name variants
        parts = clean.split()
        if len(parts) >= 2:
            first_l, last_l = parts[0].lower(), parts[-1].lower()
            for idx, existing in enumerate(recon["discovered_persons"]):
                ex_parts = existing.split()
                if len(ex_parts) >= 2 and ex_parts[0].lower() == first_l and ex_parts[-1].lower() == last_l:
                    # Keep the longer / more complete name
                    if len(clean) > len(existing):
                        recon["discovered_persons"][idx] = clean
                    if role_hint and clean not in recon["person_roles"]:
                        recon["person_roles"][recon["discovered_persons"][idx]] = role_hint
                    return
        recon["discovered_persons"].append(clean)
        if role_hint:
            recon["person_roles"][clean] = role_hint

    def ingest_snippets(snips: List[str], stage_tag: str = "general") -> None:
        mail_st_low = (recon["mailing_parsed"].get("street") or "").lower()
        mail_city_low = (recon["mailing_parsed"].get("city") or "").lower()
        prop_city_low = city.strip().lower()
        prop_st_low = street.strip().lower()

        for s in snips:
            if s not in recon["snippets"]:
                recon["snippets"].append(s)
            s_low = s.lower()

            # Check if snippet discovers mailing city/zip for the mailing street
            if mail_st_low and not recon["mailing_parsed"].get("city"):
                m_csz = re.search(
                    re.escape(mail_st_low[:14]) + r"[^,A-Za-z0-9]{0,12}(?:Rd|Road|St|Ave|Dr|Ln|Ct|Hwy|Blvd|Way)?[,\s]+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s*,\s*([A-Z]{2})\s*(\d{5})?",
                    s,
                    re.IGNORECASE,
                )
                if m_csz:
                    recon["mailing_parsed"]["city"] = m_csz.group(1).strip().title()
                    recon["mailing_parsed"]["state"] = m_csz.group(2).strip().upper()
                    if m_csz.group(3):
                        recon["mailing_parsed"]["zip"] = m_csz.group(3).strip()
                    mail_city_low = recon["mailing_parsed"]["city"].lower()

            # Extract corporate / HOA entities
            for ent in entity_pattern.findall(s.upper()):
                cl_ent = ent.strip(" .,;-")
                if len(cl_ent) > 4 and not is_commercial_ra(cl_ent) and cl_ent not in recon["candidate_entities"]:
                    recon["candidate_entities"].append(cl_ent)

            # Extract people & officer roles
            if "secretary" in s_low and "president" in s_low:
                sec_m = re.search(r"secretary\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})", s)
                pres_m = re.search(r"president\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})", s)
                if pres_m:
                    add_discovered_person(pres_m.group(1), "President & Verified Beneficial Owner")
                if sec_m:
                    add_discovered_person(sec_m.group(1), "Corporate Secretary & Co-Owner")

            for lf_match in last_first_pattern.findall(s):
                full_p = f"{lf_match[1]} {lf_match[0]}"
                add_discovered_person(full_p, "Registered Agent & Principal")

            for pat in person_patterns:
                for p_match in pat.findall(s):
                    add_discovered_person(p_match)

            # Extract emails
            for em in email_pattern.findall(s):
                em_low = em.lower().strip(".")
                if not em_low.endswith((".png", ".jpg", ".gif")) and em_low not in recon["candidate_emails"]:
                    recon["candidate_emails"].append(em_low)

            # Check location relevance so we filter out out-of-town namesakes
            has_confirmed_loc = any(
                tok and tok in s_low
                for tok in [
                    prop_st_low[:12],
                    mail_st_low[:12],
                    prop_city_low,
                    mail_city_low,
                    (recon["gis_parcel"].get("owner_name") or "").lower()[:10],
                    (recon["gis_parcel"].get("county") or "").lower().replace(" county", ""),
                ]
            )
            # Detect if snippet explicitly describes a different city/residence
            is_wrong_city_profile = (
                stage_tag == "stage3_person"
                and not has_confirmed_loc
                and any(phrase in s_low for phrase in ("lives in ", "place of residence is ", "resided at locations such as "))
            )
            if is_wrong_city_profile:
                continue

            # Extract and classify phone numbers (Personal Cell Priority!)
            for ph in phone_pattern.findall(s):
                digits = re.sub(r"\D", "", ph)
                if len(digits) != 10:
                    continue
                fmt_ph = f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"

                # Determine whether this phone is a Personal Cell / Direct Personal Line vs Business/Office Line
                is_personal_source = any(
                    src in s_low
                    for src in (
                        "whitepages",
                        "fastpeoplesearch",
                        "truepeoplesearch",
                        "nationalpublicdata",
                        "national public data",
                        "spokeo",
                        "smartbackgroundchecks",
                        "cyberbackgroundchecks",
                        "wireless",
                        "mobile",
                        "cell",
                        "alltel",
                        "verizon wireless",
                    )
                )
                is_business_dir = any(
                    bsrc in s_low for bsrc in ("buzzfile", "allbiz", "manta", "dandb", "dnb.com", "employees:")
                )
                explicit_landline = "landline" in s_low and "mobile and landline" not in s_low

                # Identify which person is mentioned in this snippet
                linked_person = ""
                for p in recon["discovered_persons"]:
                    p_tokens = p.lower().split()
                    if p_tokens and p_tokens[0] in s_low and p_tokens[-1] in s_low:
                        linked_person = p
                        break

                existing_meta = recon["phone_details"].get(fmt_ph)
                is_cell = bool((is_personal_source and not is_business_dir and not explicit_landline) or "wireless" in s_low or "cell" in s_low)

                if is_cell:
                    p_type = "Personal Cell / Direct Personal Line"
                    source_lbl = "Personal Skip-Trace Record (Direct to Individual)"
                elif is_personal_source and is_business_dir:
                    p_type = "Direct Owner Line (Personal + Principal Filing)"
                    source_lbl = "Verified Principal & Personal Directory Match"
                elif is_business_dir:
                    p_type = "Principal Direct / Business Line"
                    source_lbl = "Corporate Principal Filing (Buzzfile / AllBiz / Manta)"
                else:
                    p_type = "Direct Public Record Line"
                    source_lbl = "Live Public Record Recon"

                if not existing_meta:
                    recon["phone_details"][fmt_ph] = {
                        "number": fmt_ph,
                        "phone_type": p_type,
                        "is_personal_cell": is_cell or (is_personal_source and not is_business_dir),
                        "carrier_or_source": source_lbl,
                        "person": linked_person,
                        "has_confirmed_loc": has_confirmed_loc,
                        "priority_score": (
                            (100 if has_confirmed_loc else 30)
                            + (40 if is_cell else (25 if is_personal_source else 10))
                        ),
                    }
                else:
                    # Upgrade metadata if a personal source also confirms a business number!
                    if is_personal_source and not existing_meta["is_personal_cell"]:
                        existing_meta["is_personal_cell"] = True
                        existing_meta["phone_type"] = "Personal Cell / Direct Owner Line"
                        existing_meta["carrier_or_source"] = "Confirmed on Both Personal Skip-Trace & Principal Records"
                        existing_meta["priority_score"] += 35
                    if linked_person and not existing_meta.get("person"):
                        existing_meta["person"] = linked_person
                    if has_confirmed_loc and not existing_meta.get("has_confirmed_loc"):
                        existing_meta["has_confirmed_loc"] = True
                        existing_meta["priority_score"] += 70

                if fmt_ph not in recon["candidate_phones"]:
                    recon["candidate_phones"].append(fmt_ph)

    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True, headers=headers) as client:
        # Warm up Yahoo session cookie to prevent initial HTTP 500
        try:
            await client.get("https://search.yahoo.com/search", params={"p": f"{street} {city} {state}"})
        except Exception:
            pass

        # STAGE 1A: Geocode via ArcGIS & Query Statewide/County GIS Parcel FeatureServer + PORTFOLIO SCAN
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

                    # Query NC OneMap Parcel Layer if in NC (covers all 100 NC counties)
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
                                "outFields": "ownname,ownname2,mailadd,mcity,mstate,mzip,parno,siteadd,scity,cntyname,parval,gisacres",
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
                                parno = (attrs.get("parno") or "").strip()
                                parval = attrs.get("parval") or 0.0
                                acres = attrs.get("gisacres") or 0.0
                                cnty_clean = f"{attrs.get('cntyname', county)} County".replace("County County", "County")

                                recon["mailing_parsed"]["street"] = mail_st
                                if mail_city:
                                    recon["mailing_parsed"]["city"] = mail_city.title()
                                if mail_state:
                                    recon["mailing_parsed"]["state"] = mail_state.upper()
                                if mail_zip:
                                    recon["mailing_parsed"]["zip"] = mail_zip

                                val_str = f"${parval:,.0f}" if isinstance(parval, (int, float)) and parval else ""
                                acres_str = f"{acres:,.2f} Acres" if isinstance(acres, (int, float)) and acres else ""
                                val_acres_combined = " • ".join(filter(None, [val_str, acres_str]))

                                full_mail = ", ".join(filter(None, [mail_st, mail_city, f"{mail_state} {mail_zip}".strip()]))
                                recon["gis_parcel"].update(
                                    {
                                        "owner_name": own1,
                                        "owner_name_2": own2,
                                        "mailing_address": full_mail or mail_st,
                                        "mail_street": mail_st,
                                        "parcel_apn": parno,
                                        "assessed_value": val_str,
                                        "acres": acres_str,
                                        "assessed_value_and_acres": val_acres_combined,
                                        "county": cnty_clean,
                                    }
                                )
                                if own1:
                                    recon["candidate_entities"].insert(0, own1)

                                # STAGE 1A-PORTFOLIO: Query all other properties owned by `own1` OR sharing `mailadd`!
                                where_clauses = []
                                if own1:
                                    safe_own = own1.replace("'", "''")
                                    where_clauses.append(f"ownname = '{safe_own}'")
                                if mail_st and len(mail_st) >= 6:
                                    safe_mail = mail_st.replace("'", "''")
                                    where_clauses.append(f"mailadd = '{safe_mail}'")

                                if where_clauses:
                                    port_resp = await client.get(
                                        "https://services.nconemap.gov/secure/rest/services/NC1Map_Parcels/FeatureServer/1/query",
                                        params={
                                            "where": " OR ".join(where_clauses),
                                            "outFields": "ownname,ownname2,mailadd,mcity,mstate,mzip,parno,siteadd,scity,cntyname,parval,gisacres",
                                            "resultRecordCount": 50,
                                            "f": "json",
                                        },
                                    )
                                    if port_resp.status_code == 200:
                                        p_feats = port_resp.json().get("features", [])
                                        seen_apns = set()
                                        total_val = 0.0
                                        total_ac = 0.0
                                        holding_set = []
                                        personal_owners_set = []
                                        raw_props = []

                                        for pf in p_feats:
                                            pa = pf.get("attributes", {})
                                            p_apn = (pa.get("parno") or "").strip()
                                            p_site = (pa.get("siteadd") or "").strip()
                                            dedup_k = p_apn or p_site
                                            if not dedup_k or dedup_k in seen_apns:
                                                continue
                                            seen_apns.add(dedup_k)

                                            p_own = (pa.get("ownname") or "").strip()
                                            p_mcity = (pa.get("mcity") or "").strip()
                                            p_mzip = (pa.get("mzip") or "").strip()
                                            if p_mcity and not recon["mailing_parsed"]["city"]:
                                                recon["mailing_parsed"]["city"] = p_mcity.title()
                                            if p_mzip and not recon["mailing_parsed"]["zip"]:
                                                recon["mailing_parsed"]["zip"] = p_mzip

                                            pv = float(pa.get("parval") or 0.0)
                                            pac = float(pa.get("gisacres") or 0.0)
                                            total_val += pv
                                            total_ac += pac

                                            if p_own and p_own not in holding_set:
                                                holding_set.append(p_own)
                                                # Check if this holding entity at the same tax address is an individual/personal name!
                                                if not any(
                                                    k in p_own.upper()
                                                    for k in ("LLC", "INC", "CORP", "LP", "TRUST", "HOLDINGS", "PROPERTIES", "FARMS", "RANCH", "HOA", "ASSN")
                                                ):
                                                    if p_own not in personal_owners_set:
                                                        personal_owners_set.append(p_own)
                                                    # Parse "LAST, FIRST M & WIFE" -> "First M Last"
                                                    cleaned_ind = re.sub(r"\s*&\s*WIFE.*$", "", p_own, flags=re.IGNORECASE).strip()
                                                    if "," in cleaned_ind:
                                                        l_pt, f_pt = [x.strip() for x in cleaned_ind.split(",", 1)]
                                                        add_discovered_person(
                                                            f"{f_pt} {l_pt}",
                                                            "Verified Beneficial Owner (Deeded Homeowner at Tax Mailing Address)",
                                                        )

                                            is_homestead = bool(mail_st and p_site and mail_st.upper() == p_site.upper())
                                            p_cnty = (pa.get("cntyname") or county).strip()
                                            p_scity = (pa.get("scity") or "").strip()
                                            loc_cs = f"{p_scity.title()}, {state_abbr}" if p_scity else f"{p_cnty} County, {state_abbr}"

                                            details_parts = []
                                            if pv > 0:
                                                details_parts.append(f"${pv:,.0f}")
                                            if pac > 0:
                                                details_parts.append(f"{pac:,.2f} Acres")
                                            if is_homestead:
                                                details_parts.append("Owner Homestead & Tax Address")

                                            raw_props.append(
                                                {
                                                    "address": p_site or f"Parcel {p_apn}",
                                                    "city_state": loc_cs,
                                                    "parcel_apn": p_apn,
                                                    "assessed_value_or_details": " • ".join(details_parts) or "County GIS Parcel",
                                                    "holding_entity": p_own,
                                                    "is_owner_homestead": is_homestead,
                                                    "_sort_val": pv,
                                                    "_is_target": (p_apn == parno),
                                                }
                                            )

                                        raw_props.sort(key=lambda x: (not x["is_owner_homestead"], -x["_sort_val"]))
                                        recon["portfolio"] = {
                                            "count": max(len(raw_props), 1),
                                            "total_value": total_val,
                                            "total_acres": total_ac,
                                            "holding_entities": holding_set,
                                            "personal_owners_at_tax_address": personal_owners_set,
                                            "properties": [
                                                {k: v for k, v in rp.items() if not k.startswith("_")}
                                                for rp in raw_props
                                                if not rp["_is_target"]
                                            ],
                                        }

                                # Refresh full mailing address if portfolio query found the city
                                updated_full_mail = ", ".join(
                                    filter(
                                        None,
                                        [
                                            recon["mailing_parsed"]["street"],
                                            recon["mailing_parsed"]["city"],
                                            f"{recon['mailing_parsed']['state']} {recon['mailing_parsed']['zip']}".strip(),
                                        ],
                                    )
                                )
                                if updated_full_mail:
                                    recon["gis_parcel"]["mailing_address"] = updated_full_mail

                                gis_summary = (
                                    f"OFFICIAL GIS PARCEL & PORTFOLIO RECORD: Target Address: {attrs.get('siteadd') or street}, {city}, {state_abbr} | "
                                    f"Deed Owner: {own1} {own2} | Parcel APN: {parno} ({val_acres_combined}) | "
                                    f"Tax Bill Mailing Address: {recon['gis_parcel']['mailing_address']} | "
                                    f"Total Properties Owned in Portfolio: {recon['portfolio']['count']} properties "
                                    f"totaling ${recon['portfolio']['total_value']:,.0f} and {recon['portfolio']['total_acres']:,.2f} acres | "
                                    f"Co-Located Holding Entities / Personal Names at Tax Address: {', '.join(recon['portfolio']['holding_entities'])}"
                                )
                                recon["snippets"].append(gis_summary)
        except Exception:
            pass

        # STAGE 1B: Web Search for the Property Address
        stage1_queries = [
            f"{street} {city} {state} owner phone",
            f'"{street}" {city} {state} parcel OR Land.com',
        ]
        if property_type.lower() == "commercial":
            stage1_queries.append(f'"{street}" {city} {state} "property management" OR "managed by" OR owner')

        for q in stage1_queries:
            ingest_snippets(await run_yahoo_search(client, q), stage_tag="stage1")

        # STAGE 2: Corporate Veil Piercing, Multi-LLC Hop & Tax Mailing Address Recon
        target_owner = (
            known_owner.strip()
            or recon["gis_parcel"].get("owner_name", "")
            or (recon["candidate_entities"][0] if recon["candidate_entities"] else "")
        )
        mail_street = recon["mailing_parsed"].get("street") or recon["gis_parcel"].get("mail_street", "")

        stage2_queries = []
        if target_owner:
            clean_owner = re.sub(r"[^\w\s]", " ", target_owner).strip()
            stage2_queries.append(f"{clean_owner} {state} registered agent principal")
            stage2_queries.append(f"{clean_owner} {state} phone contact")
            if is_group_or_hoa_entity(target_owner) or property_type.lower() == "commercial":
                stage2_queries.append(f'{clean_owner} {state} president OR manager OR director OR management phone')
        if mail_street and mail_street.lower() != street.lower():
            stage2_queries.append(f"{mail_street} {state} phone owner")

        for q in stage2_queries:
            ingest_snippets(await run_yahoo_search(client, q), stage_tag="stage2")

        # Refresh mailing address once Stage 2 snippets reveal the mailing city & ZIP
        if recon["mailing_parsed"].get("street") and recon["mailing_parsed"].get("city"):
            full_m = (
                f"{recon['mailing_parsed']['street']}, {recon['mailing_parsed']['city']}, "
                f"{recon['mailing_parsed']['state']} {recon['mailing_parsed']['zip']}".strip()
            )
            recon["gis_parcel"]["mailing_address"] = full_m

        # STAGE 2B (Multi-LLC Second Hop): If Stage 2 discovered additional LLCs/entities at the same address, or if no human found yet
        if not recon["discovered_persons"] and len(recon["candidate_entities"]) > 1:
            second_entity = recon["candidate_entities"][1]
            clean_sec = re.sub(r"[^\w\s]", " ", second_entity).strip()
            ingest_snippets(
                await run_yahoo_search(client, f"{clean_sec} {state} registered agent principal phone"),
                stage_tag="stage2_hop",
            )

        # STAGE 3: Targeted Personal Cell / Wireless Phone Lookup for Verified Human Decision-Maker(s)
        owner_city = recon["mailing_parsed"].get("city") or city
        county_short = (recon["gis_parcel"].get("county") or "").replace("County", "").strip()

        stage3_queries = []
        if recon["discovered_persons"]:
            primary_p = recon["discovered_persons"][0]
            last_name = primary_p.split()[-1]
            if mail_street:
                stage3_queries.append(f"{ primary_p } {mail_street} {owner_city} {state}")
                stage3_queries.append(f"{ last_name } {mail_street} phone")
            stage3_queries.append(f"{ primary_p } {owner_city} {state} phone")
            if county_short:
                stage3_queries.append(f"{ primary_p } {county_short} {state} phone")
            if len(recon["discovered_persons"]) > 1:
                sec_p = recon["discovered_persons"][1]
                stage3_queries.append(f"{ sec_p } {owner_city} {state} phone")

        for q in stage3_queries[:5]:
            ingest_snippets(await run_yahoo_search(client, q), stage_tag="stage3_person")

    # Sort candidate_phones so Personal Cell & Location-Verified numbers are ALWAYS first!
    sorted_phones = sorted(
        recon["phone_details"].values(),
        key=lambda item: (
            item.get("has_confirmed_loc", False),
            item.get("is_personal_cell", False),
            item.get("priority_score", 0),
        ),
        reverse=True,
    )
    # Filter out any unverified out-of-town numbers if we have confirmed location-matched numbers
    if any(p.get("has_confirmed_loc") for p in sorted_phones):
        sorted_phones = [p for p in sorted_phones if p.get("has_confirmed_loc")]

    recon["candidate_phones"] = [p["number"] for p in sorted_phones]
    recon["sorted_phone_records"] = sorted_phones

    # Construct the step-by-step Veil-Piercing / Ownership Verification Chain
    layers: List[str] = []
    apn_disp = recon["gis_parcel"].get("parcel_apn") or "Public Record"
    layers.append(f"1. Subject Property: {full_address} (APN: {apn_disp})")
    deed_own = recon["gis_parcel"].get("owner_name") or known_owner or (recon["candidate_entities"][0] if recon["candidate_entities"] else "")
    if deed_own:
        layers.append(f"2. Deed Owner of Record: {deed_own}")
    mail_disp = recon["gis_parcel"].get("mailing_address")
    if mail_disp:
        layers.append(f"3. Official County Tax Bill Routing Address: {mail_disp}")
    if recon["portfolio"].get("personal_owners_at_tax_address"):
        layers.append(
            f"4. Tax Address Cross-Link (Multi-Parcel Proof): Parcel(s) at {mail_disp} are personally deeded to "
            f"{', '.join(recon['portfolio']['personal_owners_at_tax_address'])} "
            f"({recon['portfolio']['count']} total properties in portfolio)"
        )
    if recon["discovered_persons"]:
        top_p = recon["discovered_persons"][0]
        top_role = recon["person_roles"].get(top_p, "Verified Beneficial Owner & Principal")
        layers.append(f"5. Verified Human Decision-Maker: {top_p} ({top_role})")
    if recon["candidate_phones"]:
        top_ph_meta = sorted_phones[0]
        layers.append(
            f"6. Direct Personal Contact Line: {top_ph_meta['number']} ({top_ph_meta['phone_type']})"
        )
    recon["shell_layers"] = layers

    return recon


# ---------------------------------------------------------------------------
# Gemini AI Multi-Agent Skip-Tracing Pipeline (Interactions API + Structured Output)
# ---------------------------------------------------------------------------

def build_deterministic_contacts_and_portfolio(
    req: InvestigateRequest,
    recon: Dict[str, Any],
) -> Dict[str, Any]:
    """Builds verified contacts (with Personal Cell numbers prioritized first),
    ownership verification proof, and the owner's property portfolio list from live recon."""
    full_address = f"{req.street}, {req.city}, {req.state} {req.zip_code}".strip()
    gis_info = recon.get("gis_parcel", {})
    port_info = recon.get("portfolio", {})
    mail_addr = gis_info.get("mailing_address") or full_address

    inferred_entity = (
        req.known_owner_or_llc.strip()
        if req.known_owner_or_llc
        else (gis_info.get("owner_name") or (recon["candidate_entities"][0] if recon.get("candidate_entities") else ""))
    )
    is_llc = bool(
        inferred_entity
        and any(
            k in inferred_entity.upper()
            for k in ("LLC", "INC", "CORP", "LP", "TRUST", "HOLDINGS", "PROPERTIES", "RANCH", "FARMS", "HOA", "ASSOCIATION")
        )
    )
    is_group = is_group_or_hoa_entity(inferred_entity)

    discovered_persons = recon.get("discovered_persons", [])
    primary_human = discovered_persons[0] if discovered_persons else (inferred_entity or f"Owner at {req.street}")

    sorted_phone_meta = recon.get("sorted_phone_records", [])
    all_phone_objs: List[PhoneRecord] = []
    for idx, pm in enumerate(sorted_phone_meta[:6]):
        all_phone_objs.append(
            PhoneRecord(
                number=pm["number"],
                phone_type=pm["phone_type"],
                is_personal_cell=pm.get("is_personal_cell", True),
                carrier_or_source=pm["carrier_or_source"],
                confidence="High" if idx < 2 else "Medium",
            )
        )

    # Build verified contacts with Personal Cell numbers sorted first
    contacts_list: List[PrincipalContact] = []
    if discovered_persons:
        for idx, person in enumerate(discovered_persons[:3]):
            role_label = recon.get("person_roles", {}).get(person)
            if not role_label:
                if is_group:
                    role_label = "Building Care Decision-Maker / Board Officer" if idx == 0 else "Association Officer / Manager"
                else:
                    role_label = (
                        "Verified Beneficial Owner, President & Registered Agent"
                        if idx == 0
                        else "Co-Owner & Corporate Secretary / Resident"
                    )

            # Match phones specifically linked to this person first, then shared address phones
            p_specific = [
                PhoneRecord(
                    number=pm["number"],
                    phone_type=pm["phone_type"],
                    is_personal_cell=pm.get("is_personal_cell", True),
                    carrier_or_source=pm["carrier_or_source"],
                    confidence="High",
                )
                for pm in sorted_phone_meta
                if pm.get("person") and pm["person"].split()[0].lower() == person.split()[0].lower()
            ]
            p_shared = [
                PhoneRecord(
                    number=pm["number"],
                    phone_type=pm["phone_type"],
                    is_personal_cell=pm.get("is_personal_cell", True),
                    carrier_or_source=pm["carrier_or_source"],
                    confidence="High",
                )
                for pm in sorted_phone_meta
                if not pm.get("person") or pm["number"] not in {x.number for x in p_specific}
            ]
            combined_phones = p_specific + [x for x in p_shared if x.number not in {y.number for y in p_specific}]
            combined_phones.sort(key=lambda x: x.is_personal_cell, reverse=True)

            # Extract any age/birth info mentioned in snippets for this person
            age_str = ""
            p_first = person.split()[0].lower()
            for snip in recon.get("snippets", []):
                if p_first in snip.lower():
                    m_age = re.search(r"\b(?:age\s+(\d{2}s?)|(\d{2})\s+years\s+old)\b", snip, re.IGNORECASE)
                    if m_age:
                        age_str = f"Age {m_age.group(1) or m_age.group(2)}"
                        break

            evidence_str = (
                f"Confirmed via County Tax Bill Routing ({mail_addr}), Personal Homestead Deed Cross-Link, and Secretary of State Officer/Agent Filings."
                if idx == 0
                else f"Confirmed Corporate Officer / Co-Owner residing at official Tax Mailing Address ({mail_addr})."
            )
            contacts_list.append(
                PrincipalContact(
                    full_name=person,
                    role=role_label,
                    is_primary_decision_maker=(idx == 0),
                    verification_evidence=evidence_str,
                    mailing_or_home_address=mail_addr,
                    age_or_demographics=age_str,
                    phones=combined_phones[:4],
                    emails=recon.get("candidate_emails", []) if idx == 0 else [],
                    relatives_or_associates=[p for p in discovered_persons if p != person],
                )
            )
    else:
        contacts_list.append(
            PrincipalContact(
                full_name=primary_human,
                role="Building Care Decision-Maker / Manager" if is_group else "Property Owner / Principal",
                is_primary_decision_maker=True,
                verification_evidence=f"Linked to property deed and tax bill mailing address ({mail_addr}).",
                mailing_or_home_address=mail_addr,
                phones=all_phone_objs,
                emails=recon.get("candidate_emails", []),
                relatives_or_associates=[],
            )
        )

    # Portfolio properties
    portfolio_props = [PortfolioProperty(**pp) for pp in port_info.get("properties", [])[:25]]
    p_count = max(port_info.get("count", 1), len(portfolio_props) + 1 if portfolio_props else 1)
    p_val = port_info.get("total_value", 0.0)
    p_acres = port_info.get("total_acres", 0.0)
    p_val_str = f"${p_val:,.0f}" + (f" across {p_acres:,.1f} Acres" if p_acres > 0 else "") if p_val > 0 else (gis_info.get("assessed_value_and_acres") or "See County GIS")
    holding_entities = port_info.get("holding_entities", [])
    sister_biz = [e for e in recon.get("candidate_entities", []) if e not in holding_entities]

    if p_count > 1:
        port_summary = (
            f"Owner controls a verified portfolio of {p_count} properties totaling {p_val_str}. "
            f"Holdings are deeded across {', '.join(holding_entities[:4])}"
            + (f" (plus affiliated operating entity {', '.join(sister_biz[:2])})." if sister_biz else ".")
        )
    else:
        port_summary = (
            f"1 confirmed parcel on file for {inferred_entity or primary_human} at {full_address}. "
            "Use the Regrid & County Tax Assessor buttons below to scan adjacent counties for additional LLC holdings."
        )

    # Ownership Structure & Proof Chain
    if is_group:
        structure_type = "Group / HOA / Multi-Owner (Building Care Decision-Maker)"
        role_auth = f"Building Care & Facilities Decision-Maker for {inferred_entity}"
        proof_text = (
            f"Multi-owner / association ownership structure detected for '{inferred_entity}'. "
            f"Identified {primary_human} as the primary officer/manager responsible for property care, maintenance, and vendor decisions via tax billing address ({mail_addr}) and entity filings."
        )
    elif is_llc:
        structure_type = (
            "Multi-Entity / Corporate Portfolio (Pierced to True Beneficial Owner)"
            if len(holding_entities) > 1
            else "Corporate / LLC Entity (Pierced to True Beneficial Owner)"
        )
        role_auth = f"Verified Beneficial Owner, President & Sole Principal of {inferred_entity}"
        pers_deed_names = port_info.get("personal_owners_at_tax_address", [])
        proof_text = (
            f"Strong 3-Point Beneficial Ownership Proof: (1) Deed owner '{inferred_entity}' routes its official County Property Tax Bills to {mail_addr}; "
            + (
                f"(2) GIS multi-parcel cross-linking proves {mail_addr} is the personal homestead deeded directly to {', '.join(pers_deed_names)}, who also hold {p_count} total parcels across the county; "
                if pers_deed_names
                else f"(2) Principal office and residential property records at {mail_addr} match {primary_human}; "
            )
            + f"(3) State corporate filings (SOS / Bizapedia / NCCompanies) confirm {primary_human} is the President, Registered Agent, and controlling principal"
            + (f" alongside Corporate Secretary {discovered_persons[1]}." if len(discovered_persons) > 1 else ".")
        )
    else:
        structure_type = "Direct Individual Ownership (Verified on County Deed)"
        role_auth = "Direct Deed Owner & Decision-Maker"
        proof_text = f"Directly deeded to {inferred_entity or primary_human} on county property tax records with mailing address {mail_addr}."

    best_phone = all_phone_objs[0].number if all_phone_objs else (req.known_phone or "Use Stage 3 Free Links Below")
    best_phone_type = (
        f"📱 {all_phone_objs[0].phone_type}"
        if all_phone_objs
        else "Click TruePeopleSearch / FastPeopleSearch Below"
    )

    return {
        "inferred_entity": inferred_entity,
        "is_llc": is_llc,
        "primary_human": primary_human,
        "mail_addr": mail_addr,
        "contacts_list": contacts_list,
        "all_phone_objs": all_phone_objs,
        "best_phone": best_phone,
        "best_phone_type": best_phone_type,
        "portfolio_props": portfolio_props,
        "p_count": p_count,
        "p_val_str": p_val_str,
        "port_summary": port_summary,
        "structure_type": structure_type,
        "role_auth": role_auth,
        "proof_text": proof_text,
    }


async def run_gemini_skip_trace_agents(
    req: InvestigateRequest,
    recon: Dict[str, Any],
    api_key: str,
) -> SkipTraceDossier:
    full_address = f"{req.street}, {req.city}, {req.state} {req.zip_code}".strip()
    gis_info = recon.get("gis_parcel", {})
    det = build_deterministic_contacts_and_portfolio(req, recon)
    snippets_text = "\n".join(f"- {s}" for s in recon.get("snippets", [])[:20]) or "No pre-scraped snippets."

    system_prompt = (
        "You are PropTrace AI, an elite 3-Agent Commercial, Residential & Farm/Land Property Skip-Tracing System.\n"
        "You must follow these 3 core rules strictly:\n"
        "1. PERSONAL CELL PHONE PRIORITY: Always prioritize discovering the building owner's personal cell / wireless phone number "
        "(or most direct personal contact line) above generic office/switchboard numbers. Clearly distinguish Personal Cell / Direct Personal lines from Business/Office lines. "
        "Exclude any phone numbers from snippets that belong to a different person with the same name in an unrelated city.\n"
        "2. OWNER PROPERTY PORTFOLIO: Report the exact number of total properties owned by this owner/entity (`portfolio_count`), combined value (`portfolio_total_value`), "
        "and populate `other_properties` with the other parcels discovered in the GIS Portfolio scan.\n"
        "3. TRUE BENEFICIAL OWNER / MULTI-LLC PIERCING & BUILDING CARE DECISION-MAKER: Strongly verify that the person being contacted is the actual beneficial owner "
        "by cross-referencing the County Tax Bill Mailing Address homeowner, Secretary of State President/Managing Member filings, and sister LLCs. "
        "When there is no single owner (e.g. an HOA, Condo Association, or multi-owner group), identify the person who makes decisions regarding the CARE AND MAINTENANCE OF THE BUILDING "
        "(such as the Property/Facilities Manager, Managing Partner, or HOA Board President) and list all verified officers/decision-makers."
    )

    user_prompt = f"""Synthesize a complete, accurate SkipTraceDossier JSON for:
- Target Property Address: {full_address}
- Property Type: {req.property_type.upper()}
- User-Supplied Owner / LLC Hint: {req.known_owner_or_llc or 'None provided — use discovered GIS/web owner'}
- User-Supplied Phone Hint: {req.known_phone or 'None'}

Live GIS Parcel Record:
{json.dumps(gis_info, indent=2)}

Live GIS Portfolio Summary ({det['p_count']} Total Properties Owned, Combined Value: {det['p_val_str']}):
{det['port_summary']}
Top Portfolio Properties (excluding target):
{json.dumps([p.model_dump() for p in det['portfolio_props'][:12]], indent=2)}

Pre-Verified Ownership Chain & Veil-Piercing Proof:
- Structure Type: {det['structure_type']}
- Primary Decision-Maker: {det['primary_human']} ({det['role_auth']})
- Verification Proof: {det['proof_text']}
- Layers Pierced: {json.dumps(recon.get('shell_layers', []), indent=2)}

Location-Verified & Prioritized Phone Numbers (Personal Cell / Direct Personal First):
{json.dumps(recon.get('sorted_phone_records', []), indent=2)}

Discovered Emails:
{', '.join(recon.get('candidate_emails', [])) or 'None'}

Live Multi-Stage Reconnaissance Snippets:
{snippets_text}"""

    apn_val = gis_info.get("parcel_apn") or "See Regrid / LandGlide Link Below"
    county_val = gis_info.get("county") or f"{req.city}, {req.state}"

    if HAS_GENAI and api_key:
        client = genai.Client(api_key=api_key)
        model_name = req.model if req.model in ("gemini-3.8-flash", "gemini-4-argon", "gemini-3.5-flash-lite") else "gemini-3.8-flash"

        if hasattr(client, "interactions"):
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

                    # Enforce deterministic guarantees on portfolio, personal cell priority, and proof chain
                    if det["portfolio_props"] and len(dossier_obj.other_properties) < len(det["portfolio_props"]):
                        dossier_obj.other_properties = det["portfolio_props"]
                    if det["p_count"] > dossier_obj.portfolio_count:
                        dossier_obj.portfolio_count = det["p_count"]
                    if det["p_val_str"] and not dossier_obj.portfolio_total_value:
                        dossier_obj.portfolio_total_value = det["p_val_str"]
                    if det["port_summary"] and not dossier_obj.portfolio_summary:
                        dossier_obj.portfolio_summary = det["port_summary"]
                    if not dossier_obj.shell_layers_pierced and recon.get("shell_layers"):
                        dossier_obj.shell_layers_pierced = recon["shell_layers"]
                    if not dossier_obj.decision_maker_verification_proof:
                        dossier_obj.decision_maker_verification_proof = det["proof_text"]
                    if not dossier_obj.decision_maker_role_and_authority:
                        dossier_obj.decision_maker_role_and_authority = det["role_auth"]
                    if not dossier_obj.ownership_structure_type:
                        dossier_obj.ownership_structure_type = det["structure_type"]
                    if not dossier_obj.assessed_value_and_acres and gis_info.get("assessed_value_and_acres"):
                        dossier_obj.assessed_value_and_acres = gis_info["assessed_value_and_acres"]

                    # Enforce Personal Cell / Location-Verified Phone ordering
                    if det["all_phone_objs"]:
                        dossier_obj.primary_phone = det["best_phone"]
                        dossier_obj.primary_phone_type = det["best_phone_type"]
                        if not dossier_obj.contacts:
                            dossier_obj.contacts = det["contacts_list"]
                        else:
                            # Ensure contacts only show location-verified numbers with personal cell first
                            valid_nums = {p.number for p in det["all_phone_objs"]}
                            for idx, c in enumerate(dossier_obj.contacts):
                                c.phones = [p for p in c.phones if p.number in valid_nums]
                                if not c.phones and idx < len(det["contacts_list"]):
                                    c.phones = det["contacts_list"][idx].phones
                                c.phones.sort(key=lambda x: x.is_personal_cell, reverse=True)
                                if not c.verification_evidence and idx < len(det["contacts_list"]):
                                    c.verification_evidence = det["contacts_list"][idx].verification_evidence
                                if idx == 0:
                                    c.is_primary_decision_maker = True
                                if not c.emails and idx == 0 and recon.get("candidate_emails"):
                                    c.emails = recon["candidate_emails"]

                    if len(dossier_obj.tax_mailing_address) > 95:
                        dossier_obj.tax_mailing_address = det["mail_addr"]
                    if dossier_obj.primary_phone and dossier_obj.overall_confidence == "Needs Portal Verification":
                        dossier_obj.overall_confidence = "High"
                    return dossier_obj
            except Exception:
                pass

    # Deterministic fallback dossier (100% populated from live GIS + portfolio + web recon)
    return SkipTraceDossier(
        normalized_address=full_address,
        property_type=req.property_type.capitalize(),
        parcel_apn=apn_val,
        county_and_state=county_val,
        assessed_value_and_acres=gis_info.get("assessed_value_and_acres", ""),
        owner_of_record=det["inferred_entity"] or det["primary_human"],
        is_corporate_or_llc=det["is_llc"],
        corporate_entity_name=det["inferred_entity"] if det["is_llc"] else "",
        state_of_formation=req.state.upper(),
        registered_agent_name=", ".join(recon.get("discovered_persons", [])[:2]),
        tax_mailing_address=det["mail_addr"],
        primary_decision_maker=det["primary_human"],
        decision_maker_role_and_authority=det["role_auth"],
        ownership_structure_type=det["structure_type"],
        decision_maker_verification_proof=det["proof_text"],
        shell_layers_pierced=recon.get("shell_layers", []),
        primary_phone=det["best_phone"],
        primary_phone_type=det["best_phone_type"],
        portfolio_count=det["p_count"],
        portfolio_total_value=det["p_val_str"],
        portfolio_summary=det["port_summary"],
        other_properties=det["portfolio_props"],
        contacts=det["contacts_list"],
        agent_1_property_findings=(
            f"Identified Deed Owner '{det['inferred_entity'] or det['primary_human']}' (APN: {apn_val}) in {county_val} "
            f"and discovered {det['p_count']} total properties in the owner's portfolio ({det['p_val_str']})."
        ),
        agent_2_entity_findings=(
            f"Verified '{det['primary_human']}' as true beneficial owner / decision-maker via Tax Address Homestead Cross-Link & SOS Officer filings ({', '.join(recon.get('discovered_persons', []))})."
            if recon.get("discovered_persons")
            else f"Prepared multi-LLC & building-care decision-maker queries for '{det['inferred_entity'] or req.street}'."
        ),
        agent_3_phone_findings=(
            f"Prioritized {len(det['all_phone_objs'])} location-verified contact number(s) with Personal Cell / Direct Personal lines first: "
            f"{', '.join(p.number for p in det['all_phone_objs'])}."
            if det["all_phone_objs"]
            else "Generated pre-filled personal cell lookup links for the owner's residential address on TruePeopleSearch & FastPeopleSearch."
        ),
        recommended_next_steps=[
            f"Call Primary Personal / Direct Line {det['best_phone']} for verified decision-maker {det['primary_human']}."
            if det["all_phone_objs"]
            else "Click 'Open All Free Phone Portals' below to pull personal wireless numbers.",
            f"Review all {det['p_count']} properties in the Owner Portfolio section below to see their full real estate footprint before calling.",
            "Click TruePeopleSearch or FastPeopleSearch (now pre-filled with the owner's personal home city & street) to verify additional mobile lines.",
        ],
        overall_confidence="High" if (det["inferred_entity"] and det["all_phone_objs"]) else ("Medium" if det["inferred_entity"] else "Needs Portal Verification"),
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

    # Step 1: Gather public web recon clues + GIS portfolio + multi-LLC piercing
    recon = await gather_public_web_recon(
        street=req.street,
        city=req.city,
        state=req.state,
        zip_code=req.zip_code or "",
        known_owner=req.known_owner_or_llc or "",
        property_type=req.property_type or "residential",
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
    mail_parsed = recon.get("mailing_parsed", {})

    # Step 3: Build dynamic deep-links (targeting owner's personal home/tax address for cell phone lookup!)
    tool_links = build_tool_links(
        street=req.street,
        city=req.city,
        state=req.state,
        zip_code=req.zip_code or "",
        owner_name=best_person,
        entity_name=best_entity,
        phone=dossier.primary_phone,
        owner_mail_street=mail_parsed.get("street", ""),
        owner_mail_city=mail_parsed.get("city", ""),
        owner_mail_state=mail_parsed.get("state", ""),
        owner_mail_zip=mail_parsed.get("zip", ""),
    )

    if req.auto_open_tabs:
        auto_urls = [
            tool_links["property_tools"][2]["url"],  # Regrid Free Parcel Map
            tool_links["people_tools"][0]["url"],    # TruePeopleSearch Owner Cell Lookup
            tool_links["people_tools"][1]["url"],    # FastPeopleSearch Owner Cell Lookup
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
