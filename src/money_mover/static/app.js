const fmt = (n) =>
  new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(n || 0);

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${path}: ${res.status} ${text}`);
  }
  return res.headers.get("content-type")?.includes("application/json") ? res.json() : res.text();
}

// --- Plaid Link flow -------------------------------------------------------
document.getElementById("link-btn")?.addEventListener("click", async () => {
  try {
    const { link_token } = await api("/api/link-token");
    const handler = Plaid.create({
      token: link_token,
      onSuccess: async (publicToken, metadata) => {
        await api("/api/exchange-public-token", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ public_token: publicToken, institution: metadata?.institution?.name }),
        });
        location.reload();
      },
    });
    handler.open();
  } catch (err) {
    alert("Link failed: " + err.message);
  }
});

document.getElementById("sync-btn")?.addEventListener("click", async (e) => {
  e.target.disabled = true;
  e.target.textContent = "Syncing…";
  try {
    const counts = await api("/api/sync", { method: "POST" });
    alert(`Synced ${counts.accounts} accounts, ${counts.transactions} transactions.`);
    location.reload();
  } catch (err) {
    alert("Sync failed: " + err.message);
  } finally {
    e.target.disabled = false;
    e.target.textContent = "Sync";
  }
});

// --- Dashboard charts ------------------------------------------------------
async function loadDashboard() {
  if (!document.getElementById("net-worth-chart")) return;

  const [netWorth, spending] = await Promise.all([api("/api/net-worth"), api("/api/spending")]);

  const latest = netWorth[netWorth.length - 1] || { assets: 0, liabilities: 0, net: 0, potential_net: 0 };
  document.getElementById("net-worth-value").textContent = fmt(latest.net);
  document.getElementById("potential-net-worth-value").textContent = fmt(latest.potential_net);
  document.getElementById("assets-value").textContent = fmt(latest.assets);
  document.getElementById("liabilities-value").textContent = fmt(latest.liabilities);
  const monthSpend = spending.reduce((s, c) => s + c.total, 0);
  document.getElementById("month-spend-value").textContent = fmt(monthSpend);

  new Chart(document.getElementById("net-worth-chart"), {
    type: "line",
    data: {
      labels: netWorth.map((p) => p.date),
      datasets: [
        { label: "Net", data: netWorth.map((p) => p.net), borderColor: "#4f9eff", tension: 0.25, fill: true, backgroundColor: "rgba(79,158,255,0.15)" },
        { label: "Potential Net", data: netWorth.map((p) => p.potential_net), borderColor: "#a371f7", borderDash: [6, 4], tension: 0.25 },
        { label: "Assets", data: netWorth.map((p) => p.assets), borderColor: "#2ea043", tension: 0.25 },
        { label: "Liabilities", data: netWorth.map((p) => p.liabilities), borderColor: "#f85149", tension: 0.25 },
      ],
    },
    options: { responsive: true, plugins: { legend: { labels: { color: "#e6edf3" } } }, scales: gridScales() },
  });

  new Chart(document.getElementById("spending-chart"), {
    type: "doughnut",
    data: {
      labels: spending.map((s) => s.category),
      datasets: [{ data: spending.map((s) => s.total), backgroundColor: palette(spending.length) }],
    },
    options: { responsive: true, plugins: { legend: { position: "right", labels: { color: "#e6edf3" } } } },
  });

  new Chart(document.getElementById("liabilities-chart"), {
    type: "line",
    data: {
      labels: netWorth.map((p) => p.date),
      datasets: [
        { label: "Liabilities", data: netWorth.map((p) => p.liabilities), borderColor: "#f85149", tension: 0.25, fill: true, backgroundColor: "rgba(248,81,73,0.15)" },
      ],
    },
    options: { responsive: true, plugins: { legend: { labels: { color: "#e6edf3" } } }, scales: gridScales() },
  });

  new Chart(document.getElementById("net-worth-trend-chart"), {
    type: "line",
    data: {
      labels: netWorth.map((p) => p.date),
      datasets: [
        { label: "Net Worth", data: netWorth.map((p) => p.net), borderColor: "#4f9eff", tension: 0.25, fill: true, backgroundColor: "rgba(79,158,255,0.15)" },
      ],
    },
    options: { responsive: true, plugins: { legend: { labels: { color: "#e6edf3" } } }, scales: gridScales() },
  });
}

function gridScales() {
  const grid = { color: "rgba(255,255,255,0.08)" };
  const ticks = { color: "#8b98a5" };
  return { x: { grid, ticks }, y: { grid, ticks } };
}

function palette(n) {
  const colors = ["#4f9eff", "#2ea043", "#f85149", "#d29922", "#a371f7", "#39c5cf", "#db6d28", "#58a6ff"];
  return Array.from({ length: n }, (_, i) => colors[i % colors.length]);
}

// --- Budgets page ----------------------------------------------------------
let spendingCategoriesChart = null;
let currentSpendingCategories = [];

async function loadBudgets() {
  if (!document.getElementById("spending-categories-chart")) return;

  const [progress, categories] = await Promise.all([
    api("/api/budgets"),
    api("/api/spending-categories"),
  ]);
  currentSpendingCategories = categories;

  renderSpendingCategories(categories);
  renderBudgetProgress(progress);
}

function renderSpendingCategories(categories) {
  const canvas = document.getElementById("spending-categories-chart");
  const tbody = document.querySelector("#spending-categories-table tbody");
  if (!canvas || !tbody) return;

  if (spendingCategoriesChart) spendingCategoriesChart.destroy();
  spendingCategoriesChart = new Chart(canvas, {
    type: "doughnut",
    data: {
      labels: categories.map((c) => c.category),
      datasets: [{ data: categories.map((c) => c.total), backgroundColor: palette(categories.length) }],
    },
    options: {
      responsive: true,
      plugins: { legend: { position: "right", labels: { color: "#e6edf3", boxWidth: 12, font: { size: 11 } } } },
    },
  });

  tbody.innerHTML = categories
    .map(
      (c, i) => `
      <tr data-category-index="${i}">
        <td>${c.category}</td>
        <td class="num">${fmt(c.total)}</td>
        <td class="num">${c.count}</td>
        <td><button class="btn btn-sm set-budget-btn" data-set-budget-category="${c.category}" data-set-budget-suggested="${Math.ceil(c.total / 50) * 50}">Set budget</button></td>
      </tr>`
    )
    .join("");

  // Click row → load transactions for that category
  document.querySelectorAll("#spending-categories-table tbody tr").forEach((tr) => {
    tr.addEventListener("click", (e) => {
      if (e.target.closest(".set-budget-btn")) return;
      const idx = parseInt(tr.dataset.categoryIndex, 10);
      const cat = currentSpendingCategories[idx].category;
      document.querySelectorAll("#spending-categories-table tbody tr").forEach((r) => r.classList.remove("active"));
      tr.classList.add("active");
      loadCategoryTransactions(cat);
    });
  });

  // Set-budget buttons
  document.querySelectorAll("[data-set-budget-category]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const form = document.getElementById("budget-form");
      form.elements.category.value = btn.dataset.setBudgetCategory;
      form.elements.monthly_limit.value = btn.dataset.setBudgetSuggested;
      form.elements.monthly_limit.focus();
    });
  });
}

async function loadCategoryTransactions(category) {
  const panel = document.getElementById("category-transactions-panel");
  const title = document.getElementById("category-transactions-title");
  const body = document.getElementById("category-transactions-body");
  if (!panel || !body) return;

  title.textContent = `Transactions in "${category}"`;
  body.innerHTML = '<tr><td colspan="5" class="muted">Loading…</td></tr>';
  panel.hidden = false;

  try {
    const txns = await api(
      `/api/spending-categories/${encodeURIComponent(category)}/transactions`
    );
    if (!txns.length) {
      body.innerHTML = '<tr><td colspan="5" class="muted">No transactions this month.</td></tr>';
      return;
    }
    body.innerHTML = txns
      .map(
        (t) => `
        <tr data-txn-id="${t.transaction_id}" data-merchant="${(t.merchant || t.name).replace(/"/g, "&quot;")}">
          <td>${t.date}</td>
          <td>${t.merchant || t.name}</td>
          <td class="muted">${t.account_name || ""}</td>
          <td class="num">${fmt(t.amount)}</td>
          <td><button class="btn btn-sm reclassify-btn">Reclassify</button></td>
        </tr>`
      )
      .join("");

    body.querySelectorAll(".reclassify-btn").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const tr = btn.closest("tr");
        toggleReclassifyRow(tr, category);
      });
    });
  } catch (err) {
    body.innerHTML = `<tr><td colspan="5" class="muted">Error: ${err.message}</td></tr>`;
  }
}

async function toggleReclassifyRow(tr, currentCategory) {
  // Collapse any other open reclassify row
  document.querySelectorAll(".reclassify-row").forEach((r) => r.remove());
  if (tr.dataset.reclassifyOpen === "1") {
    tr.dataset.reclassifyOpen = "0";
    return;
  }
  tr.dataset.reclassifyOpen = "1";

  const merchant = tr.dataset.merchant;
  const options = await loadCategoryOptions();
  const optHTML = options
    .map((o) => `<option value="${o}" ${o === currentCategory ? "selected" : ""}>${o}</option>`)
    .join("");

  const newRow = document.createElement("tr");
  newRow.className = "reclassify-row";
  newRow.innerHTML = `
    <td colspan="5">
      <div class="reclassify-form">
        <select class="reclassify-select">${optHTML}</select>
        <button class="btn btn-sm" data-action="txn">Just this transaction</button>
        <button class="btn btn-sm" data-action="vendor">All from "${merchant}"</button>
        <button class="btn btn-sm btn-secondary" data-action="cancel">Cancel</button>
      </div>
    </td>`;
  tr.after(newRow);

  newRow.querySelector('[data-action="cancel"]').addEventListener("click", () => {
    newRow.remove();
    tr.dataset.reclassifyOpen = "0";
  });
  newRow.querySelector('[data-action="txn"]').addEventListener("click", async () => {
    const target = newRow.querySelector(".reclassify-select").value;
    await api(`/api/transactions/${encodeURIComponent(tr.dataset.txnId)}/category`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ override_category: target }),
    });
    newRow.remove();
    tr.dataset.reclassifyOpen = "0";
    await loadBudgets();
  });
  newRow.querySelector('[data-action="vendor"]').addEventListener("click", async () => {
    const target = newRow.querySelector(".reclassify-select").value;
    await api("/api/merchant-rules", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ merchant_pattern: merchant.toLowerCase(), target_category: target }),
    });
    newRow.remove();
    tr.dataset.reclassifyOpen = "0";
    await loadBudgets();
  });
}

let _categoryOptionsCache = null;
async function loadCategoryOptions() {
  if (_categoryOptionsCache) return _categoryOptionsCache;
  _categoryOptionsCache = await api("/api/category-options");
  // Populate datalist for the vendor-rule form too
  const dl = document.getElementById("category-options-list");
  if (dl) dl.innerHTML = _categoryOptionsCache.map((o) => `<option value="${o}">`).join("");
  return _categoryOptionsCache;
}

function renderBudgetProgress(progress) {
  const list = document.getElementById("budget-progress-list");
  if (!list) return;
  if (!progress.length) {
    list.innerHTML = '<p class="muted">No budgets set yet. Click <strong>Set budget</strong> on a spend row, or fill in the form above.</p>';
    return;
  }
  list.innerHTML = progress
    .map((b) => {
      const pct = Math.min(b.pct_used, 100);
      const over = b.pct_used > 100;
      return `
        <div class="budget-bar ${over ? "over" : ""}">
          <div class="label">
            <span>${b.category} <button class="btn btn-sm btn-danger" data-delete-budget="${b.category}">Delete</button></span>
            <span>${fmt(b.spent_so_far)} / ${fmt(b.monthly_limit)} (${b.pct_used.toFixed(0)}%)</span>
          </div>
          <div class="track"><div class="fill" style="width:${pct}%"></div></div>
        </div>`;
    })
    .join("");

  document.querySelectorAll("[data-delete-budget]").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      await api(`/api/budgets/${encodeURIComponent(btn.dataset.deleteBudget)}`, { method: "DELETE" });
      loadBudgets();
    });
  });
}

// --- Merchant rules --------------------------------------------------------
async function loadMerchantRules() {
  const list = document.getElementById("merchant-rules-list");
  if (!list) return;
  const rules = await api("/api/merchant-rules");
  if (!rules.length) {
    list.innerHTML = '<p class="muted">No vendor rules yet.</p>';
    return;
  }
  list.innerHTML = `<table class="accounts-table"><thead><tr><th>Vendor</th><th>Category</th><th></th></tr></thead><tbody>${rules
    .map(
      (r) => `<tr>
        <td>${r.merchant_pattern}</td>
        <td>${r.target_category}</td>
        <td><button class="btn btn-sm btn-danger" data-delete-rule="${r.merchant_pattern}">Delete</button></td>
      </tr>`
    )
    .join("")}</tbody></table>`;

  list.querySelectorAll("[data-delete-rule]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await api(`/api/merchant-rules/${encodeURIComponent(btn.dataset.deleteRule)}`, { method: "DELETE" });
      loadBudgets();
      loadMerchantRules();
    });
  });
}

document.getElementById("budget-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  await api("/api/budgets", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ category: form.get("category"), monthly_limit: parseFloat(form.get("monthly_limit")) }),
  });
  e.target.reset();
  loadBudgets();
});

document.getElementById("merchant-rule-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  await api("/api/merchant-rules", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      merchant_pattern: form.get("merchant_pattern"),
      target_category: form.get("target_category"),
    }),
  });
  e.target.reset();
  loadBudgets();
  loadMerchantRules();
});

// --- Subscription detection -----------------------------------------------
async function loadSubscriptions() {
  const list = document.getElementById("subscriptions-list");
  if (!list) return;
  const subs = await api("/api/subscriptions");
  if (!subs.length) {
    list.innerHTML = '<p class="muted">No recurring charges detected yet. Sync more transactions to improve detection.</p>';
    return;
  }
  list.innerHTML = `<table class="accounts-table"><thead><tr>
    <th>Merchant</th><th class="num">Amount</th><th class="num">Times</th><th>Last seen</th><th>Category</th>
  </tr></thead><tbody>${subs
    .map(
      (s) => `<tr>
        <td>${s.label}</td>
        <td class="num">${fmt(s.amount)}</td>
        <td class="num">${s.occurrences}</td>
        <td>${s.last_date}</td>
        <td class="muted">${s.category}</td>
      </tr>`
    )
    .join("")}</tbody></table>`;
}

loadDashboard();
loadBudgets();
loadMerchantRules();
loadCategoryOptions();
loadSubscriptions();

// --- Portfolio page --------------------------------------------------------
let portfolioChart = null;
let currentPortfolioSectors = [];

async function loadPortfolio() {
  const canvas = document.getElementById("portfolio-chart");
  if (!canvas) return;

  const [sectors, options] = await Promise.all([
    api("/api/portfolio"),
    api("/api/sector-options"),
  ]);
  currentPortfolioSectors = sectors;
  _sectorOptionsCache = options;

  const tbody = document.querySelector("#portfolio-sectors-table tbody");
  if (portfolioChart) portfolioChart.destroy();

  portfolioChart = new Chart(canvas, {
    type: "doughnut",
    data: {
      labels: sectors.map((s) => s.sector),
      datasets: [{ data: sectors.map((s) => s.total), backgroundColor: palette(sectors.length) }],
    },
    options: {
      responsive: true,
      plugins: { legend: { position: "right", labels: { color: "#e6edf3", boxWidth: 12, font: { size: 11 } } } },
    },
  });

  tbody.innerHTML = sectors
    .map(
      (s, i) => `
      <tr data-sector-index="${i}">
        <td>${s.sector}</td>
        <td class="num">${fmt(s.total)}</td>
        <td class="num">${s.asset_count}</td>
        <td></td>
      </tr>`
    )
    .join("");

  document.querySelectorAll("#portfolio-sectors-table tbody tr").forEach((tr) => {
    tr.addEventListener("click", () => {
      const idx = parseInt(tr.dataset.sectorIndex, 10);
      const sec = currentPortfolioSectors[idx].sector;
      document.querySelectorAll("#portfolio-sectors-table tbody tr").forEach((r) => r.classList.remove("active"));
      tr.classList.add("active");
      loadSectorAssets(sec);
    });
  });
}

let _sectorOptionsCache = null;
async function loadSectorOptions() {
  if (_sectorOptionsCache) return _sectorOptionsCache;
  _sectorOptionsCache = await api("/api/sector-options");
  return _sectorOptionsCache;
}

async function loadSectorAssets(sector) {
  const panel = document.getElementById("sector-assets-panel");
  const title = document.getElementById("sector-assets-title");
  const body = document.getElementById("sector-assets-body");
  if (!panel || !body) return;

  title.textContent = `Assets in "${sector}"`;
  body.innerHTML = '<tr><td colspan="6" class="muted">Loading…</td></tr>';
  panel.hidden = false;

  try {
    const assets = await api(`/api/portfolio/${encodeURIComponent(sector)}/assets`);
    if (!assets.length) {
      body.innerHTML = '<tr><td colspan="6" class="muted">No assets in this sector.</td></tr>';
      return;
    }
    body.innerHTML = assets
      .map(
        (a) => `
        <tr data-account-id="${a.account_id}" data-security-id="${a.security_id || ""}" data-name="${(a.name || a.ticker || "").replace(/"/g, "&quot;")}">
          <td>${a.ticker || "—"}</td>
          <td>${a.name}</td>
          <td class="muted">${a.account_name || ""}${a.institution ? ` (${a.institution})` : ""}</td>
          <td class="num">${fmt(a.value)}</td>
          <td class="num">${a.quantity != null ? a.quantity.toFixed(4) : "—"}</td>
          <td><button class="btn btn-sm reclassify-sector-btn">Reclassify</button></td>
        </tr>`
      )
      .join("");

    body.querySelectorAll(".reclassify-sector-btn").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const tr = btn.closest("tr");
        toggleReclassifySectorRow(tr, sector);
      });
    });
  } catch (err) {
    body.innerHTML = `<tr><td colspan="6" class="muted">Error: ${err.message}</td></tr>`;
  }
}

async function toggleReclassifySectorRow(tr, currentSector) {
  document.querySelectorAll(".reclassify-sector-row").forEach((r) => r.remove());
  if (tr.dataset.reclassifyOpen === "1") {
    tr.dataset.reclassifyOpen = "0";
    return;
  }
  tr.dataset.reclassifyOpen = "1";

  const securityId = tr.dataset.securityId || null;
  const accountId = tr.dataset.accountId;
  const name = tr.dataset.name;

  if (!securityId) {
    alert(`"${name}" is a cash account and can't be reclassified to another sector.`);
    tr.dataset.reclassifyOpen = "0";
    return;
  }

  const options = await loadSectorOptions();
  const optHTML = options
    .map((o) => `<option value="${o}" ${o === currentSector ? "selected" : ""}>${o}</option>`)
    .join("");

  const newRow = document.createElement("tr");
  newRow.className = "reclassify-sector-row";
  newRow.innerHTML = `
    <td colspan="6">
      <div class="reclassify-form">
        <select class="reclassify-select">${optHTML}</select>
        <button class="btn btn-sm" data-action="save">Save</button>
        <button class="btn btn-sm btn-secondary" data-action="cancel">Cancel</button>
      </div>
    </td>`;
  tr.after(newRow);

  newRow.querySelector('[data-action="cancel"]').addEventListener("click", () => {
    newRow.remove();
    tr.dataset.reclassifyOpen = "0";
  });
  newRow.querySelector('[data-action="save"]').addEventListener("click", async () => {
    const target = newRow.querySelector(".reclassify-select").value;
    await api("/api/portfolio/assets/sector", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ account_id: accountId, security_id: securityId, sector: target }),
    });
    newRow.remove();
    tr.dataset.reclassifyOpen = "0";
    await loadPortfolio();
  });
}

