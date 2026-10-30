# PropTrace AI — owner-first property research

PropTrace runs locally at http://127.0.0.1:8765 for residential, commercial and farm property research. A research result can remain unresolved. It does not establish a current legal owner, beneficial owner, repair authority or callable owner number without supporting records.

## Run

Use `Start-PropTraceAI.bat`, or from this development directory in PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

Restart the development server and refresh the browser after upgrading. Existing portable ZIPs are separate, older builds and have not been rebuilt. Run one server process per data folder; a process lock prevents competing queue workers. The server binds to localhost.

## Source coverage

| Source | Actual behavior |
| --- | --- |
| ArcGIS World Geocoder | Automated address suggestions and address-match validation nationwide. |
| NC OneMap county-contributed parcels | Automated point-intersection lookup only when the supplied state is NC. Street/city and any supplied APN must uniquely match. Raw record, query URL and retrieval timestamp are retained. Requires current county/deed review. |
| Other county assessor/GIS/deed sources | Manual research; additional authorized source adapters are needed. Unsupported states never use the NC map. |
| State business registries | Manual filing research. Registered agents, attorneys and officers are not automatically beneficial owners. |
| OpenCorporates, Bizapedia, CorporationWiki, B2BHint | Manual portals/search links; no record retrieval or API integration is claimed. |
| Buildout, LandGlide, Regrid, Land.com, employer tools | Manual portals; authenticated accounts/authorized APIs are not connected. |
| TruePeopleSearch, FastPeopleSearch, Spokeo and other phone portals | Manual authorized research. No automated people-search retrieval. Owner-name links for the primary tools are nationwide. |
| Gemini | Optional evidence-based verification suggestions using `client.aio.models.generate_content`, JSON MIME type and a Pydantic schema. Installed/tested SDK: 2.31.0. AI off is the default. AI output never changes factual dossier fields. Failures are visible. |

There is **no default state or city**. Single-address and batch location fields are independent. Choosing an address suggestion fills street/city/state/ZIP; manual typing or mapped spreadsheet fields also supplies location. Missing locations produce actionable errors, never Raleigh/NC fallbacks. Conservative address matching can require manual review for street aliases, rural parcels or suite-level ambiguity.

## Evidence and owner priority

The app separates official parcel records, corporate filings, people-search records, snippets and user hints. It preserves source URLs when available, UTC retrieval timestamps, raw parcel records, role relationships and phone/person associations. Conflicting owners require review. A phone candidate is retained regardless of its area code. Corroboration requires person/address evidence, an original record and independent underlying data lineages; number order does not raise confidence. No candidate is promoted to a primary owner phone automatically.

Owner contact research comes first. A manager or other secondary contact requires documented repair authority after owner research is exhausted. This build does not automate that authority investigation or provide a manual source-record ingestion editor. The launchpad and saved research notes support follow-up; corporate/phone adapters can implement the modular source interface in `research.py`. Provider-supplied records must carry real provenance; do not treat an adapter fixture as a live integration.

## Batch workflow

1. Upload `.xlsx`, `.xls`, `.csv` or a delimited text table, or paste tab-separated cells. Row 1 is always the header; only the first Excel worksheet is read. Word documents are rejected.
2. Review field mappings and the first 50 mapped rows. Full addresses containing suites and hyphenated house numbers are retained. Tax mailing fields remain hints, never the subject property address. Fallback city/state values apply only when explicitly entered in the batch dialog.
3. Submit the preview to the SQLite queue. Every nonempty source row is retained, including invalid rows and duplicate properties. Current limits are 10 MB uploads, 10,000 data rows and 128 columns per job.
4. Monitor row-level status. Closing the browser does not interrupt server work. Pause finishes the active row, then stops. Resume processes pending rows; retry requeues failed rows without rerunning successes. After a server restart, interrupted jobs are paused and the active row is returned to pending. Select the saved job and resume it.
5. Export every row to Excel, including failures, invalid inputs, duplicates, source evidence and original hints. A completed research attempt can require manual review; it does not mean a verified phone was found.

Queue concurrency is one active row with a 1.5-second interval; shared public-source requests are bounded to three. HTTP 429/503 Retry-After cooldowns suppress further requests to the throttled host; retry failed rows after service recovery. The cooldown is in-memory and resets on restart. Source calls and row work have timeouts.

Saved research uses deterministic property identifiers and atomic JSON writes. Retry replay returns an existing lead and preserves manual edits. It does not merge or delete older duplicate records. It also does not silently replace previously saved evidence for the same property; each batch attempt retains its own dossier in SQLite.

## Local data and privacy

Private files stay outside Git: `config.json`, `saved_leads.json`, `batch_jobs.sqlite3` and its WAL/SHM files, plus exports. Settings updates preserve unrelated keys. Invalid saved-lead JSON blocks saving instead of silently replacing data. Do not delete the SQLite WAL/SHM files while the app is running; stop the server before making a consistent backup. Lead JSON and settings are written through flushed temporary files and atomic replacement.

Address queries go to the geocoder and applicable parcel service. Enabling AI sends retrieved evidence to Google; portal links send the selected queries to external services. Keys are stored locally in plain-text configuration and are not returned in status responses or logged by application code. This is a single-user local app, not a multi-user deployment. Cross-origin browser writes and unexpected Host headers are rejected.

Existing saved leads are preserved as historical user data. Their old confidence claims are not retroactively reverified. Review their evidence before use.

## Verification

Run from this development directory:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --test tests/autocomplete.test.cjs
node --check static/app.js
.\.venv\Scripts\python.exe -m compileall -q app.py research.py jobs.py tests
```

The tests use synthetic properties, mocked source responses and temporary storage. They do not research private people. Browser verification uses an isolated server with synthetic source records. TestClient emits an upstream Starlette/httpx deprecation warning; the tests pass.

API format reference: https://googleapis.github.io/python-genai/
Parcel service metadata: https://services.nconemap.gov/secure/rest/services/NC1Map_Parcels/FeatureServer/1
