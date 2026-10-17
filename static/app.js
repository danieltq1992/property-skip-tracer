// PropTrace AI — Frontend Controller

let currentDossier = null;
let currentToolLinks = null;
let savedLeads = [];
let customCore365Url = localStorage.getItem("proptrace_core365_url") || "";

document.addEventListener("DOMContentLoaded", () => {
  initPropertyToggle();
  initAddressAutocomplete();
  initModals();
  initDemoButtons();
  initCopyButtons();
  checkApiStatus();
  loadSavedLeads();
  renderDefaultLaunchpad();

  document.getElementById("skipTraceForm").addEventListener("submit", handleInvestigateSubmit);
  document.getElementById("saveLeadBtn").addEventListener("click", handleSaveCurrentLead);
  document.getElementById("copyGeminiPromptBtn").addEventListener("click", handleCopyGeminiPrompt);
  document.getElementById("exportCsvBtn").addEventListener("click", exportLeadsToCsv);
  document.getElementById("addManualLeadBtn").addEventListener("click", handleQuickAddLead);

  document.getElementById("launchStage1Btn").addEventListener("click", () => launchGroup("property_tools"));
  document.getElementById("launchStage2Btn").addEventListener("click", () => launchGroup("entity_tools"));
  document.getElementById("launchStage3Btn").addEventListener("click", () => launchGroup("people_tools"));
});

function showToast(msg) {
  const toast = document.getElementById("toast");
  toast.textContent = msg;
  toast.hidden = false;
  clearTimeout(window._toastTimer);
  window._toastTimer = setTimeout(() => {
    toast.hidden = true;
  }, 3400);
}

function initAddressAutocomplete() {
  const streetInput = document.getElementById("streetInput");
  const cityInput = document.getElementById("cityInput");
  const stateInput = document.getElementById("stateInput");
  const zipInput = document.getElementById("zipInput");
  const listEl = document.getElementById("addressSuggestionsList");

  let debounceTimer = null;
  let currentItems = [];
  let activeIndex = -1;

  function closeDropdown() {
    listEl.hidden = true;
    listEl.innerHTML = "";
    streetInput.setAttribute("aria-expanded", "false");
    currentItems = [];
    activeIndex = -1;
  }

  function selectSuggestion(item) {
    streetInput.value = item.street || "";
    cityInput.value = item.city || "";
    stateInput.value = (item.state || "").toUpperCase();
    zipInput.value = item.zip_code || "";
    closeDropdown();

    [streetInput, cityInput, stateInput, zipInput].forEach((el) => {
      el.classList.add("autofill-flash");
      setTimeout(() => el.classList.remove("autofill-flash"), 900);
    });

    showToast(`✓ Auto-populated: ${item.street}, ${item.city}, ${item.state} ${item.zip_code}`);
  }

  function renderSuggestions(items) {
    currentItems = items || [];
    activeIndex = -1;
    listEl.innerHTML = "";

    if (!currentItems.length) {
      closeDropdown();
      return;
    }

    currentItems.forEach((item, idx) => {
      const li = document.createElement("li");
      li.className = "autocomplete-item";
      li.setAttribute("role", "option");
      li.dataset.index = idx;

      li.innerHTML = `
        <div class="autocomplete-main">
          <span class="autocomplete-street">${escapeHtml(item.street)}</span>
          <span class="autocomplete-csz">${escapeHtml(item.city)}, ${escapeHtml(item.state)} ${escapeHtml(item.zip_code)}</span>
        </div>
        ${item.county ? `<span class="autocomplete-county">${escapeHtml(item.county)}</span>` : ""}
      `;

      li.addEventListener("mousedown", (e) => {
        e.preventDefault();
        selectSuggestion(item);
      });

      listEl.appendChild(li);
    });

    listEl.hidden = false;
    streetInput.setAttribute("aria-expanded", "true");
  }

  streetInput.addEventListener("input", () => {
    const val = streetInput.value.trim();
    clearTimeout(debounceTimer);
    if (val.length < 3) {
      closeDropdown();
      return;
    }

    debounceTimer = setTimeout(async () => {
      try {
        const params = new URLSearchParams({
          q: val,
          city: cityInput.value.trim(),
          state: stateInput.value.trim(),
        });
        const res = await fetch(`/api/address-suggest?${params.toString()}`);
        if (!res.ok) return;
        const data = await res.json();
        renderSuggestions(data.suggestions || []);
      } catch (err) {
        console.error("Address suggest error:", err);
      }
    }, 220);
  });

  streetInput.addEventListener("keydown", (e) => {
    if (listEl.hidden || !currentItems.length) return;
    const optionEls = listEl.querySelectorAll(".autocomplete-item");

    if (e.key === "ArrowDown") {
      e.preventDefault();
      activeIndex = (activeIndex + 1) % currentItems.length;
      optionEls.forEach((el, i) => el.classList.toggle("active", i === activeIndex));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      activeIndex = (activeIndex - 1 + currentItems.length) % currentItems.length;
      optionEls.forEach((el, i) => el.classList.toggle("active", i === activeIndex));
    } else if (e.key === "Enter" && activeIndex >= 0) {
      e.preventDefault();
      selectSuggestion(currentItems[activeIndex]);
    } else if (e.key === "Escape") {
      closeDropdown();
    }
  });

  document.addEventListener("click", (e) => {
    if (!e.target.closest(".autocomplete-wrapper")) {
      closeDropdown();
    }
  });
}

