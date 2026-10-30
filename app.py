import asyncio
from contextlib import asynccontextmanager, suppress
import csv
import io
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# If someone double-clicks app.py directly in File Explorer using global Python,
# automatically re-launch inside the local .venv virtual environment if needed.
_BASE_DIR_INIT = Path(__file__).resolve().parent
_VENV_WIN = _BASE_DIR_INIT / ".venv" / "Scripts" / "python.exe"
_VENV_POSIX = _BASE_DIR_INIT / ".venv" / "bin" / "python"
try:
    import httpx
    import openpyxl
    from fastapi import FastAPI, File, Form, HTTPException, UploadFile
    from fastapi.responses import FileResponse, Response
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel, Field
except ImportError:
    _target_py = _VENV_WIN if _VENV_WIN.exists() else (_VENV_POSIX if _VENV_POSIX.exists() else None)
    if _target_py and Path(sys.executable).resolve() != _target_py.resolve():
        raise SystemExit(subprocess.call([str(_target_py), str(Path(__file__).resolve()), *sys.argv[1:]]))
    raise

try:
    import xlrd
    HAS_XLRD = True
except ImportError:
    HAS_XLRD = False

try:
    from google import genai
    HAS_GENAI = True
except ImportError:
    HAS_GENAI = False

from research import investigate, GeminiAdvisor, property_key, validation_error, public_get
from jobs import JobStore, WorkerLock, safe_cell
from evidence_store import EvidenceStore, ReviewedRecord
from rate_limits import RateLimitStore
import research

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
LEADS_FILE = BASE_DIR / "saved_leads.json"
CONFIG_FILE = BASE_DIR / "config.json"

JOB_FILE = BASE_DIR / "batch_jobs.sqlite3"
LEADS_LOCK = threading.RLock()


@asynccontextmanager
async def lifespan(app):
    lock = WorkerLock(BASE_DIR / "batch_worker.lock")
    lock.acquire()
    try:
        store = JobStore(JOB_FILE)
        store.recover()
        app.state.evidence = EvidenceStore(JOB_FILE)
        research.RATE_LIMIT_STORE = RateLimitStore(JOB_FILE)
    except Exception:
        lock.release()
        raise
    app.state.jobs = store

    async def worker():
        while True:
            try:
                await store.step(research_row, save_batch_result)
            except Exception:
                # Fail closed on persistence errors; do not expose private records in logs.
                app.state.worker_error = "Queue storage failed; restart after checking local disk integrity."
                return
            await asyncio.sleep(1.5)  # one active row, bounded request rate

    task = asyncio.create_task(worker())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        research.RATE_LIMIT_STORE = None
        lock.release()


app = FastAPI(
    title="PropTrace AI — Commercial, Residential & Farm Property Owner & Personal Cell Skip-Tracing Agent",
    version="2.0.0",
    lifespan=lifespan,
)

@app.middleware("http")
async def local_origin_guard(request, call_next):
    host = request.headers.get("host", "").split(":")[0].lower()
    if host not in ("127.0.0.1", "localhost", "testserver"):
        return Response("Local host required", status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
            return Response("Cross-origin request denied", status_code=403)
    return await call_next(request)


# Request models. Factual evidence is assembled independently in research.py.

class InvestigateRequest(BaseModel):
    street: str
    city: str
    state: str
    zip_code: Optional[str] = ""
    property_type: str = "residential"
    known_owner_or_llc: Optional[str] = ""
    known_phone: Optional[str] = ""
    model: str = "disabled"
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
    portfolio_count: int = 0
    portfolio_total_value: str = ""
    confidence: str = "Needs verification"
    notes: str = ""
    property_key: str = ""
    evidence: List[Dict[str, Any]] = Field(default_factory=list)
    dossier: Dict[str, Any] = Field(default_factory=dict)


class OpenTabsRequest(BaseModel):
    urls: List[str]


class ApiKeyRequest(BaseModel):
    gemini_api_key: str


class BatchParseTextRequest(BaseModel):
    raw_text: str
    default_city: str = ""
    default_state: str = ""
    default_zip: str = ""
    default_property_type: str = "residential"
    custom_roles: Optional[List[str]] = None


class BatchRemapRequest(BaseModel):
    header_row: List[str]
    data_rows: List[List[str]]
    column_roles: List[str]
    default_city: str = ""
    default_state: str = ""
    default_zip: str = ""
    default_property_type: str = "residential"


# ---------------------------------------------------------------------------
# Helper Functions: Config, Saved Leads & Spreadsheet Header-Row Parser
# ---------------------------------------------------------------------------

def excel_col_letter(col_idx: int) -> str:
    """Converts 0-based column index to Excel column letter (0 -> A, 25 -> Z, 26 -> AA)."""
    result = ""
    n = col_idx + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


def format_cell_value(val: Any) -> str:
    """Formats Excel/CSV cell values cleanly, preserving integer ZIP codes and house numbers."""
    if val is None:
        return ""
    if isinstance(val, bool):
        return "TRUE" if val else "FALSE"
    if isinstance(val, int):
        return str(val)
    if isinstance(val, float):
        if val.is_integer():
            return str(int(val))
        return str(val)
    return str(val).strip()


def extract_raw_rows_from_bytes(file_bytes: bytes, filename: str) -> Dict[str, Any]:
    """Reads an Excel (.xlsx, .xlsm, .xls) or CSV/TSV file and returns sheet names + raw rows."""
    fname_low = (filename or "").lower()
    if not fname_low.endswith((".xlsx", ".xls", ".csv", ".tsv", ".txt")):
        raise HTTPException(400, "Choose .xlsx, .xls, .csv or a tab-separated text table.")
    rows: List[List[str]] = []
    sheet_name = "Sheet1"

    if fname_low.endswith((".xlsx", ".xlsm", ".xltx", ".xltm")):
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True, read_only=True)
        sheet_name = wb.sheetnames[0] if wb.sheetnames else "Sheet1"
        ws = wb[sheet_name]
        for row_vals in ws.iter_rows(values_only=True):
            cleaned = [format_cell_value(c) for c in row_vals]
            while cleaned and cleaned[-1] == "":
                cleaned.pop()
            rows.append(cleaned)
        wb.close()
    elif fname_low.endswith(".xls"):
        if not HAS_XLRD:
            raise HTTPException(status_code=400, detail="xlrd package is required to read legacy .xls files.")
        wb = xlrd.open_workbook(file_contents=file_bytes)
        sheet = wb.sheet_by_index(0)
        sheet_name = sheet.name
        for r_idx in range(sheet.nrows):
            cleaned = [format_cell_value(sheet.cell_value(r_idx, c_idx)) for c_idx in range(sheet.ncols)]
            while cleaned and cleaned[-1] == "":
                cleaned.pop()
            rows.append(cleaned)
    else:
        # CSV, TSV, or plain text file
        text_content = ""
        for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
            try:
                text_content = file_bytes.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        return extract_raw_rows_from_text(text_content, sheet_name=filename or "CSV")

    return normalize_single_col_table(rows, sheet_name=sheet_name)