// Portfolio-specific Plaid Link button (separate from the dashboard's)
document.getElementById("portfolio-link-btn")?.addEventListener("click", async () => {
  try {
    const { link_token } = await api("/api/link-token");
    const handler = Plaid.create({
      token: link_token,
      onSuccess: async (publicToken, metadata) => {
        await api("/api/exchange-public-token", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ public_token: publicToken, institution: metadata?.institution?.name }),
        });
        location.reload();
      },
    });
    handler.open();
  } catch (err) {
    alert("Link failed: " + err.message);
  }
});

// Portfolio item unlink buttons
document.querySelectorAll("[data-unlink-item]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    if (!confirm("Remove this linked item and all its accounts/holdings?")) return;
    await api(`/api/items/${encodeURIComponent(btn.dataset.unlinkItem)}`, { method: "DELETE" });
    location.reload();
  });
});

// --- 401(k) plan allocations ----------------------------------------------
async function loadPlanAllocations() {
  const list = document.getElementById("plan-allocations-list");
  if (!list) return;

  const [allocs, sectors] = await Promise.all([
    api("/api/plan-allocations"),
    api("/api/sector-options"),
  ]);

  // Populate the sector datalist.
  const dl = document.getElementById("sector-options-list");
  if (dl) dl.innerHTML = sectors.map((s) => `<option value="${s}">`).join("");

  if (!allocs.length) {
    list.innerHTML = '<p class="muted">No 401(k) allocations yet. Add funds below.</p>';
    return;
  }

  // Group by account.
  const byAccount = {};
  allocs.forEach((a) => {
    if (!byAccount[a.account_id]) byAccount[a.account_id] = [];
    byAccount[a.account_id].push(a);
  });

  list.innerHTML = Object.entries(byAccount)
    .map(([aid, rows]) => {
      const bal = rows[0].balance || 0;
      const totalPct = rows.reduce((s, r) => s + r.allocation_pct, 0);
      return `
        <div class="plan-account">
          <div class="label">
            <strong>${rows[0].account_name}</strong>
            <span class="muted">(${rows[0].institution}) — Balance: ${fmt(bal)}</span>
          </div>
          <table class="accounts-table">
            <thead><tr><th>Fund</th><th>Ticker</th><th class="num">%</th><th>Sector</th><th class="num">Value</th><th></th></tr></thead>
            <tbody>
              ${rows
                .map(
                  (r) => `<tr data-account-id="${r.account_id}" data-label="${r.label.replace(/"/g, "&quot;")}">
                    <td>${r.label}</td>
                    <td>${r.ticker || "—"}</td>
                    <td class="num">${r.allocation_pct.toFixed(2)}%</td>
                    <td class="muted">${r.sector}</td>
                    <td class="num">${fmt(bal * r.allocation_pct / 100)}</td>
                    <td><button class="btn btn-sm btn-danger" data-delete-alloc='{"account_id":"${r.account_id}","label":"${r.label.replace(/"/g, "&quot;")}"}'>Delete</button></td>
                  </tr>`
                )
                .join("")}
              <tr class="muted">
                <td colspan="2">Total</td>
                <td class="num">${totalPct.toFixed(2)}%</td>
                <td colspan="2">${totalPct < 100 ? `${(100 - totalPct).toFixed(2)}% unallocated` : ""}</td>
                <td></td>
              </tr>
            </tbody>
          </table>
        </div>`;
    })
    .join("");

  list.querySelectorAll("[data-delete-alloc]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const { account_id, label } = JSON.parse(btn.dataset.deleteAlloc);
      await api(`/api/plan-allocations/${encodeURIComponent(account_id)}/${encodeURIComponent(label)}`, {
        method: "DELETE",
      });
      await loadPlanAllocations();
      await loadPortfolio();
    });
  });
}