function initPropertyToggle() {
  const buttons = document.querySelectorAll(".toggle-btn");
  const hiddenInput = document.getElementById("propertyType");
  buttons.forEach((btn) => {
    btn.addEventListener("click", () => {
      buttons.forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      hiddenInput.value = btn.dataset.type;
    });
  });
}

function initModals() {
  const settingsDialog = document.getElementById("settingsDialog");
  const batchDialog = document.getElementById("batchDialog");

  document.getElementById("openSettingsModalBtn").addEventListener("click", () => {
    document.getElementById("core365UrlInput").value = customCore365Url;
    settingsDialog.showModal();
  });

  document.getElementById("openBatchModalBtn").addEventListener("click", () => {
    batchDialog.showModal();
  });

  document.querySelectorAll("[data-close-dialog]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const target = document.getElementById(btn.dataset.closeDialog);
      if (target) target.close();
    });
  });

  document.getElementById("settingsForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const key = document.getElementById("apiKeyModalInput").value.trim();
    const c365 = document.getElementById("core365UrlInput").value.trim();
    if (c365) {
      customCore365Url = c365;
      localStorage.setItem("proptrace_core365_url", c365);
    }
    if (key) {
      await fetch("/api/settings/apikey", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ gemini_api_key: key }),
      });
    }
    settingsDialog.close();
    await checkApiStatus();
    showToast("Settings saved!");
  });

  document.getElementById("runBatchBtn").addEventListener("click", handleBatchRun);
}

async function checkApiStatus() {
  try {
    const res = await fetch("/api/status");
    const data = await res.json();
    const badge = document.getElementById("apiStatusBadge");
    if (data.has_api_key) {
      badge.textContent = `✓ Gemini API Active (${data.masked_key})`;
      badge.className = "status-pill status-ok";
    } else {
      badge.textContent = "⚡ Free Recon + Deep-Link Mode (Add Gemini Key in Settings)";
      badge.className = "status-pill status-warn";
    }
  } catch (err) {
    console.error(err);
  }
}

function initDemoButtons() {
  const farmBtn = document.getElementById("sampleFarmBtn");
  if (farmBtn) {
    farmBtn.addEventListener("click", () => {
      document.querySelector('[data-type="farm"]').click();
      document.getElementById("streetInput").value = "6666 US Highway 82";
      document.getElementById("cityInput").value = "Guthrie";
      document.getElementById("stateInput").value = "TX";
      document.getElementById("zipInput").value = "79236";
      document.getElementById("knownOwnerInput").value = "Four Sixes Ranch";
      document.getElementById("skipTraceForm").requestSubmit();
    });
  }

  document.getElementById("sampleCommercialBtn").addEventListener("click", () => {
    document.querySelector('[data-type="commercial"]').click();
    document.getElementById("streetInput").value = "350 5th Ave";
    document.getElementById("cityInput").value = "New York";
    document.getElementById("stateInput").value = "NY";
    document.getElementById("zipInput").value = "10118";
    document.getElementById("knownOwnerInput").value = "Empire State Realty Trust Inc";
    document.getElementById("skipTraceForm").requestSubmit();
  });

  document.getElementById("sampleResidentialBtn").addEventListener("click", () => {
    document.querySelector('[data-type="residential"]').click();
    document.getElementById("streetInput").value = "1600 Pennsylvania Ave NW";
    document.getElementById("cityInput").value = "Washington";
    document.getElementById("stateInput").value = "DC";
    document.getElementById("zipInput").value = "20500";
    document.getElementById("knownOwnerInput").value = "";
    document.getElementById("skipTraceForm").requestSubmit();
  });
}

