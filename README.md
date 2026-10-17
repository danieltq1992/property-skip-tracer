# 🏢 PropTrace AI — Standalone Property Owner, Portfolio & Personal Cell Skip-Tracer

**PropTrace AI** is a standalone, local-first Multi-Agent Property Owner & Personal Cell Phone Skip-Tracing application for **Residential**, **Commercial**, and **Farm / Land** properties.

---

## 🔒 100% Standalone & Private by Design (Zero Shared Data)

Every person who downloads PropTrace AI runs it **independently on their own computer or server** (`http://127.0.0.1:8765`):

- **No Shared Server:** Your instance runs 100% locally on your own machine and is never connected to anyone else's server.
- **No Shared Leads or Phone Numbers:** All property searches, discovered owners, phone numbers, saved leads (`saved_leads.json`), and CSV exports stay strictly on your own hard drive. No other user can ever see your leads, and you will never see anyone else's leads.
- **Bring Your Own Gemini API Key:** Each user creates and uses their own personal Google Gemini API key stored locally in `config.json` (which is excluded via `.gitignore` and never uploaded anywhere).
- **Read-Only Upstream Source Protection:** You are free to customize your downloaded copy of the tool for your own personal workflow, but external users have **zero commit/push rights** to this repository—nothing you change locally can ever alter the master source code.

---

## 🚀 Core Capabilities

1. **📱 Personal Cell Phone Priority (Stage 3 Skip-Tracing):**
   - Automatically prioritizes the building owner's **Personal Cell / Wireless** numbers and direct personal lines above generic business switchboards or landlines.
   - Filters out out-of-town namesakes by cross-checking phone records against the owner's confirmed County Tax Bill Mailing Address and home city.
2. **🏘️ Owner Property Portfolio Discovery (Stage 1 GIS & Parcel Recon):**
   - Automatically queries County/Statewide GIS parcel records by both **Deed Owner Name** and **Tax Mailing Address** to uncover **how many total properties** the owner controls, combined assessed value, total acreage, personal homesteads, and sister LLCs.
3. **🛡️ True Beneficial Owner, Multi-LLC Veil Piercing & HOA/Group Decision-Maker Verification (Stage 2):**
   - Pierces single and multi-layered LLCs/Corporations by cross-referencing the **Deed Owner** $\rightarrow$ **Official County Tax Bill Mailing Address** $\rightarrow$ **Personal Homestead Deed** $\rightarrow$ **Secretary of State Officers (President / Managing Member / Registered Agent)**.
   - Filters out commercial registered agent services (CT Corp, CSC, LegalZoom, etc.).
   - For multi-owner groups, Churches, Condominiums, or HOAs where there is no single individual owner, automatically targets the **Building Care / Facilities Decision-Maker** (Board President, Trustee, or Property Manager).
4. **⚡ Live Street Address Autofill & 18 One-Click Deep-Link Portals:**
   - Type the first few characters of any US street address to auto-populate Street, City, State, ZIP, and County.
   - Generates pre-filled deep-links for **LandGlide, Land.com, Prospect by Buildout, Regrid, OpenCorporates, CorporationWiki, B2BHint, Bizapedia, TruePeopleSearch, FastPeopleSearch, Spokeo, CyberBackgroundChecks, SearchPeopleFree**, and **Google Gemini**.

---

## 🛠️ Easy Setup Guide (For Coworkers & New Users)

### Step 1: Install Python (One-Time Prerequisite)
Make sure **Python 3.10 or newer** is installed on your computer:
- Download from **[python.org/downloads](https://www.python.org/downloads/)**.
- ⚠️ **Important (Windows):** During installation, check the box that says **"Add python.exe to PATH"**.

---

### Step 2: Download the Latest Release & Launch Your Server
1. Go to the **[Releases](../../releases)** page of this GitHub repository.
2. Download the latest **`PropTrace-AI-v1.0.0-Portable.zip`** (or `Source code (zip)`).
3. Right-click the downloaded `.zip` file and click **Extract All...** to a folder on your computer (for example, `C:\PropTrace-AI`).
4. Double-click **`Start-PropTraceAI.bat`**.
   - On your very first launch, `Start-PropTraceAI.bat` will automatically create a local Python virtual environment (`.venv`) and install all required dependencies (`requirements.txt`).
   - Once setup finishes, your browser will automatically open **`http://127.0.0.1:8765`**.

*(Mac / Linux / Manual Terminal Users)*:
```bash
python -m venv .venv
# Windows:
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python app.py
# Mac/Linux:
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

---

### Step 3: Create & Add Your Own Free Google Gemini API Key

Each user must use their own Google Gemini API key. Creating one is **free** and takes about 30 seconds:

1. Go to **[Google AI Studio — Get API Key (https://aistudio.google.com/apikey)](https://aistudio.google.com/apikey)** and sign in with your Google account.
2. Click **"Create API Key"** and copy the generated key (it starts with `AIzaSy...`).
3. Put the key into PropTrace AI using **any one** of these 3 methods:
   - **Method A — Inside the App UI (Recommended):**
     When you first open `http://127.0.0.1:8765`, the **⚙️ Bring-Your-Own Gemini API Key & Local Settings** popup will appear automatically (or click **⚙️ API Key & Settings** in the top-right corner at any time). Paste your `AIzaSy...` key and click **💾 Save API Key Locally**.
   - **Method B — Local `config.json` File:**
     Create a file named `config.json` in your PropTrace AI folder with:
     ```json
     {
       "gemini_api_key": "AIzaSyYOUR_OWN_API_KEY_HERE"
     }
     ```
   - **Method C — Environment Variable:**
     Set the `GEMINI_API_KEY` environment variable on your system before starting the server.

> **Note:** Even before you add a Gemini API key, PropTrace AI's live GIS parcel/portfolio lookup, public web reconnaissance, and all 18 one-click skip-trace portal buttons work in **Free Recon Mode**!

---

## 🔧 Customizing Your Local Copy

- Once you extract the release onto your computer, **it is 100% yours to customize**.
- You can set your own custom CRM / **Core365 | via** URL inside **⚙️ API Key & Settings**, edit `app.py` or `static/app.js`, or modify the CSV export columns to match your personal workflow.
- Because your copy runs locally and has no write access to the upstream GitHub repository, **nothing you edit or save on your machine can ever change the original repository or affect any other user**.