def extract_raw_rows_from_text(raw_text: str, sheet_name: str = "Pasted Table") -> Dict[str, Any]:
    """Parse header-first delimited tables while preserving quoted multiline cells."""
    if not (raw_text or "").strip():
        return {"sheet_name": sheet_name, "rows": []}
    first_line = raw_text.splitlines()[0]
    delimiter = next((d for d in ("\t", ",", ";", "|") if d in first_line), None)
    if delimiter:
        rows = [[cell.strip() for cell in row] for row in csv.reader(io.StringIO(raw_text), delimiter=delimiter)]
    else:
        rows = [[line.strip()] for line in raw_text.splitlines()]

    return normalize_single_col_table(rows, sheet_name=sheet_name)


def normalize_single_col_table(rows: List[List[str]], sheet_name: str = "Sheet1") -> Dict[str, Any]:
    """Pad cells without changing hyphenated street numbers or column semantics."""
    if not rows:
        return {"sheet_name": sheet_name, "rows": []}

    max_cols = max(len(r) for r in rows)

    if len(rows) > 10001 or max_cols > 128:
        raise HTTPException(400, "Use at most 10,000 data rows and 128 columns per batch.")
    if not any(rows[0]):
        raise HTTPException(400, "Row 1 must contain column headers.")
    # Pad all rows to max_cols so every row has the exact same number of columns as Row 1
    if len(rows[0]) < max_cols:
        for idx in range(len(rows[0]), max_cols):
            rows[0].append(f"Column {excel_col_letter(idx)}")
    padded_rows = [r + [""] * (max_cols - len(r)) for r in rows]
    return {"sheet_name": sheet_name, "rows": padded_rows}


def classify_single_header(raw_header: str, sample_val: str = "") -> str:
    """Determines what field a column represents based on its Row 1 header text."""
    h_clean = re.sub(r"^[-•*#]+\s*", "", (raw_header or "").strip())
    h_low = re.sub(r"[^a-z0-9]+", " ", h_clean.lower()).strip()
    h_nospace = h_low.replace(" ", "")

    # 1. Check Mailing / Tax Address columns first so they don't collide with Property Address/City/State/Zip
    if any(
        w in h_low
        for w in (
            "mailing address",
            "mail address",
            "mailadd",
            "tax address",
            "billing address",
            "owner address",
            "mailing street",
            "mail st",
        )
    ):
        return "mailing_address"
    if re.search(r"\b(mail|mailing|tax|billing|owner)\b", h_low) and re.search(r"\b(city|state|zip|postal)\b", h_low):
        return "notes"
    if h_nospace in ("mcity", "mailcity", "mailingcity", "mstate", "mailstate", "mzip", "mailzip"):
        return "notes"

    # 2. Street / Property Address
    if any(
        w in h_low
        for w in (
            "full address",
            "street address",
            "property address",
            "site address",
            "siteadd",
            "physical address",
            "situs address",
            "situs",
            "parcel address",
            "prop address",
            "building address",
            "location address",
            "street name",
            "street no",
            "street number",
            "house number",
        )
    ) or h_low in ("address", "street", "addr", "road", "location", "property", "site"):
        return "street"

    # 3. Property City
    if h_low in ("city", "town", "municipality", "scity", "site city", "property city", "situs city", "physical city") or (
        "city" in h_low and "mail" not in h_low
    ):
        return "city"

    # 4. Property State
    if h_low in ("state", "st", "province", "region", "sstate", "site state", "property state", "situs state") or (
        "state" in h_low and "mail" not in h_low and "estate" not in h_low
    ):
        return "state"

    # 5. Property ZIP Code
    if h_low in ("zip", "zip code", "zipcode", "postal", "postal code", "szip", "site zip", "property zip", "situs zip") or (
        ("zip" in h_low or "postal" in h_low) and "mail" not in h_low
    ):
        return "zip_code"

    # 6. Deed Owner / LLC / Entity Name
    if any(
        w in h_low
        for w in (
            "owner",
            "ownname",
            "deed",
            "llc",
            "company",
            "business",
            "entity",
            "corporation",
            "corp",
            "taxpayer",
            "landowner",
            "farm name",
            "ranch name",
            "grantor",
            "grantee",
            "trust",
            "contact name",
            "full name",
            "client",
            "customer",
        )
    ) or h_low in ("name", "principal"):
        return "known_owner_or_llc"

    # 7. Phone / Cell Number
    if any(w in h_low for w in ("phone", "cell", "mobile", "wireless", "telephone", "tel", "contact number")):
        return "known_phone"

    # 8. Property Type / Land Use
    if any(
        w in h_low
        for w in (
            "property type",
            "prop type",
            "land use",
            "building type",
            "asset type",
            "property class",
            "zoning",
            "category",
        )
    ) or h_low in ("type", "use", "class"):
        return "property_type"

    # 9. Parcel APN / PIN / Tax ID
    if any(w in h_low for w in ("apn", "parcel", "parno", "pin", "tax id", "pid", "account number")):
        return "parcel_apn"

    # Fallback: check if the sample value in Row 2 looks strongly like a street address and no header matched
    if sample_val and re.match(r"^\d+\s+[A-Za-z0-9]", sample_val.strip()) and h_low in ("col a", "column a", "field1", ""):
        return "street"

    return "notes"