function initCopyButtons() {
  document.querySelectorAll(".copy-mini-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const targetEl = document.getElementById(btn.dataset.copyTarget);
      if (targetEl && targetEl.textContent) {
        navigator.clipboard.writeText(targetEl.textContent.trim());
        showToast(`Copied: ${targetEl.textContent.trim()}`);
      }
    });
  });
}

function setPipelineState(stageNum, state, message) {
  const stepEl = document.getElementById(`stepAgent${stageNum}`);
  const statusEl = document.getElementById(`statusAgent${stageNum}`);
  const outEl = document.getElementById(`outputAgent${stageNum}`);

  stepEl.classList.remove("running", "done");
  if (state === "running") {
    stepEl.classList.add("running");
    statusEl.textContent = "Running...";
  } else if (state === "done") {
    stepEl.classList.add("done");
    statusEl.textContent = "Complete";
  } else {
    statusEl.textContent = "Idle";
  }
  if (message) outEl.textContent = message;
}

async function handleInvestigateSubmit(e) {
  e.preventDefault();
  const payload = {
    street: document.getElementById("streetInput").value.trim(),
    city: document.getElementById("cityInput").value.trim(),
    state: document.getElementById("stateInput").value.trim(),
    zip_code: document.getElementById("zipInput").value.trim(),
    property_type: document.getElementById("propertyType").value,
    known_owner_or_llc: document.getElementById("knownOwnerInput").value.trim(),
    known_phone: document.getElementById("knownPhoneInput").value.trim(),
    model: document.getElementById("modelSelect").value,
    auto_open_tabs: document.getElementById("autoOpenTabsCheckbox").checked,
  };

  const runBtn = document.getElementById("runAgentsBtn");
  const timerBadge = document.getElementById("pipelineTimer");
  runBtn.disabled = true;
  runBtn.textContent = "⏳ Investigating Property & Piercing Entities...";
  timerBadge.textContent = "Agents Active";

  setPipelineState(1, "running", `Searching parcel, farm & deed records for ${payload.street}, ${payload.city}, ${payload.state}...`);
  setPipelineState(2, "running", "Querying OpenCorporates, CorporationWiki & B2BHint for corporate principals...");
  setPipelineState(3, "running", "Cross-referencing TruePeopleSearch, FastPeopleSearch, Spokeo & public records...");

  try {
    const res = await fetch("/api/investigate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || "Investigation failed");
    }
    const data = await res.json();
    currentDossier = data.dossier;
    currentToolLinks = data.tool_links;

    setPipelineState(1, "done", currentDossier.agent_1_property_findings);
    setPipelineState(2, "done", currentDossier.agent_2_entity_findings);
    setPipelineState(3, "done", currentDossier.agent_3_phone_findings);
    timerBadge.textContent = "Investigation Complete";

    renderDossier(currentDossier);
    renderLaunchpad(currentToolLinks);
    showToast("Skip-trace dossier & all deep-links updated!");
  } catch (err) {
    showToast(`Error: ${err.message}`);
    timerBadge.textContent = "Error";
  } finally {
    runBtn.disabled = false;
    runBtn.textContent = "🚀 Run AI Skip-Trace Agents";
  }
}

