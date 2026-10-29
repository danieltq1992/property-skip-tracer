// PropTrace AI — Frontend Controller

let currentDossier = null;
let currentToolLinks = null;
let savedLeads = [];
let customCore365Url = localStorage.getItem("proptrace_core365_url") || "";
let batchSheetState = {
  filename: "",
  sheet_name: "",
  header_row: [],
  columns: [],
  data_rows: [],
  mapped_rows: [],
};

document.addEventListener("DOMContentLoaded", () => {
  initPropertyToggle();
  initAddressAutocomplete();
  initModals();
  initBatchSpreadsheetHandlers();
  initDemoButtons();
  initCopyButtons();
  checkApiStatus();
  loadSavedLeads();
  renderDefaultLaunchpad();

  document.getElementById("skipTraceForm").addEventListener("submit", handleInvestigateSubmit);
  document.getElementById("saveLeadBtn").addEventListener("click", handleSaveCurrentLead);
  document.getElementById("copyGeminiPromptBtn").addEventListener("click", handleCopyGeminiPrompt);
  document.getElementById("exportCsvBtn").addEventListener("click", exportLeadsToCsv);
  const exportXlsxBtn = document.getElementById("exportXlsxBtn");
  if (exportXlsxBtn) {
    exportXlsxBtn.addEventListener("click", exportLeadsToXlsx);
  }
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
  let abortController = null;
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

  async function triggerSuggestLookup() {
    const val = streetInput.value.trim();
    clearTimeout(debounceTimer);
    if (abortController) {
      abortController.abort();
      abortController = null;
    }
    if (val.length < 2) {
      closeDropdown();
      return;
    }

    debounceTimer = setTimeout(async () => {
      abortController = new AbortController();
      try {
        const params = new URLSearchParams({
          q: val,
          city: cityInput.value.trim(),
          state: stateInput.value.trim(),
        });
        const res = await fetch(`/api/address-suggest?${params.toString()}`, {
          signal: abortController.signal,
        });
        if (!res.ok) return;
        const data = await res.json();
        renderSuggestions(data.suggestions || []);
      } catch (err) {
        if (err.name !== "AbortError") {
          console.error("Address suggest error:", err);
        }
      }
    }, 140);
  }

  streetInput.addEventListener("input", triggerSuggestLookup);
  streetInput.addEventListener("focus", () => {
    if (streetInput.value.trim().length >= 2 && listEl.hidden) {
      triggerSuggestLookup();
    }
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

  const openSettings = () => {
    document.getElementById("core365UrlInput").value = customCore365Url;
    settingsDialog.showModal();
  };

  document.getElementById("openSettingsModalBtn").addEventListener("click", openSettings);
  document.getElementById("apiStatusBadge").addEventListener("click", openSettings);
  document.getElementById("apiStatusBadge").style.cursor = "pointer";

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
    showToast("Settings saved locally!");
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
      badge.textContent = "⚡ Free Recon Mode (Click to Add Your Gemini Key)";
      badge.className = "status-pill status-warn";
      if (!localStorage.getItem("proptrace_onboarded")) {
        localStorage.setItem("proptrace_onboarded", "1");
        const settingsDialog = document.getElementById("settingsDialog");
        if (settingsDialog && !settingsDialog.open) {
          settingsDialog.showModal();
        }
      }
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

  const valInfo = dossier.assessed_value_and_acres ? ` • ${dossier.assessed_value_and_acres}` : "";
  document.getElementById("dossierSubtitle").textContent =
    `${dossier.normalized_address} • ${dossier.property_type} Property${valInfo}`;
  document.getElementById("confidenceBadge").textContent = dossier.overall_confidence;
  document.getElementById("resDecisionMaker").textContent = dossier.primary_decision_maker || "—";
  document.getElementById("resDecisionMakerRole").textContent = dossier.decision_maker_role_and_authority || "";
  document.getElementById("resPrimaryPhone").textContent = dossier.primary_phone || "Use Stage 3 Free Links Below";
  document.getElementById("resPrimaryPhoneType").textContent =
    dossier.primary_phone_type || "📱 Personal Cell / Direct Owner Line";
  document.getElementById("resOwnerOfRecord").textContent = dossier.owner_of_record || "—";
  document.getElementById("resMailingAddress").textContent = dossier.tax_mailing_address || dossier.normalized_address;
  document.getElementById("resParcelApn").textContent = `APN: ${dossier.parcel_apn || "See Regrid/LandGlide/Land.com"}`;
  document.getElementById("resRegisteredAgent").textContent =
    dossier.registered_agent_name ? `Officers / Agent: ${dossier.registered_agent_name}` : "";
  document.getElementById("resEntityBadge").textContent =
    dossier.is_corporate_or_llc
      ? `Corporate/LLC/Trust Entity (${dossier.state_of_formation || "US"})`
      : "Individual / Direct Ownership";

  // Portfolio Summary Box
  const pCount = dossier.portfolio_count || 1;
  document.getElementById("resPortfolioCount").textContent =
    `${pCount} ${pCount === 1 ? "Property" : "Properties"} Owned`;
  document.getElementById("resPortfolioValue").textContent =
    dossier.portfolio_total_value || dossier.assessed_value_and_acres || "See Portfolio Below";

  // Ownership Verification Chain
  document.getElementById("resStructureType").textContent =
    dossier.ownership_structure_type || "Verified Ownership";
  document.getElementById("resVerificationProof").textContent =
    dossier.decision_maker_verification_proof || "";
  const layersEl = document.getElementById("shellLayersList");
  layersEl.innerHTML = (dossier.shell_layers_pierced || [])
    .map((layer) => `<span class="shell-layer-step">${escapeHtml(layer)}</span>`)
    .join("");

  // Contacts Breakdown (Personal Cell First)
  const contactsContainer = document.getElementById("contactsList");
  contactsContainer.innerHTML = "";
  const contacts = dossier.contacts && dossier.contacts.length ? dossier.contacts : [];

  contacts.forEach((c, idx) => {
    const card = document.createElement("div");
    card.className = `contact-card ${c.is_primary_decision_maker || idx === 0 ? "primary-contact-card" : ""}`;
    const phonesHtml = (c.phones && c.phones.length)
      ? c.phones
          .map(
            (p) => `
          <div class="phone-item ${p.is_personal_cell ? "personal-cell-item" : ""}">
            <div>
              <strong>${escapeHtml(p.number)}</strong>
              ${p.is_personal_cell ? `<span class="cell-tag">📱 PERSONAL CELL / DIRECT</span>` : ""}
              <div class="subtitle">${escapeHtml(p.phone_type)} • ${escapeHtml(p.carrier_or_source)}</div>
            </div>
            <button type="button" class="copy-mini-btn" onclick="navigator.clipboard.writeText('${escapeHtml(p.number)}'); showToast('Copied phone!')">Copy</button>
          </div>`
          )
          .join("")
      : `<div class="phone-item"><span class="subtitle">Click TruePeopleSearch or FastPeopleSearch below for instant free wireless/cell numbers</span></div>`;

    card.innerHTML = `
      <div class="contact-card-header">
        <strong>${escapeHtml(c.full_name)} ${c.age_or_demographics ? `<small class="subtitle">(${escapeHtml(c.age_or_demographics)})</small>` : ""}</strong>
        <span class="badge ${c.is_primary_decision_maker || idx === 0 ? "badge-success" : ""}">${escapeHtml(c.role)}</span>
      </div>
      ${c.verification_evidence ? `<div class="contact-evidence">✓ ${escapeHtml(c.verification_evidence)}</div>` : ""}
      <div class="subtitle">📍 Tax / Home Address: ${escapeHtml(c.mailing_or_home_address || dossier.normalized_address)}</div>
      ${c.emails && c.emails.length ? `<div class="subtitle">✉️ Email(s): ${escapeHtml(c.emails.join(", "))}</div>` : ""}
      ${c.relatives_or_associates && c.relatives_or_associates.length ? `<div class="subtitle">👥 Associated Co-Owners / Relatives: ${escapeHtml(c.relatives_or_associates.join(", "))}</div>` : ""}
      <div class="phone-list">${phonesHtml}</div>
    `;
    contactsContainer.appendChild(card);
  });

  // Portfolio Section
  document.getElementById("portfolioHeaderCount").textContent = pCount;
  document.getElementById("resPortfolioSummary").textContent =
    dossier.portfolio_summary || "";
  const portGrid = document.getElementById("portfolioPropertiesGrid");
  portGrid.innerHTML = "";
  const otherProps = dossier.other_properties || [];
  if (otherProps.length) {
    otherProps.forEach((op) => {
      const pCard = document.createElement("div");
      pCard.className = `portfolio-card ${op.is_owner_homestead ? "homestead-card" : ""}`;
      pCard.innerHTML = `
        <div class="portfolio-card-top">
          <span class="portfolio-addr">${escapeHtml(op.address)}</span>
          ${op.is_owner_homestead ? `<span class="cell-tag">🏠 Owner Homestead</span>` : `<span class="portfolio-meta">APN: ${escapeHtml(op.parcel_apn)}</span>`}
        </div>
        <div class="portfolio-meta">${escapeHtml(op.city_state)} • Held by: <strong>${escapeHtml(op.holding_entity)}</strong></div>
        <div class="portfolio-meta">${escapeHtml(op.assessed_value_or_details)}</div>
      `;
      portGrid.appendChild(pCard);
    });
  } else {
    portGrid.innerHTML = `<div class="portfolio-card"><span class="portfolio-meta">Target property is the primary parcel on record for this entity/address.</span></div>`;
  }

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
      { name: "TruePeopleSearch", badge: "Bookmark #3", source_type: "screenshot", description: "100% Free People Search & Personal Cell Phone Lookup", url: "https://www.truepeoplesearch.com/", copy_text: "" },
      { name: "FastPeopleSearch", badge: "Bookmark #2", source_type: "screenshot", description: "Free People Search & Personal Wireless Lookup", url: "https://www.fastpeoplesearch.com/", copy_text: "" },
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
  const prompt = `Find the true beneficial property owner of record, pierce any multi-layered LLC/Corporation (or identify the building-care manager if an HOA/group), list all other properties they own, and find their personal cell phone number for ${street}, ${city}, ${state} ${known ? `(Entity hint: ${known})` : ""}.`;
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
    const pCount = lead.portfolio_count || 1;
    const pBadge = `${pCount} ${pCount === 1 ? "Prop" : "Props"}${lead.portfolio_total_value ? ` (${lead.portfolio_total_value})` : ""}`;

    tr.innerHTML = `
      <th>${escapeHtml(lead.address)}</th>
      <td><span class="badge">${escapeHtml(lead.property_type)}</span></td>
      <td>${escapeHtml(lead.owner_of_record)}</td>
      <td><strong>${escapeHtml(lead.decision_maker)}</strong></td>
      <td><strong style="color:#34d399">${escapeHtml(phonesDisplay || "—")}</strong></td>
      <td><span class="badge badge-purple">${escapeHtml(pBadge)}</span></td>
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
  const allEmails = [];
  (currentDossier.contacts || []).forEach((c) => {
    (c.phones || []).forEach((p) => allPhones.push(p.number));
    (c.emails || []).forEach((em) => allEmails.push(em));
  });

  const payload = {
    address: currentDossier.normalized_address,
    property_type: currentDossier.property_type,
    owner_of_record: currentDossier.owner_of_record,
    decision_maker: currentDossier.primary_decision_maker,
    primary_phone: currentDossier.primary_phone,
    primary_phone_type: currentDossier.primary_phone_type || "Personal Cell / Direct",
    all_phones: allPhones,
    emails: allEmails,
    mailing_address: currentDossier.tax_mailing_address,
    portfolio_count: currentDossier.portfolio_count || 1,
    portfolio_total_value: currentDossier.portfolio_total_value || "",
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

const COLUMN_ROLE_OPTIONS = [
  { value: "street", label: "📍 Street / Property Address" },
  { value: "city", label: "🏙️ Property City" },
  { value: "state", label: "🗺️ Property State" },
  { value: "zip_code", label: "📮 ZIP Code" },
  { value: "known_owner_or_llc", label: "👤 Owner / LLC / Entity Name" },
  { value: "property_type", label: "🏢 Property Type (Res/Comm/Farm)" },
  { value: "known_phone", label: "📱 Phone / Cell Number" },
  { value: "mailing_address", label: "✉️ Mailing / Tax Address" },
  { value: "parcel_apn", label: "🔢 Parcel ID / APN" },
  { value: "notes", label: "📝 Extra Column / Keep in Notes" },
  { value: "ignore", label: "🚫 Ignore Column" },
];

function getBatchFallbackDefaults() {
  const dCity =
    (document.getElementById("batchDefaultCity")?.value || "").trim() ||
    (document.getElementById("cityInput")?.value || "").trim() ||
    "";
  const dState =
    (document.getElementById("batchDefaultState")?.value || "").trim() ||
    (document.getElementById("stateInput")?.value || "").trim() ||
    "NC";
  const dZip = (document.getElementById("zipInput")?.value || "").trim() || "";
  const dType =
    document.getElementById("batchDefaultType")?.value ||
    document.getElementById("propertyType")?.value ||
    "residential";
  return { dCity, dState, dZip, dType };
}

function initBatchSpreadsheetHandlers() {
  const dropZone = document.getElementById("batchDropZone");
  const fileInput = document.getElementById("batchFileInput");
  const browseBtn = document.getElementById("browseSpreadsheetBtn");
  const parsePastedBtn = document.getElementById("parsePastedBtn");

  if (!dropZone || !fileInput) return;

  browseBtn?.addEventListener("click", (e) => {
    e.stopPropagation();
    fileInput.click();
  });

  dropZone.addEventListener("click", (e) => {
    if (e.target !== browseBtn) {
      fileInput.click();
    }
  });

  fileInput.addEventListener("change", () => {
    if (fileInput.files && fileInput.files[0]) {
      uploadBatchSpreadsheetFile(fileInput.files[0]);
    }
  });

  dropZone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropZone.classList.add("drag-active");
  });

  dropZone.addEventListener("dragleave", () => {
    dropZone.classList.remove("drag-active");
  });

  dropZone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropZone.classList.remove("drag-active");
    if (e.dataTransfer?.files && e.dataTransfer.files[0]) {
      uploadBatchSpreadsheetFile(e.dataTransfer.files[0]);
    }
  });

  parsePastedBtn?.addEventListener("click", () => {
    parseBatchPastedText();
  });

  ["batchDefaultCity", "batchDefaultState", "batchDefaultType"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) {
      el.addEventListener("change", () => {
        if (batchSheetState.header_row.length) {
          remapCurrentBatchSheet();
        }
      });
    }
  });
}

async function uploadBatchSpreadsheetFile(file) {
  const { dCity, dState, dZip, dType } = getBatchFallbackDefaults();
  const formData = new FormData();
  formData.append("file", file);
  formData.append("default_city", dCity);
  formData.append("default_state", dState);
  formData.append("default_zip", dZip);
  formData.append("default_property_type", dType);

  const badge = document.getElementById("loadedFileBadge");
  if (badge) {
    badge.hidden = false;
    badge.textContent = `⏳ Reading Row 1 headers & rows from ${file.name}...`;
  }

  try {
    const res = await fetch("/api/batch/parse-spreadsheet", {
      method: "POST",
      body: formData,
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || "Failed to parse spreadsheet");
    }
    const data = await res.json();
    batchSheetState = {
      filename: data.filename,
      sheet_name: data.sheet_name,
      header_row: data.header_row || [],
      columns: data.columns || [],
      data_rows: data.data_rows || [],
      mapped_rows: data.mapped_rows || [],
    };
    if (badge) {
      badge.textContent = `✓ Loaded ${data.filename} (${data.sheet_name}) — ${data.total_rows} rows mapped from Row 1`;
    }
    renderBatchMappingAndPreview();
    showToast(`Read Row 1 (${batchSheetState.header_row.length} columns) & mapped ${data.total_rows} rows!`);
  } catch (err) {
    if (badge) {
      badge.textContent = `⚠️ ${err.message}`;
    }
    showToast(`Spreadsheet error: ${err.message}`);
  }
}

async function parseBatchPastedText() {
  const raw = document.getElementById("batchTextarea")?.value.trim() || "";
  if (!raw) {
    showToast("Paste rows from Excel or CSV into the box first!");
    return false;
  }
  const { dCity, dState, dZip, dType } = getBatchFallbackDefaults();
  try {
    const res = await fetch("/api/batch/parse-text", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        raw_text: raw,
        default_city: dCity,
        default_state: dState,
        default_zip: dZip,
        default_property_type: dType,
      }),
    });
    if (!res.ok) return false;
    const data = await res.json();
    batchSheetState = {
      filename: data.filename,
      sheet_name: data.sheet_name,
      header_row: data.header_row || [],
      columns: data.columns || [],
      data_rows: data.data_rows || [],
      mapped_rows: data.mapped_rows || [],
    };
    renderBatchMappingAndPreview();
    showToast(`Mapped ${data.total_rows} rows using top row column order!`);
    return true;
  } catch (e) {
    console.error(e);
    return false;
  }
}

function renderBatchMappingAndPreview() {
  const sec = document.getElementById("batchMappingSection");
  const colsContainer = document.getElementById("batchColumnsContainer");
  const countBadge = document.getElementById("batchRowCountBadge");
  const summaryEl = document.getElementById("batchSheetSummary");

  if (!batchSheetState.columns.length) {
    sec.hidden = true;
    return;
  }
  sec.hidden = false;

  countBadge.textContent = `${batchSheetState.mapped_rows.length} Rows Ready`;
  summaryEl.textContent = `Source: ${batchSheetState.filename} (${batchSheetState.sheet_name}) • Top Row has ${batchSheetState.columns.length} columns in order (${batchSheetState.columns.map((c) => `Col ${c.col_letter}: "${c.header_name}"`).join(" → ")})`;

  colsContainer.innerHTML = "";
  batchSheetState.columns.forEach((col, idx) => {
    const card = document.createElement("div");
    card.className = "col-map-card";

    const optionsHtml = COLUMN_ROLE_OPTIONS.map(
      (opt) =>
        `<option value="${opt.value}" ${col.mapped_to === opt.value ? "selected" : ""}>${escapeHtml(opt.label)}</option>`
    ).join("");

    card.innerHTML = `
      <div class="col-map-top">
        <span class="col-letter-tag">Col ${escapeHtml(col.col_letter)} (#${idx + 1})</span>
        <span class="col-header-title" title="Row 1 Header: ${escapeHtml(col.header_name)}">${escapeHtml(col.header_name)}</span>
      </div>
      <div class="col-sample-text" title="Row 2 Sample: ${escapeHtml(col.sample_value || "—")}">
        Row 2: ${escapeHtml(col.sample_value || "—")}
      </div>
      <select class="col-role-select" data-col-index="${idx}" aria-label="Column mapping for ${escapeHtml(col.header_name)}">
        ${optionsHtml}
      </select>
    `;

    card.querySelector(".col-role-select").addEventListener("change", (e) => {
      batchSheetState.columns[idx].mapped_to = e.target.value;
      remapCurrentBatchSheet();
    });

    colsContainer.appendChild(card);
  });

  renderBatchPreviewTable();
}

function renderBatchPreviewTable() {
  const tbody = document.getElementById("batchPreviewBody");
  const countBadge = document.getElementById("batchRowCountBadge");
  if (!tbody) return;

  countBadge.textContent = `${batchSheetState.mapped_rows.length} Rows Ready`;
  tbody.innerHTML = "";

  batchSheetState.mapped_rows.slice(0, 50).forEach((r) => {
    const tr = document.createElement("tr");
    const extraInfo = [r.known_phone ? `📱 ${r.known_phone}` : "", r.notes || ""].filter(Boolean).join(" • ") || "—";
    tr.innerHTML = `
      <td><span class="badge">Row ${r.row_number}</span></td>
      <td><strong>${escapeHtml(r.street || "—")}</strong></td>
      <td>${escapeHtml(r.city || "—")}</td>
      <td>${escapeHtml(r.state || "—")}</td>
      <td>${escapeHtml(r.zip_code || "—")}</td>
      <td>${escapeHtml(r.known_owner_or_llc || "—")}</td>
      <td><span class="badge">${escapeHtml(r.property_type)}</span></td>
      <td><small>${escapeHtml(extraInfo)}</small></td>
    `;
    tbody.appendChild(tr);
  });
}

async function remapCurrentBatchSheet() {
  if (!batchSheetState.header_row.length) return;
  const { dCity, dState, dZip, dType } = getBatchFallbackDefaults();
  const roles = batchSheetState.columns.map((c) => c.mapped_to);

  try {
    const res = await fetch("/api/batch/remap", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        header_row: batchSheetState.header_row,
        data_rows: batchSheetState.data_rows,
        column_roles: roles,
        default_city: dCity,
        default_state: dState,
        default_zip: dZip,
        default_property_type: dType,
      }),
    });
    if (!res.ok) return;
    const data = await res.json();
    batchSheetState.mapped_rows = data.mapped_rows || [];
    renderBatchPreviewTable();
  } catch (e) {
    console.error(e);
  }
}

async function handleBatchRun() {
  // If user pasted text into textarea without clicking "Read Top Row Headers" yet, parse it automatically first
  if (!batchSheetState.mapped_rows.length) {
    const ok = await parseBatchPastedText();
    if (!ok || !batchSheetState.mapped_rows.length) {
      showToast("Upload an Excel (.xlsx/.xls) or CSV file, or paste rows above first!");
      return;
    }
  }

  const rowsToRun = batchSheetState.mapped_rows;
  const progBox = document.getElementById("batchProgressBox");
  const progTxt = document.getElementById("batchProgressText");
  const runBtn = document.getElementById("runBatchBtn");
  progBox.hidden = false;
  runBtn.disabled = true;

  let completed = 0;
  for (let i = 0; i < rowsToRun.length; i++) {
    const item = rowsToRun[i];
    if (!item.street) continue;

    progTxt.textContent = `Processing Sheet Row ${item.row_number} (${i + 1} of ${rowsToRun.length}): ${item.street}, ${item.city}, ${item.state} ${item.known_owner_or_llc ? `(${item.known_owner_or_llc})` : ""}...`;

    try {
      const res = await fetch("/api/investigate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          street: item.street,
          city: item.city,
          state: item.state,
          zip_code: item.zip_code || "",
          property_type: item.property_type || "residential",
          known_owner_or_llc: item.known_owner_or_llc || "",
          known_phone: item.known_phone || "",
        }),
      });
      if (res.ok) {
        const data = await res.json();
        const d = data.dossier;
        const allPhones = [];
        const allEmails = [];
        (d.contacts || []).forEach((c) => {
          (c.phones || []).forEach((p) => allPhones.push(p.number));
          (c.emails || []).forEach((em) => allEmails.push(em));
        });
        await fetch("/api/leads", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            address: d.normalized_address,
            property_type: d.property_type,
            owner_of_record: d.owner_of_record,
            decision_maker: d.primary_decision_maker,
            primary_phone: d.primary_phone,
            primary_phone_type: d.primary_phone_type || "Personal Cell / Direct",
            all_phones: allPhones,
            emails: allEmails,
            mailing_address: d.tax_mailing_address,
            portfolio_count: d.portfolio_count || 1,
            portfolio_total_value: d.portfolio_total_value || "",
            confidence: d.overall_confidence,
            notes: item.notes || "",
          }),
        });
        completed++;
      }
    } catch (e) {
      console.error(e);
    }
  }

  runBtn.disabled = false;
  await loadSavedLeads();
  progTxt.textContent = `✓ Completed ${completed} of ${rowsToRun.length} spreadsheet rows! All results saved to Leads Table.`;
  showToast(`Batch skip-trace complete (${completed} properties saved)!`);
}

function exportLeadsToXlsx() {
  if (!savedLeads.length) {
    showToast("No saved leads to export yet!");
    return;
  }
  window.location.href = "/api/leads/export-xlsx";
  showToast("Downloading Microsoft Excel (.xlsx) workbook...");
}

function exportLeadsToCsv() {
  if (!savedLeads.length) {
    showToast("No saved leads to export yet!");
    return;
  }
  const headers = [
    "Timestamp",
    "Property Address",
    "Type",
    "Deed Owner / LLC",
    "Verified Decision Maker",
    "Primary Personal Cell / Phone",
    "All Discovered Phones",
    "Portfolio Properties Count",
    "Portfolio Value & Details",
    "Tax / Home Mailing Address",
    "Notes / Verification Proof",
    "Confidence",
  ];
  const rows = savedLeads.map((l) => [
    l.created_at || "",
    l.address || "",
    l.property_type || "",
    l.owner_of_record || "",
    l.decision_maker || "",
    l.primary_phone || "",
    (l.all_phones || []).join(" | "),
    l.portfolio_count || 1,
    l.portfolio_total_value || "",
    l.mailing_address || "",
    l.notes || "",
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