def classify_top_row_headers(header_row: List[str], data_rows: List[List[str]]) -> List[Dict[str, Any]]:
    """Reads the very top row (Row 1) in its exact column order and determines what each column represents."""
    sample_row = data_rows[0] if data_rows else []
    columns_meta: List[Dict[str, Any]] = []

    for col_idx, raw_hdr in enumerate(header_row):
        sample_val = sample_row[col_idx] if col_idx < len(sample_row) else ""
        role = classify_single_header(raw_hdr, sample_val=sample_val)
        columns_meta.append(
            {
                "col_index": col_idx,
                "col_letter": excel_col_letter(col_idx),
                "header_name": raw_hdr.strip() or f"Column {excel_col_letter(col_idx)}",
                "mapped_to": role,
                "sample_value": sample_val,
            }
        )

    return columns_meta


US_STATE_TO_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO",
    "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH",
    "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC", "washington dc": "DC",
}


def normalize_state_abbr(raw_state: str, default_state: str = "") -> str:
    s = (raw_state or "").strip()
    if not s:
        s = (default_state or "").strip()
    s_low = re.sub(r"[^a-z\s]", "", s.lower()).strip()
    if s_low in US_STATE_TO_ABBR:
        return US_STATE_TO_ABBR[s_low]
    return s.upper()


def normalize_property_type_value(raw_val: str, default_type: str = "residential") -> str:
    low = (raw_val or "").strip().lower()
    if not low:
        return default_type
    if any(k in low for k in ("comm", "retail", "office", "ind", "warehouse", "store", "shopping", "plaza", "hotel", "motel")):
        return "commercial"
    if any(k in low for k in ("farm", "ag", "ranch", "land", "timber", "acre", "poultry", "crop", "rural", "vacant", "pasture")):
        return "farm"
    if any(k in low for k in ("res", "single", "sfr", "home", "house", "condo", "townhouse", "apt", "multi", "duplex")):
        return "residential"
    return default_type