function renderDossier(dossier) {
  const sec = document.getElementById("dossierSection");
  sec.hidden = false;

  document.getElementById("dossierSubtitle").textContent =
    `${dossier.normalized_address} • ${dossier.property_type} Property`;
  document.getElementById("confidenceBadge").textContent = dossier.overall_confidence;
  document.getElementById("resDecisionMaker").textContent = dossier.primary_decision_maker || "—";
  document.getElementById("resPrimaryPhone").textContent = dossier.primary_phone || "Use Stage 3 Free Links Below";
  document.getElementById("resOwnerOfRecord").textContent = dossier.owner_of_record || "—";
  document.getElementById("resMailingAddress").textContent = dossier.tax_mailing_address || dossier.normalized_address;
  document.getElementById("resParcelApn").textContent = `APN: ${dossier.parcel_apn || "See Regrid/LandGlide/Land.com"}`;
  document.getElementById("resRegisteredAgent").textContent =
    dossier.registered_agent_name ? `Registered Agent / Officer: ${dossier.registered_agent_name}` : "";
  document.getElementById("resEntityBadge").textContent =
    dossier.is_corporate_or_llc
      ? `Corporate/LLC/Trust Entity (${dossier.state_of_formation || "US"})`
      : "Individual / Direct Ownership";

  // Contacts Breakdown
  const contactsContainer = document.getElementById("contactsList");
  contactsContainer.innerHTML = "";
  const contacts = dossier.contacts && dossier.contacts.length ? dossier.contacts : [];

  contacts.forEach((c) => {
    const card = document.createElement("div");
    card.className = "contact-card";
    const phonesHtml = (c.phones && c.phones.length)
      ? c.phones
          .map(
            (p) => `
          <div class="phone-item">
            <div>
              <strong>${escapeHtml(p.number)}</strong>
              <span class="subtitle">(${escapeHtml(p.phone_type)} • ${escapeHtml(p.carrier_or_source)})</span>
            </div>
            <button type="button" class="copy-mini-btn" onclick="navigator.clipboard.writeText('${escapeHtml(p.number)}'); showToast('Copied phone!')">Copy</button>
          </div>`
          )
          .join("")
      : `<div class="phone-item"><span class="subtitle">Click TruePeopleSearch or FastPeopleSearch below for instant free wireless/landline numbers</span></div>`;

    card.innerHTML = `
      <div class="contact-card-header">
        <strong>${escapeHtml(c.full_name)}</strong>
        <span class="badge">${escapeHtml(c.role)}</span>
      </div>
      <div class="subtitle">📍 ${escapeHtml(c.mailing_or_home_address || dossier.normalized_address)}</div>
      ${c.emails && c.emails.length ? `<div class="subtitle">✉️ ${escapeHtml(c.emails.join(", "))}</div>` : ""}
      <div class="phone-list">${phonesHtml}</div>
    `;
    contactsContainer.appendChild(card);
  });

  // Next Steps
  const stepsUl = document.getElementById("recommendedStepsList");
  stepsUl.innerHTML = (dossier.recommended_next_steps || [])
    .map((s) => `<li>${escapeHtml(s)}</li>`)
    .join("");
}

function renderDefaultLaunchpad() {
  renderLaunchpad({
    property_tools: [
      { name: "LandGlide", badge: "Bookmark #8", source_type: "screenshot", description: "Enter an address above to pre-fill LandGlide parcel search", url: "https://app.landglide.com/", copy_text: "" },
      { name: "Land.com (Farm, Ranch & Acreage Owners)", badge: "Farm & Land Source", source_type: "screenshot", description: "Find farm, ranch & rural land owners, brochures & listing brokers on Land.com", url: "https://www.land.com/", copy_text: "" },
      { name: "Prospect by Buildout", badge: "Bookmark #9", source_type: "screenshot", description: "Commercial & residential property ownership & predictive intel", url: "https://prospect.buildout.com/", copy_text: "" },
      { name: "Regrid Free Parcel & Deed Search", badge: "Bonus Free GIS", source_type: "bonus", description: "Free nationwide parcel viewer with APN & Deed Owner", url: "https://app.regrid.com/us", copy_text: "" },
    ],
    entity_tools: [
      { name: "OpenCorporates", badge: "Bookmark #5", source_type: "screenshot", description: "Corporate & LLC officer/registered agent registry", url: "https://opencorporates.com/", copy_text: "" },
      { name: "CorporationWiki", badge: "Bookmark #4", source_type: "screenshot", description: "Relationship graph mapping LLCs to human principals", url: "https://www.corporationwiki.com/", copy_text: "" },
      { name: "B2BHint (Business Data Platform)", badge: "Bookmark #6", source_type: "screenshot", description: "Business Data Platform, Company Search, API & Network", url: "https://b2bhint.com/en", copy_text: "" },
    ],
    people_tools: [
      { name: "TruePeopleSearch", badge: "Bookmark #3", source_type: "screenshot", description: "100% Free People Search & Reverse Address/Phone Lookup", url: "https://www.truepeoplesearch.com/", copy_text: "" },
      { name: "FastPeopleSearch", badge: "Bookmark #2", source_type: "screenshot", description: "Free People Search & Reverse Phone/Address Lookup", url: "https://www.fastpeoplesearch.com/", copy_text: "" },
      { name: "Spokeo", badge: "Bookmark #1", source_type: "screenshot", description: "People Search | White Pages | Reverse Phone Lookup", url: "https://www.spokeo.com/", copy_text: "" },
      { name: "Core365 | via & Google Gemini", badge: "Bookmarks #7 & #10", source_type: "screenshot", description: "AI Agent synthesis & CRM outreach portal", url: "https://gemini.google.com/app", copy_text: "" },
    ],
  });
}