document.getElementById("plan-allocation-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  await api("/api/plan-allocations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      account_id: form.get("account_id"),
      label: form.get("label"),
      ticker: form.get("ticker") || null,
      allocation_pct: parseFloat(form.get("allocation_pct")),
      sector: form.get("sector"),
    }),
  });
  e.target.reset();
  await loadPlanAllocations();
  await loadPortfolio();
});

loadPortfolio();
loadPlanAllocations();

// --- Debt Tracker page -----------------------------------------------------
let debtChart = null;
let debtBalanceChart = null;
let currentLoans = [];

async function loadDebtSummary() {
  if (!document.getElementById("debt-total-balance")) return;
  const s = await api("/api/debt/summary");
  document.getElementById("debt-total-balance").textContent = fmt(s.total_balance);
  document.getElementById("debt-num-loans").textContent = `${s.num_loans} loan${s.num_loans === 1 ? "" : "s"}`;
  document.getElementById("debt-total-min").textContent = fmt(s.total_min_payment);
  document.getElementById("debt-payoff-date").textContent =
    s.projected_payoff_date || "—";
  const monthsNote = s.projected_payoff_months
    ? `${s.projected_payoff_months} months at minimums`
    : (s.unprojectable.length ? "enter rate + balance + min" : "at minimums only");
  document.getElementById("debt-payoff-months").textContent = monthsNote;
  document.getElementById("debt-projected-interest").textContent =
    s.projected_total_interest != null ? fmt(s.projected_total_interest) : "—";
}