def build_mapped_rows_from_table(
    header_row: List[str],
    data_rows: List[List[str]],
    column_roles: List[str],
    default_city: str = "",
    default_state: str = "",
    default_zip: str = "",
    default_property_type: str = "residential",
) -> List[Dict[str, Any]]:
    """Maps every data row (Row 2+) strictly according to the column order and roles established by Row 1."""
    mapped_records: List[Dict[str, Any]] = []

    for offset, row_cells in enumerate(data_rows):
        excel_row_num = offset + 2  # Row 1 is the header row
        if not any((c or "").strip() for c in row_cells):
            continue

        street_parts: List[str] = []
        city_val = ""
        state_val = ""
        zip_val = ""
        owner_parts: List[str] = []
        phone_val = ""
        prop_type_raw = ""
        mailing_val = ""
        apn_val = ""
        extra_notes: List[str] = []
        row_by_header: Dict[str, str] = {}

        for col_idx, role in enumerate(column_roles):
            hdr_label = (
                header_row[col_idx].strip()
                if col_idx < len(header_row) and header_row[col_idx].strip()
                else f"Column {excel_col_letter(col_idx)}"
            )
            cell_val = row_cells[col_idx].strip() if col_idx < len(row_cells) and row_cells[col_idx] else ""
            row_by_header[hdr_label] = cell_val
            if not cell_val or role == "ignore":
                continue

            if role == "street":
                street_parts.append(re.sub(r"^[-•*]+\s*", "", cell_val).strip())
            elif role == "city" and not city_val:
                city_val = cell_val
            elif role == "state" and not state_val:
                state_val = cell_val
            elif role == "zip_code" and not zip_val:
                # Keep 5-digit ZIP
                m_z = re.search(r"\b(\d{5})\b", cell_val)
                zip_val = m_z.group(1) if m_z else cell_val
            elif role == "known_owner_or_llc":
                owner_parts.append(cell_val)
            elif role == "known_phone" and not phone_val:
                phone_val = cell_val
            elif role == "property_type" and not prop_type_raw:
                prop_type_raw = cell_val
            elif role == "mailing_address" and not mailing_val:
                mailing_val = cell_val
            elif role == "parcel_apn" and not apn_val:
                apn_val = cell_val
            else:
                extra_notes.append(f"{hdr_label}: {cell_val}")

        street_combined = " ".join(street_parts).strip()

        # If the Street cell contains a full combined address like "123 Main St, Beulaville, NC 28518"
        # and City/State were not in separate columns, split them out cleanly
        if street_combined and ("," in street_combined or re.search(r"\b[A-Z]{2}\s+\d{5}\b", street_combined)):
            m_full = re.match(
                r"^(.+),\s*([^,]+?)(?:,\s*|\s+)([A-Za-z]{2})(?:\s+(\d{5}(?:-\d{4})?))?$",
                street_combined.strip(),
            )
            if m_full:
                street_combined = m_full.group(1).strip()
                if not city_val:
                    city_val = m_full.group(2).strip()
                if not state_val:
                    state_val = m_full.group(3).strip().upper()
                if not zip_val and m_full.group(4):
                    zip_val = m_full.group(4).strip()

        final_city = city_val or default_city.strip()
        final_state = normalize_state_abbr(state_val, default_state)
        final_zip = zip_val or default_zip.strip()
        final_owner = " / ".join(owner_parts).strip()

        final_type = normalize_property_type_value(prop_type_raw, default_type=default_property_type)

        if apn_val:
            extra_notes.insert(0, f"Spreadsheet APN: {apn_val}")
        if mailing_val:
            extra_notes.insert(0, f"Spreadsheet Mailing Addr: {mailing_val}")


        mapped_records.append(
            {
                "row_number": excel_row_num,
                "validation_error": validation_error(dict(street=street_combined, city=final_city, state=final_state)),
                "street": street_combined,
                "city": final_city,
                "state": final_state,
                "zip_code": final_zip,
                "property_type": final_type,
                "known_owner_or_llc": final_owner,
                "known_phone": phone_val,
                "mailing_address_hint": mailing_val,
                "parcel_apn_hint": apn_val,
                "notes": " | ".join(extra_notes),
                "row_by_header": row_by_header,
            }
        )

    return mapped_records

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


