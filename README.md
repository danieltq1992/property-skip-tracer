# PropTrace AI 🏢🌾🏡

**PropTrace AI** is a Multi-Agent Commercial, Residential & Farm/Land Property Owner and Phone Skip-Tracing GUI built with FastAPI, Google Gemini (`google-genai` Interactions API), live GIS parcel layers, and one-click deep-link launchers.

## ✨ Features

- **3-Stage AI Skip-Tracing Pipeline:**
  - **Stage 1 — Property, Farm & Parcel Recon:** Queries County GIS / Statewide Parcel FeatureServers, **LandGlide**, **Land.com**, **Prospect by Buildout**, **Regrid**, **LoopNet**, and **Crexi** to extract the **Deed Owner of Record**, **Parcel APN**, **Assessed Value**, and **Tax Bill Mailing Address**.
  - **Stage 2 — Corporate & LLC Veil Piercing:** Pierces LLCs, Corporations, and Farm Trusts via **OpenCorporates**, **CorporationWiki**, **B2BHint**, **Bizapedia**, and **Secretary of State** filings to uncover the human **Managing Member / President / Registered Agent**.
  - **Stage 3 — People Search & Phone Skip-Tracing:** Extracts wireless, landline, and office phone numbers across public directories, **TruePeopleSearch**, **FastPeopleSearch**, **Spokeo**, **CyberBackgroundChecks**, and **SearchPeopleFree**.
- **Live US Street Address Autocomplete:** Type a street number and name to auto-populate Street, City, 2-letter State, ZIP Code, and County.
- **Interactive Deep-Link Launchpad:** Pre-fills all 18 property, corporate, and phone skip-tracing portals with the discovered owner name, LLC name, and address.
- **Batch CSV Lookup & CRM Export:** Run multiple properties in batch mode and export saved leads to CSV (ready for **Core365 | via** or dialers).

## 🚀 Quick Start

1. **Create & activate a virtual environment:**
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\pip install -r requirements.txt
   ```

2. **Start the server:**
   - Double-click `Start-PropTraceAI.bat`, **or** run:
     ```powershell
     .\.venv\Scripts\python.exe app.py
     ```
   - Open `http://127.0.0.1:8765` in your browser.

3. **Optional — Configure Gemini API Key:**
   - Click **⚙️ API Key & Settings** in the top-right corner of the GUI to save your `GEMINI_API_KEY` locally in `config.json` (ignored by Git via `.gitignore`).