function populateLoanSelects() {
  const selects = [
    document.querySelector('form#loan-payment-form select[name="loan_id"]'),
  ].filter(Boolean);
  for (const sel of selects) {
    const cur = sel.value;
    sel.innerHTML = '<option value="">Combined (all loans)</option>' +
      currentLoans
        .map((l) => `<option value="${l.loan_id}">${l.name}</option>`)
        .join("");
    if (cur) sel.value = cur;
  }
}

function renderLoansTable() {
  const tbody = document.querySelector("#debt-loans-table tbody");
  if (!tbody) return;
  tbody.innerHTML = currentLoans.map((l) => `
    <tr>
      <td>${escapeHtml(l.name)}${l.notes ? `<br><small class="muted">${escapeHtml(l.notes)}</small>` : ""}</td>
      <td>${escapeHtml(l.loan_type || "—")}</td>
      <td>${l.interest_rate != null ? `${l.interest_rate}%` : "—"}</td>
      <td class="num">${l.min_payment != null ? fmt(l.min_payment) : "—"}</td>
      <td class="num negative">${l.current_balance != null ? fmt(l.current_balance) : "—"}</td>
      <td>${l.next_due_date || "—"}</td>
      <td>${escapeHtml(l.status)}</td>
      <td>${l.auto_pay ? "Yes" : "No"}</td>
      <td>
        <button class="btn btn-sm btn-secondary" data-edit-loan="${l.loan_id}">Edit</button>
        <button class="btn btn-sm btn-danger" data-del-loan="${l.loan_id}">Del</button>
      </td>
    </tr>
  `).join("");
}