def atomic_json(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


def save_api_key(key: str) -> None:
    with LEADS_LOCK:
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
        config["gemini_api_key"] = key.strip()
        atomic_json(CONFIG_FILE, config)
    os.environ["GEMINI_API_KEY"] = key.strip()


def load_saved_leads():
    if not LEADS_FILE.exists():
        return []
    data = json.loads(LEADS_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Saved leads file must contain a list; restore it before saving.")
    return data


def write_saved_leads(leads):
    atomic_json(LEADS_FILE, leads)


def persist_lead(payload, key):
    import hashlib
    with LEADS_LOCK:
        leads = load_saved_leads()
        for lead in leads:
            if lead.get("property_key") == key or street_key_address(lead.get("address", "")) == street_key_address(payload["address"]):
                return lead  # preserve prior manual edits on retries
        lead = dict(payload, id="lead_" + hashlib.sha256(key.encode()).hexdigest()[:24],
                    property_key=key, created_at=datetime.now().isoformat())
        leads.insert(0, lead)
        write_saved_leads(leads)
        return lead


def street_key_address(address):
    return re.sub(r"[^a-z0-9-]", "", address.casefold())


def provider_config():
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
    providers = config.get("providers", {})
    for name, env in (("regrid", "REGRID_API_TOKEN"), ("opencorporates", "OPENCORPORATES_API_TOKEN")):
        item = dict(providers.get(name, {}))
        item["token"] = os.environ.get(env) or item.get("token", "")
        providers[name] = item
    return providers


async def research_row(row, advisor=None):
    records = app.state.evidence.records(row)
    sources = research.PublicSources(provider_config(), records)
    dossier = await investigate(row, sources=sources, advisor=advisor)
    app.state.evidence.snapshot(row, dossier)
    return dossier


def save_batch_result(dossier, row):
    persist_lead(dict(address=dossier["normalized_address"], property_type=dossier["property_type"],
        owner_of_record=dossier["owner_of_record"], decision_maker=dossier["primary_decision_maker"],
        primary_phone=dossier["primary_phone"], confidence=dossier["overall_confidence"],
        notes=row.get("notes", ""), evidence=dossier["evidence"], dossier=dossier), property_key(row))


def slugify(text: str) -> str:
    cleaned = re.sub(r"[^\w\s-]", "", (text or "").strip())
    return re.sub(r"[\s_]+", "-", cleaned).lower()


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
            "description": "Open official state filing research. Officer and registered-agent roles do not establish beneficial ownership.",
            "url": f"https://www.google.com/search?q={urllib.parse.quote('\"' + (target_entity or street) + '\" ' + state + ' Secretary of State business entity filings')}",
            "copy_text": target_entity or full_address,
        },
    ]

    # Stage 3: Personal Cell Phone Skip-Tracing (Targets Owner Name + Personal/Tax Residence!)
    tps_person_url = (
        f"https://www.truepeoplesearch.com/results?name={enc_person}"
        if target_person
        else f"https://www.truepeoplesearch.com/resultaddress?streetaddress={enc_p_street}&citystatezip={enc_p_csz}"
    )
    fps_person_url = (
        f"https://www.fastpeoplesearch.com/name/{person_slug}"
        if target_person
        else f"https://www.fastpeoplesearch.com/address/{fps_addr_slug}"
    )
    spokeo_url = (
        f"https://www.spokeo.com/{person_slug.title()}"
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
            "description": f"100% Free Personal Wireless / Cell phone finder for {target_person or 'owner'} nationwide; verify identity independently",
            "url": tps_person_url,
            "copy_text": target_person or p_full_addr,
        },
        {
            "id": "fps_name",
            "name": f"FastPeopleSearch ({target_person or 'Owner Personal Cell'})",
            "source_type": "screenshot",
            "badge": "Bookmark #2 • Cell Priority",
            "stage": "Stage 3: Phone Skip-Trace",
            "description": f"Free personal cell/mobile lookup for {target_person or 'discovered owner'} nationwide; verify identity independently",
            "url": fps_person_url,
            "copy_text": target_person or p_full_addr,
        },
        {
            "id": "tps_address",
            "name": f"TruePeopleSearch (Address clue: {p_street})",
            "source_type": "screenshot",
            "badge": "Bookmark #3",
            "stage": "Stage 3: Phone Skip-Trace",
            "description": f"100% Free reverse address lookup at subject/mailing address clue ({p_full_addr})",
            "url": f"https://www.truepeoplesearch.com/resultaddress?streetaddress={enc_p_street}&citystatezip={enc_p_csz}",
            "copy_text": p_full_addr,
        },
        {
            "id": "fps_address",
            "name": f"FastPeopleSearch (Address clue: {p_street})",
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

@app.get("/api/address-suggest")
async def suggest_addresses(q: str = "", city: str = "", state: str = ""):
    """Live US street address autocomplete returning street, city, 2-letter state, ZIP, and county."""
    query_str = q.strip()
    if len(query_str) < 2:
        return {"suggestions": []}

    scoped_line = query_str
    if city.strip() and city.strip().lower() not in query_str.lower():
        scoped_line += f", {city.strip()}"
    if state.strip() and state.strip().lower() not in query_str.lower():
        scoped_line += f", {state.strip()}"

    suggestions: List[Dict[str, str]] = []
    seen_keys = set()

    def add_suggestion(st_addr: str, c_city: str, c_state: str, c_zip: str, c_county: str = "") -> None:
        st_addr = st_addr.strip()
        c_city = c_city.strip()
        c_state = c_state.strip().upper()[:2]
        c_zip = c_zip.strip()[:5]
        if not st_addr or not c_city or not c_state:
            return
        dedup_key = f"{st_addr.lower()}|{c_city.lower()}|{c_state.lower()}|{c_zip}"
        if dedup_key in seen_keys:
            return
        seen_keys.add(dedup_key)
        label = f"{st_addr}, {c_city}, {c_state} {c_zip}".strip()
        suggestions.append(
            {
                "label": label,
                "street": st_addr,
                "city": c_city,
                "state": c_state,
                "zip_code": c_zip,
                "county": c_county.strip(),
            }
        )

    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            async def fetch_suggest_list(text_q: str) -> List[Dict[str, Any]]:
                r = await public_get(client,
                    "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/suggest",
                    params={
                        "text": text_q,
                        "countryCode": "USA",
                        "category": "Address,Street Address",
                        "maxSuggestions": 8,
                        "f": "json",
                    },
                )
                return r.json().get("suggestions", []) if r.status_code == 200 else []

            # Run both scoped (with current City/State if any) and nationwide query in parallel
            # so leftover City/State values from a prior search never block new addresses!
            if scoped_line != query_str:
                scoped_items, nationwide_items = await asyncio.gather(
                    fetch_suggest_list(scoped_line),
                    fetch_suggest_list(query_str),
                    return_exceptions=True,
                )
                sug_items = (scoped_items if isinstance(scoped_items, list) else []) + (
                    nationwide_items if isinstance(nationwide_items, list) else []
                )
            else:
                sug_items = await fetch_suggest_list(query_str)

            # Fast path: ArcGIS /suggest text is formatted as "309 Merchant St, Emporia, KS, 66801, USA"
            unresolved_items: List[Dict[str, Any]] = []
            for item in sug_items:
                txt = (item.get("text") or "").strip()
                parts = [p.strip() for p in txt.split(",")]
                if len(parts) >= 4 and len(parts[2]) == 2 and re.match(r"^\d{5}", parts[3]):
                    add_suggestion(parts[0], parts[1], parts[2], parts[3][:5], "")
                elif len(parts) >= 3 and len(parts[2][:2].strip()) == 2:
                    unresolved_items.append(item)

            # Fallback resolution if < 6 parsed directly from suggest text
            if len(suggestions) < 4 and (unresolved_items or not sug_items):
                async def resolve_candidate(text_val: str, magic_key: str = ""):
                    params = {
                        "SingleLine": text_val,
                        "countryCode": "USA",
                        "outFields": "StAddr,City,RegionAbbr,Postal,Subregion,PlaceName",
                        "maxLocations": 4,
                        "f": "json",
                    }
                    if magic_key:
                        params["magicKey"] = magic_key
                        params["maxLocations"] = 1
                    r = await public_get(client,
                        "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates",
                        params=params,
                    )
                    return r.json().get("candidates", []) if r.status_code == 200 else []

                if unresolved_items:
                    batches = await asyncio.gather(
                        *[resolve_candidate(it.get("text", ""), it.get("magicKey", "")) for it in unresolved_items[:4]],
                        return_exceptions=True,
                    )
                    cands = [c for b in batches if isinstance(b, list) for c in b]
                else:
                    cands = await resolve_candidate(query_str)

                for cand in cands:
                    attrs = cand.get("attributes", {})
                    st_addr = (attrs.get("StAddr") or "").strip()
                    c_city = (attrs.get("City") or "").strip()
                    c_state = (attrs.get("RegionAbbr") or "").strip().upper()
                    c_zip = (attrs.get("Postal") or "").strip()[:5]
                    c_county = (attrs.get("Subregion") or "").strip()
                    if not st_addr and cand.get("address"):
                        p_list = [p.strip() for p in cand["address"].split(",")]
                        if p_list:
                            st_addr = p_list[0]
                            if len(p_list) > 1 and not c_city:
                                c_city = p_list[1]
                    add_suggestion(st_addr, c_city, c_state, c_zip, c_county)
        except Exception:
            pass

    return {"suggestions": suggestions[:8]}


@app.get("/api/status")
async def get_status():
    key = get_saved_api_key()
    return {
        "has_api_key": bool(key),
        "masked_key": "Configured" if key else "",
        "has_genai_sdk": HAS_GENAI,
        "default_model": "disabled",
    }


class ProviderSetting(BaseModel):
    enabled: bool = False
    token: str = Field(default="", max_length=1000)


class ProviderSettings(BaseModel):
    regrid: ProviderSetting
    opencorporates: ProviderSetting


@app.get("/api/settings/sources")
async def get_source_settings():
    config = provider_config()
    return {name: {"enabled": bool(config.get(name, {}).get("enabled")),
        "configured": bool(config.get(name, {}).get("token"))} for name in ("regrid","opencorporates")}


@app.post("/api/settings/sources")
async def save_source_settings(req: ProviderSettings):
    with LEADS_LOCK:
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
        providers = config.setdefault("providers", {})
        for name, value in req.model_dump().items():
            previous = providers.setdefault(name, {})
            previous["enabled"] = value["enabled"]
            if value["token"].strip():
                previous["token"] = value["token"].strip()
        atomic_json(CONFIG_FILE, config)
    return {"status": "saved"}


class SubjectRequest(BaseModel):
    street: str
    city: str
    state: str
    zip_code: str = ""
    property_type: str = "residential"

    def row(self):
        row = self.model_dump()
        row["state"] = normalize_state_abbr(row["state"])
        error = validation_error(row)
        if error:
            raise HTTPException(400, error)
        return row


class EvidenceRequest(BaseModel):
    subject: SubjectRequest
    record: ReviewedRecord


def recompute_reviewed(row):
    prior = app.state.evidence.latest(row) or {}
    prior_records = [r for r in prior.get("evidence", []) if r.get("ingestion_method") not in ("manual_research","browser_assisted")]
    dossier = research.assemble(row, prior_records + app.state.evidence.records(row))
    dossier["ai"] = {"status": "disabled", "hypotheses": []}
    app.state.evidence.snapshot(row, dossier)
    return dossier


@app.post("/api/research/evidence")
async def add_research_evidence(req: EvidenceRequest):
    row = req.subject.row()
    app.state.evidence.add(row, req.record.record())
    return {"dossier": recompute_reviewed(row)}


@app.post("/api/research/context")
async def research_context(req: SubjectRequest):
    row = req.row()
    dossier = app.state.evidence.latest(row)
    return {"subject":row, "dossier":dossier, "instructions":
        "Inspect permitted source pages. Record URLs, dates, original wording and person relationships. "
        "Submit /api/research/evidence with review_method=browser_assisted. Never claim a human reviewed it. "
        "Stop at login or access restrictions that require user action. AI text is not a source record.",
        "record_schema":ReviewedRecord.model_json_schema()}


@app.post("/api/research/evidence/{record_id}/retract")
async def retract_research_evidence(record_id: str, req: SubjectRequest):
    row = req.row()
    if not app.state.evidence.retract(row, record_id):
        raise HTTPException(404, "Active record not found for this property")
    return {"dossier": recompute_reviewed(row)}


@app.post("/api/research/save")
async def save_reviewed_research(req: SubjectRequest):
    row = req.row()
    dossier = app.state.evidence.latest(row)
    if not dossier:
        raise HTTPException(404, "No research found for this property")
    # Explicit update of derived research fields; handwritten notes stay intact.
    with LEADS_LOCK:
        leads = load_saved_leads()
        for lead in leads:
            if lead.get("property_key") == property_key(row) or street_key_address(lead.get("address", "")) == street_key_address(dossier["normalized_address"]):
                lead.update(owner_of_record=dossier["owner_of_record"],decision_maker=dossier["primary_decision_maker"],
                    primary_phone=dossier["primary_phone"],confidence=dossier["overall_confidence"],evidence=dossier["evidence"],dossier=dossier,
                    updated_at=datetime.now().isoformat())
                write_saved_leads(leads)
                return {"status":"updated", "lead":lead}
        save_batch_result(dossier, row)
    return {"status":"saved"}


@app.post("/api/settings/apikey")
async def update_api_key(req: ApiKeyRequest):
    save_api_key(req.gemini_api_key)
    return {"status": "ok", "has_api_key": bool(req.gemini_api_key.strip())}


@app.post("/api/investigate")
async def investigate_property(req: InvestigateRequest):
    mapped = build_mapped_rows_from_table(
        ["Street", "City", "State", "ZIP"],
        [[req.street, req.city, req.state, req.zip_code or ""]],
        ["street", "city", "state", "zip_code"],
    )
    if not mapped:
        raise HTTPException(400, "Enter the subject property address.")
    row = mapped[0] | {"property_type": req.property_type,
        "known_owner_or_llc": req.known_owner_or_llc, "known_phone": req.known_phone}
    error = validation_error(row)
    if error:
        raise HTTPException(400, error)
    key = req.gemini_api_key or get_saved_api_key()
    advisor = GeminiAdvisor(key, req.model) if key and req.model != "disabled" else None
    dossier = await research_row(row, advisor=advisor)
    if req.model != "disabled" and not key:
        dossier["ai"] = {"status": "unavailable", "hypotheses": [], "error": "AI was selected but no API key is configured."}
    links = build_tool_links(street=row["street"], city=row["city"], state=row["state"], zip_code=row["zip_code"],
        owner_name=dossier["primary_decision_maker"], entity_name=dossier["corporate_entity_name"], phone="")
    for group in links.values():
        for link in group:
            link["access_mode"] = "manual_portal"
            link["badge"] = "Manual portal — not automatically searched"
    if req.auto_open_tabs:
        for link in links["property_tools"][:2]:
            webbrowser.open_new_tab(link["url"])
    return {"dossier": dossier, "tool_links": links, "recon_snippets": []}


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
    payload = req.model_dump()
    key = req.property_key or street_key_address(req.address)
    lead = persist_lead(payload, key)
    return {"status": "ok", "lead": lead, "total": len(load_saved_leads())}


@app.delete("/api/leads/{lead_id}")
async def delete_lead(lead_id: str):
    with LEADS_LOCK:
        leads = [item for item in load_saved_leads() if item.get("id") != lead_id]
        write_saved_leads(leads)
    return {"status": "ok", "total": len(leads)}


@app.get("/api/leads/export-xlsx")
async def export_leads_xlsx():
    """Exports all saved leads as a formatted Microsoft Excel (.xlsx) workbook."""
    leads = load_saved_leads()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "PropTrace AI Leads"

    headers = [
        "Timestamp",
        "Property Address",
        "Type",
        "Deed Owner / LLC",
        "Verified Decision Maker",
        "Primary Personal Cell / Phone",
        "Phone Type",
        "All Discovered Phones",
        "Emails",
        "Portfolio Properties Count",
        "Portfolio Value & Details",
        "Tax / Home Mailing Address",
        "Notes / Extra Columns",
        "Confidence",
    ]
    ws.append(headers)

    from openpyxl.styles import Alignment, Font, PatternFill

    header_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    for col_num in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_num)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="left", vertical="center")

    for l in leads:
        ws.append(
            [
                l.get("created_at") or "",
                l.get("address") or "",
                l.get("property_type") or "",
                l.get("owner_of_record") or "",
                l.get("decision_maker") or "",
                l.get("primary_phone") or "",
                l.get("primary_phone_type") or "Personal Cell / Direct",
                " | ".join(l.get("all_phones") or []),
                " | ".join(l.get("emails") or []),
                l.get("portfolio_count", 0),
                l.get("portfolio_total_value") or "",
                l.get("mailing_address") or "",
                l.get("notes") or "",
                l.get("confidence") or "",
            ]
        )

    for cells in ws.iter_rows(min_row=2):
        for cell in cells:
            if isinstance(cell.value, str):
                cell.value = safe_cell(cell.value)
    ws.freeze_panes = "A2"
    for col in ws.columns:
        max_len = max((len(str(cell.value or "")) for cell in col), default=12)
        col_letter = col[0].column_letter
        ws.column_dimensions[col_letter].width = min(max(max_len + 3, 14), 48)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"proptrace_leads_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/batch/parse-spreadsheet")