function renderLaunchpad(toolLinks) {
  renderToolColumn("propertyToolsContainer", toolLinks.property_tools || []);
  renderToolColumn("entityToolsContainer", toolLinks.entity_tools || []);
  renderToolColumn("peopleToolsContainer", toolLinks.people_tools || []);
}

function renderToolColumn(containerId, tools) {
  const container = document.getElementById(containerId);
  container.innerHTML = "";

  tools.forEach((tool) => {
    const a = document.createElement("a");
    a.className = "tool-card";
    a.href = tool.id === "core365_gemini" && customCore365Url ? customCore365Url : tool.url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";

    a.addEventListener("click", () => {
      if (tool.copy_text) {
        navigator.clipboard.writeText(tool.copy_text);
        showToast(`Copied "${tool.copy_text}" to clipboard & opening ${tool.name}`);
      }
    });

    a.innerHTML = `
      <div class="tool-card-top">
        <span class="tool-name">↗ ${escapeHtml(tool.name)}</span>
        <span class="tool-badge ${tool.source_type === "screenshot" ? "screenshot" : "bonus"}">${escapeHtml(tool.badge)}</span>
      </div>
      <p class="tool-desc">${escapeHtml(tool.description)}</p>
    `;
    container.appendChild(a);
  });
}

async function launchGroup(groupKey) {
  if (!currentToolLinks || !currentToolLinks[groupKey]) {
    showToast("Run a property investigation first so deep-links are populated with the address & owner!");
    return;
  }
  const urls = currentToolLinks[groupKey].map((t) => t.url);
  await fetch("/api/open-tabs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ urls }),
  });
  showToast(`Opened ${urls.length} tabs in your browser!`);
}

function handleCopyGeminiPrompt() {
  const street = document.getElementById("streetInput").value.trim();
  const city = document.getElementById("cityInput").value.trim();
  const state = document.getElementById("stateInput").value.trim();
  const known = document.getElementById("knownOwnerInput").value.trim();
  const prompt = `Find the property owner of record and pierce any LLC/Corporation for ${street}, ${city}, ${state} ${known ? `(Entity hint: ${known})` : ""}. Check county tax assessor records, OpenCorporates, CorporationWiki, B2BHint, TruePeopleSearch, and FastPeopleSearch to identify the human managing member/owner, their mailing address, and their phone numbers.`;
  navigator.clipboard.writeText(prompt);
  window.open("https://gemini.google.com/app", "_blank");
  showToast("Skip-trace prompt copied! Press Ctrl+V in Google Gemini.");
}

async function loadSavedLeads() {
  const res = await fetch("/api/leads");
  const data = await res.json();
  savedLeads = data.leads || [];
  renderLeadsTable();
}

function renderLeadsTable() {
  const tbody = document.getElementById("leadsTableBody");
  tbody.innerHTML = "";

  if (!savedLeads.length) {
    tbody.innerHTML = `
      <tr>
        <th>No saved leads yet</th>
        <td>—</td>
        <td>Run a property investigation above and click "Save Lead"</td>
        <td>—</td>
        <td>—</td>
        <td>—</td>
        <td>—</td>
      </tr>`;
    return;
  }

  savedLeads.forEach((lead) => {
    const tr = document.createElement("tr");
    const phonesDisplay = [lead.primary_phone, ...(lead.all_phones || [])]
      .filter(Boolean)
      .filter((v, i, arr) => arr.indexOf(v) === i)
      .join(", ");

    tr.innerHTML = `
      <th>${escapeHtml(lead.address)}</th>
      <td><span class="badge">${escapeHtml(lead.property_type)}</span></td>
      <td>${escapeHtml(lead.owner_of_record)}</td>
      <td><strong>${escapeHtml(lead.decision_maker)}</strong></td>
      <td>${escapeHtml(phonesDisplay || "—")}</td>
      <td>${escapeHtml(lead.mailing_address || "—")}</td>
      <td>
        <button type="button" class="copy-mini-btn" onclick="deleteLead('${escapeHtml(lead.id)}')">🗑️ Delete</button>
      </td>
    `;
    tbody.appendChild(tr);
  });
}