function escapeHtml(s) {
  return String(s || "").replace(/[&<>"']/g, (c) =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
}

async function loadLoans() {
  if (!document.getElementById("debt-loans-table")) return;
  currentLoans = await api("/api/loans");
  renderLoansTable();
  populateLoanSelects();
}

async function loadLoanPayments() {
  const tbody = document.querySelector("#debt-payments-table tbody");
  if (!tbody) return;
  const payments = await api("/api/loan-payments");
  const loanName = (id) => (id ? (currentLoans.find((l) => l.loan_id === id)?.name || "—") : "Combined");
  tbody.innerHTML = payments.map((p) => `
    <tr>
      <td>${p.payment_date}</td>
      <td class="num">${fmt(p.amount)}</td>
      <td>${escapeHtml(loanName(p.loan_id))}</td>
      <td>${escapeHtml(p.status)}</td>
      <td>${escapeHtml(p.source || "—")}</td>
      <td><button class="btn btn-sm btn-danger" data-del-payment="${p.payment_id}">Del</button></td>
    </tr>
  `).join("");
}

async function loadDebtBalanceChart() {
  const canvas = document.getElementById("debt-balance-chart");
  if (!canvas) return;
  const history = await api("/api/debt/balance-history");
  const labels = history.map((h) => h.date);
  const data = history.map((h) => h.total);
  if (debtBalanceChart) debtBalanceChart.destroy();
  debtBalanceChart = new Chart(canvas, {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "Total balance",
          data,
          borderColor: "#4f9eff",
          backgroundColor: "rgba(79,158,255,0.1)",
          fill: true,
          tension: 0.2,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { labels: { color: "#e6edf3" } } },
      scales: gridScales(),
    },
  });
}