async def parse_batch_spreadsheet(
    file: UploadFile = File(...),
    default_city: str = Form(""),
    default_state: str = Form(""),
    default_zip: str = Form(""),
    default_property_type: str = Form("residential"),
):
    """Reads an uploaded Microsoft Excel (.xlsx, .xlsm, .xls) or CSV/TSV file,
    inspects the very top row (Row 1) in its exact column order to determine what each column represents,
    and maps every subsequent row (Row 2+) to those columns."""
    raw_bytes = await file.read(10 * 1024 * 1024 + 1)
    if len(raw_bytes) > 10 * 1024 * 1024:
        raise HTTPException(413, "Spreadsheet must be 10 MB or smaller.")
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    try:
        parsed = extract_raw_rows_from_bytes(raw_bytes, file.filename or "spreadsheet.xlsx")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "Cannot read spreadsheet. Check that it is a valid, unencrypted Excel or CSV file.") from None
    all_rows = parsed["rows"]
    if not all_rows:
        raise HTTPException(status_code=400, detail="No rows found in spreadsheet.")

    # Row 1 (index 0) is the top header row defining the columns and their order
    header_row = all_rows[0]
    data_rows = all_rows[1:]
    columns_meta = classify_top_row_headers(header_row, data_rows)
    column_roles = [c["mapped_to"] for c in columns_meta]

    mapped_rows = build_mapped_rows_from_table(
        header_row=header_row,
        data_rows=data_rows,
        column_roles=column_roles,
        default_city=default_city,
        default_state=default_state,
        default_zip=default_zip,
        default_property_type=default_property_type,
    )

    return {
        "filename": file.filename or "spreadsheet.xlsx",
        "sheet_name": parsed["sheet_name"],
        "header_row": header_row,
        "columns": columns_meta,
        "data_rows": data_rows,
        "mapped_rows": mapped_rows,
        "total_rows": len(mapped_rows),
    }