async function handleSaveCurrentLead() {
  if (!currentDossier) return;
  const allPhones = [];
  (currentDossier.contacts || []).forEach((c) => {
    (c.phones || []).forEach((p) => allPhones.push(p.number));
  });

  const payload = {
    address: currentDossier.normalized_address,
    property_type: currentDossier.property_type,
    owner_of_record: currentDossier.owner_of_record,
    decision_maker: currentDossier.primary_decision_maker,
    primary_phone: currentDossier.primary_phone,
    all_phones: allPhones,
    emails: [],
    mailing_address: currentDossier.tax_mailing_address,
    confidence: currentDossier.overall_confidence,
  };

  await fetch("/api/leads", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  await loadSavedLeads();
  showToast("Lead saved to table!");
}

async function handleQuickAddLead() {
  const street = document.getElementById("streetInput").value.trim() || "Custom Property";
  const city = document.getElementById("cityInput").value.trim();
  const state = document.getElementById("stateInput").value.trim();
  const owner = document.getElementById("knownOwnerInput").value.trim() || "Unknown Owner";
  const phone = document.getElementById("knownPhoneInput").value.trim() || "";

  await fetch("/api/leads", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      address: `${street}, ${city}, ${state}`.replace(/^,\s*/, ""),
      property_type: document.getElementById("propertyType").value,
      owner_of_record: owner,
      decision_maker: owner,
      primary_phone: phone,
      mailing_address: `${street}, ${city}, ${state}`.replace(/^,\s*/, ""),
    }),
  });
  await loadSavedLeads();
  showToast("Added lead to table!");
}

async function deleteLead(id) {
  await fetch(`/api/leads/${id}`, { method: "DELETE" });
  await loadSavedLeads();
  showToast("Lead removed.");
}

async function handleBatchRun() {
  const raw = document.getElementById("batchTextarea").value.trim();
  if (!raw) return;
  const lines = raw.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  const progBox = document.getElementById("batchProgressBox");
  const progTxt = document.getElementById("batchProgressText");
  progBox.hidden = false;

  for (let i = 0; i < lines.length; i++) {
    progTxt.textContent = `Processing ${i + 1} of ${lines.length}: ${lines[i]}...`;
    const parts = lines[i].split(",").map((p) => p.trim());
    const street = parts[0] || "";
    const city = parts[1] || "";
    const state = parts[2] || "";
    const zip_code = parts[3] || "";
    const property_type = (parts[4] || "residential").toLowerCase().includes("comm") ? "commercial" : "residential";
    const known_owner_or_llc = parts[5] || "";

    if (!street || !city || !state) continue;
    try {
      const res = await fetch("/api/investigate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ street, city, state, zip_code, property_type, known_owner_or_llc }),
      });
      if (res.ok) {
        const data = await res.json();
        const d = data.dossier;
        await fetch("/api/leads", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            address: d.normalized_address,
            property_type: d.property_type,
            owner_of_record: d.owner_of_record,
            decision_maker: d.primary_decision_maker,
            primary_phone: d.primary_phone,
            mailing_address: d.tax_mailing_address,
            confidence: d.overall_confidence,
          }),
        });
      }
    } catch (e) {
      console.error(e);
    }
  }

  await loadSavedLeads();
  progTxt.textContent = `Completed ${lines.length} properties! Saved to Leads Table.`;
  showToast(`Batch skip-trace complete (${lines.length} properties)!`);
}

function exportLeadsToCsv() {
  if (!savedLeads.length) {
    showToast("No saved leads to export yet!");
    return;
  }
  const headers = ["Timestamp", "Property Address", "Type", "Deed Owner / LLC", "Decision Maker", "Primary Phone", "Mailing Address", "Confidence"];
  const rows = savedLeads.map((l) => [
    l.created_at || "",
    l.address || "",
    l.property_type || "",
    l.owner_of_record || "",
    l.decision_maker || "",
    l.primary_phone || "",
    l.mailing_address || "",
    l.confidence || "",
  ]);

  const csvContent =
    [headers, ...rows]
      .map((row) => row.map((cell) => `"${String(cell).replace(/"/g, '""')}"`).join(","))
      .join("\n");

  const blob = new Blob([csvContent], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `proptrace_leads_${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

function escapeHtml(str) {
  return String(str || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