async function loadDebtChart(extraMonthly, extraOnetime) {
  const canvas = document.getElementById("debt-chart");
  if (!canvas) return;
  const [history, projection] = await Promise.all([
    api("/api/debt/balance-history"),
    api(
      `/api/debt/projection?extra_monthly=${encodeURIComponent(extraMonthly || 0)}` +
        `&extra_onetime=${encodeURIComponent(extraOnetime || 0)}`
    ),
  ]);

  const histLabels = history.map((h) => h.date);
  const histData = history.map((h) => h.total);

  // Project forward from the last historical point (or today if none).
  const projLabels = projection.schedule.map((p) => p.date);
  const projData = projection.schedule.map((p) => p.total_balance);

  // Bridge: prepend the last actual point so the dashed line connects.
  let bridgeLabel = null, bridgeVal = null;
  if (history.length && projLabels.length) {
    bridgeLabel = history[history.length - 1].date;
    bridgeVal = history[history.length - 1].total;
  } else if (projLabels.length) {
    bridgeLabel = projLabels[0];
    bridgeVal = projData[0];
  }

  const labels = histLabels.concat(projLabels);
  const histSeries = histData.concat(Array(projLabels.length).fill(null));
  const projSeries = Array(histLabels.length - (bridgeLabel ? 1 : 0))
    .fill(null)
    .concat(bridgeVal ? [bridgeVal] : [])
    .concat(projData);

  if (debtChart) debtChart.destroy();
  debtChart = new Chart(canvas, {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "Actual balance",
          data: histSeries,
          borderColor: "#4f9eff",
          backgroundColor: "rgba(79,158,255,0.1)",
          fill: true,
          tension: 0.2,
        },
        {
          label: "Projected payoff",
          data: projSeries,
          borderColor: "#2ea043",
          borderDash: [6, 4],
          fill: false,
          tension: 0.2,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { labels: { color: "#e6edf3" } } },
      scales: {
        x: {
          grid: { color: "rgba(255,255,255,0.08)" },
          ticks: {
            color: "#8b98a5",
            autoSkip: false,
            callback: function (value, index) {
              const lbl = this.getLabelForValue(value) || "";
              const yr = parseInt(lbl.slice(0, 4), 10);
              if (Number.isNaN(yr)) return "";
              if (index === 0) return String(yr);       // leftmost = this year
              return yr % 2 === 0 ? String(yr) : "";     // label even years only
            },
          },
          // Keep only one tick per year (the first month of that year present
          // in the data) so vertical hashes align to yearly boundaries and we
          // don't draw a hash for every monthly data point.
          afterBuildTicks: (axis) => {
            const all = axis.chart.data.labels || [];
            if (!all.length) return;
            const ticks = [];
            let lastYear = null;
            for (let i = 0; i < all.length; i++) {
              const yr = parseInt(String(all[i]).slice(0, 4), 10);
              if (Number.isNaN(yr)) continue;
              if (yr === lastYear) continue;
              lastYear = yr;
              ticks.push({ value: i, label: String(all[i]) });
            }
            axis.ticks = ticks;
          },
        },
        y: { grid: { color: "rgba(255,255,255,0.08)" }, ticks: { color: "#8b98a5" } },
      },
      parsing: { xAxisKey: "x", yAxisKey: "y" },
    },
  });

  const note = document.getElementById("debt-projection-note");
  if (note) {
    if (projection.months_to_payoff != null) {
      const yrs = (projection.months_to_payoff / 12).toFixed(1);
      note.textContent = `Paid off in ${projection.months_to_payoff} months (~${yrs} yrs) — ${projection.payoff_date}. Interest: ${fmt(projection.total_interest)}.`;
    } else if (projection.unprojectable.length) {
      note.textContent = `Cannot project — missing rate/balance/min on: ${projection.unprojectable.join(", ")}.`;
    } else {
      note.textContent = "Beyond 50-year horizon at current payment.";
    }
  }
}