@app.post("/api/batch/parse-text")
async def parse_batch_text(req: BatchParseTextRequest):
    """Parses pasted Excel/CSV/tabular text. Uses Row 1 as the header row if it contains column titles,
    or synthesizes default column headers if the user pasted raw data lines without headers."""
    parsed = extract_raw_rows_from_text(req.raw_text, sheet_name="Pasted List")
    all_rows = parsed["rows"]
    if not all_rows:
        return {
            "filename": "Pasted List",
            "sheet_name": "Pasted List",
            "header_row": [],
            "columns": [],
            "data_rows": [],
            "mapped_rows": [],
            "total_rows": 0,
        }

    header_row, data_rows = all_rows[0], all_rows[1:]

    columns_meta = classify_top_row_headers(header_row, data_rows)
    if req.custom_roles and len(req.custom_roles) == len(columns_meta):
        for idx, r_val in enumerate(req.custom_roles):
            columns_meta[idx]["mapped_to"] = r_val
    column_roles = [c["mapped_to"] for c in columns_meta]

    mapped_rows = build_mapped_rows_from_table(
        header_row=header_row,
        data_rows=data_rows,
        column_roles=column_roles,
        default_city=req.default_city,
        default_state=req.default_state,
        default_zip=req.default_zip,
        default_property_type=req.default_property_type,
    )

    return {
        "filename": "Pasted Table",
        "sheet_name": "Pasted Table",
        "header_row": header_row,
        "columns": columns_meta,
        "data_rows": data_rows,
        "mapped_rows": mapped_rows,
        "total_rows": len(mapped_rows),
    }