async function loadDebt() {
  if (!document.getElementById("debt-chart")) return;
  await loadLoans();
  await Promise.all([
    loadDebtSummary(),
    loadLoanPayments(),
    loadDebtChart(0),
    loadDebtBalanceChart(),
  ]);
}

// Loan form submit (add / edit)
document.getElementById("debt-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  const body = {
    loan_id: form.get("loan_id") || null,
    name: form.get("name"),
    loan_type: form.get("loan_type") || null,
    interest_rate: form.get("interest_rate") ? parseFloat(form.get("interest_rate")) : null,
    min_payment: form.get("min_payment") ? parseFloat(form.get("min_payment")) : null,
    current_balance: form.get("current_balance") ? parseFloat(form.get("current_balance")) : null,
    next_due_date: form.get("next_due_date") || null,
    auto_pay: form.get("auto_pay") === "on",
    status: form.get("status"),
    notes: form.get("notes") || null,
  };
  await api("/api/loans", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  resetLoanForm();
  await loadDebt();
});

document.getElementById("debt-form-cancel")?.addEventListener("click", resetLoanForm);

function resetLoanForm() {
  const form = document.getElementById("debt-form");
  if (!form) return;
  form.reset();
  form.querySelector('[name="loan_id"]').value = "";
  document.getElementById("debt-form-title").textContent = "Add Loan";
  document.getElementById("debt-form-cancel").hidden = true;
}

document.querySelector("#debt-loans-table")?.addEventListener("click", async (e) => {
  const editId = e.target.getAttribute("data-edit-loan");
  const delId = e.target.getAttribute("data-del-loan");
  if (editId) {
    const l = currentLoans.find((x) => x.loan_id === editId);
    if (!l) return;
    const form = document.getElementById("debt-form");
    form.querySelector('[name="loan_id"]').value = l.loan_id;
    form.querySelector('[name="name"]').value = l.name;
    form.querySelector('[name="loan_type"]').value = l.loan_type || "";
    form.querySelector('[name="interest_rate"]').value = l.interest_rate ?? "";
    form.querySelector('[name="min_payment"]').value = l.min_payment ?? "";
    form.querySelector('[name="current_balance"]').value = l.current_balance ?? "";
    form.querySelector('[name="next_due_date"]').value = l.next_due_date || "";
    form.querySelector('[name="auto_pay"]').checked = !!l.auto_pay;
    form.querySelector('[name="status"]').value = l.status;
    form.querySelector('[name="notes"]').value = l.notes || "";
    document.getElementById("debt-form-title").textContent = "Edit Loan";
    document.getElementById("debt-form-cancel").hidden = false;
    window.scrollTo({ top: 0, behavior: "smooth" });
  } else if (delId) {
    if (!confirm("Delete this loan and all its payments/snapshots?")) return;
    await api(`/api/loans/${delId}`, { method: "DELETE" });
    await loadDebt();
  }
});

// Payment form submit
document.getElementById("loan-payment-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  const body = {
    loan_id: form.get("loan_id") || null,
    payment_date: form.get("payment_date"),
    amount: parseFloat(form.get("amount")),
    status: form.get("status"),
    source: form.get("source") || null,
    notes: null,
    new_balance: form.get("new_balance") ? parseFloat(form.get("new_balance")) : null,
  };
  await api("/api/loan-payments", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  e.target.reset();
  await loadDebt();
});

document.querySelector("#debt-payments-table")?.addEventListener("click", async (e) => {
  const delId = e.target.getAttribute("data-del-payment");
  if (!delId) return;
  if (!confirm("Delete this payment record?")) return;
  await api(`/api/loan-payments/${delId}`, { method: "DELETE" });
  await loadDebt();
});

// Re-project button
document.getElementById("debt-reproject")?.addEventListener("click", async () => {
  const extra = parseFloat(document.getElementById("debt-extra").value || "0");
  const onetime = parseFloat(
    document.getElementById("debt-extra-onetime")?.value || "0"
  );
  await loadDebtChart(extra, onetime);
});

loadDebt();