@app.post("/api/batch/remap")
async def remap_batch_columns(req: BatchRemapRequest):
    """Re-maps loaded spreadsheet rows when the user changes a column role dropdown or default City/State."""
    mapped_rows = build_mapped_rows_from_table(
        header_row=req.header_row,
        data_rows=req.data_rows,
        column_roles=req.column_roles,
        default_city=req.default_city,
        default_state=req.default_state,
        default_zip=req.default_zip,
        default_property_type=req.default_property_type,
    )
    return {
        "mapped_rows": mapped_rows,
        "total_rows": len(mapped_rows),
    }


class BatchJobRequest(BaseModel):
    rows: List[Dict[str, Any]] = Field(min_length=1, max_length=10000)


@app.get("/api/batch/jobs")
async def list_batch_jobs():
    return {"jobs": app.state.jobs.list(), "worker_error": getattr(app.state, "worker_error", "")}


@app.post("/api/batch/jobs")
async def create_batch_job(req: BatchJobRequest):
    # Never persist credentials supplied with an arbitrary row payload.
    allowed = {"row_number", "street", "city", "state", "zip_code", "property_type", "known_owner_or_llc",
               "known_phone", "mailing_address_hint", "parcel_apn_hint", "notes"}
    rows = [{k: str(v or "") for k, v in row.items() if k in allowed} for row in req.rows]
    return {"id": app.state.jobs.create(rows)}


@app.get("/api/batch/jobs/{job_id}")
async def batch_job_status(job_id: str):
    try:
        return app.state.jobs.get(job_id)
    except KeyError:
        raise HTTPException(404, "Batch job not found")


@app.post("/api/batch/jobs/{job_id}/{action}")
async def batch_job_action(job_id: str, action: str):
    try:
        app.state.jobs.action(job_id, action)
        return {"status": "ok"}
    except KeyError:
        raise HTTPException(404, "Batch job not found")
    except ValueError:
        raise HTTPException(400, "Use cancel, resume, or retry")


@app.get("/api/batch/jobs/{job_id}/export")
async def export_batch_job(job_id: str):
    try:
        content = app.state.jobs.export(job_id)
    except KeyError:
        raise HTTPException(404, "Batch job not found")
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="batch-results.xlsx"'})


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def serve_index():
    return FileResponse(STATIC_DIR / "index.html")


def _is_port_open(host: str = "127.0.0.1", port: int = 8765) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


def _open_browser_when_ready(url: str = "http://127.0.0.1:8765") -> None:
    for _ in range(40):
        if _is_port_open("127.0.0.1", 8765):
            time.sleep(0.2)
            webbrowser.open(url)
            return
        time.sleep(0.25)


if __name__ == "__main__":
    import uvicorn

    url = "http://127.0.0.1:8765"
    no_browser = "--no-browser" in sys.argv or os.environ.get("PROPTRACE_NO_BROWSER") == "1"

    if _is_port_open("127.0.0.1", 8765):
        print(f"PropTrace AI server is already running at {url} - opening in your browser now!")
        if not no_browser:
            webbrowser.open(url)
        raise SystemExit(0)

    if not no_browser:
        threading.Thread(target=_open_browser_when_ready, args=(url,), daemon=True).start()

    print(f"Starting PropTrace AI Server at {url} ...")
    uvicorn.run("app:app", host="127.0.0.1", port=8765, reload=False)

