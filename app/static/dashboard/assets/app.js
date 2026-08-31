(function () {
  "use strict";

  const state = {
    accounts: [],
    activeAccountId: null,
    issues: [],
    jobs: [],
    cache: {}, // accountId -> { issues, jobs }, shown instantly while a fresh fetch runs
    loadingAccountId: null,
  };

  const tabsEl = document.getElementById("tabs");
  const panelEl = document.getElementById("panel");
  const modalEl = document.getElementById("add-account-modal");
  const tokenInput = document.getElementById("add-account-token");
  const addErrorEl = document.getElementById("add-account-error");
  const aiSettingsModal = document.getElementById("ai-settings-modal");
  const aiProviderSelect = document.getElementById("ai-provider");
  const aiKeyInput = document.getElementById("ai-api-key");
  const aiModelSelect = document.getElementById("ai-model");
  const aiStatusEl = document.getElementById("ai-status");
  const aiErrorEl = document.getElementById("ai-error");
  const aiRemoveBtn = document.getElementById("ai-remove");
  const aiTestBtn = document.getElementById("ai-test");
  let providerCatalog = [];

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function safeHref(url) {
    return typeof url === "string" && url.startsWith("https://") ? url : "#";
  }

  /**
   * Run an async action while the button shows a spinner, so a slow request
   * never leaves the button looking stagnant. The button keeps its original
   * width to stop the row from jumping, and is restored afterwards even on
   * failure. Re-entrant clicks are ignored while one is in flight.
   */
  async function withBusy(btn, busyLabel, action) {
    if (!btn) return action();
    if (btn.dataset.busy === "1") return;
    const original = btn.innerHTML;
    const originalWidth = btn.offsetWidth;
    btn.dataset.busy = "1";
    btn.disabled = true;
    btn.classList.add("is-busy");
    btn.style.minWidth = `${originalWidth}px`;
    btn.innerHTML = `<span class="spinner"></span>${busyLabel ? esc(busyLabel) : ""}`;
    try {
      return await action();
    } finally {
      // The panel often re-renders on success, detaching this node; restoring
      // a detached button is harmless, and matters when it survives.
      btn.dataset.busy = "";
      btn.disabled = false;
      btn.classList.remove("is-busy");
      btn.style.minWidth = "";
      btn.innerHTML = original;
    }
  }

  async function api(path, options) {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    if (response.status === 401) {
      window.location.href = "/dashboard/login";
      return new Promise(() => {}); // navigation is in flight; stop this caller here
    }
    if (!response.ok) {
      let detail = response.statusText;
      try {
        const body = await response.json();
        detail = body.detail || detail;
      } catch (_) { /* no JSON body */ }
      throw new Error(detail);
    }
    if (response.status === 204) return null;
    return response.json();
  }

  function statusBadge(status) {
    const map = {
      NOT_QUEUED: ["", "Not queued"],
      QUEUED: ["badge-accent", "Queued"],
      PROCESSING: ["badge-accent", "Processing"],
      WAITING_CI: ["badge-warn", "Waiting on CI"],
      DONE: ["badge-ok", "Done"],
      FAILED: ["badge-danger", "Failed"],
      SKIPPED: ["badge-danger", "Skipped"],
      SKIPPED_BY_USER: ["", "Skipped"],
      NEEDS_REVIEW: ["badge-warn", "Needs attention"],
      NEEDS_API_KEY: ["badge-danger", "AI key required"],
      NEEDS_TESTS: ["badge-warn", "No CI checks"],
    };
    const [cls, label] = map[status] || ["", status];
    return `<span class="badge ${cls}">${esc(label)}</span>`;
  }

  async function loadAccounts() {
    state.accounts = await api("/api/accounts");
    if (!state.activeAccountId && state.accounts.length) {
      state.activeAccountId = state.accounts[0].id;
    }
    if (state.activeAccountId && !state.accounts.some((a) => a.id === state.activeAccountId)) {
      state.activeAccountId = state.accounts[0] ? state.accounts[0].id : null;
    }
    renderTabs();
    await refreshPanel();
  }

  function renderTabs() {
    document.getElementById("account-count").textContent = state.accounts.length;
    tabsEl.innerHTML = state.accounts.map((account) => `
      <div class="account-item ${account.id === state.activeAccountId ? "active" : ""}"
           data-id="${account.id}">
        <span class="account-avatar">${esc(account.github_username.slice(0, 2).toUpperCase())}</span>
        <span class="account-copy"><strong>@${esc(account.github_username)}</strong><small>GitHub connected</small></span>
        <button class="account-remove" data-remove="${account.id}" title="Remove account" aria-label="Remove @${esc(account.github_username)}">&times;</button>
      </div>
    `).join("");

    if (!state.accounts.length) {
      tabsEl.innerHTML = `<div class="account-list-empty">No accounts connected yet.</div>`;
    }

    tabsEl.querySelectorAll(".account-item").forEach((el) => {
      el.addEventListener("click", (event) => {
        if (event.target.dataset.remove) return;
        activateAccount(Number(el.dataset.id));
      });
    });
    tabsEl.querySelectorAll("[data-remove]").forEach((el) => {
      el.addEventListener("click", (event) => {
        event.stopPropagation();
        const id = Number(el.dataset.remove);
        if (!confirm("Remove this account? This does not close any GitHub issues.")) return;
        withBusy(el, "", async () => {
          try {
            await api(`/api/accounts/${id}`, { method: "DELETE" });
            delete state.cache[id];
            await loadAccounts();
          } catch (err) {
            alert(err.message);
          }
        });
      });
    });
  }

  function activateAccount(accountId) {
    state.activeAccountId = accountId;
    renderTabs();
    const cached = state.cache[accountId];
    if (cached) {
      state.issues = cached.issues;
      state.jobs = cached.jobs;
      renderPanel();
    } else {
      renderLoadingState();
    }
    refreshPanel();
  }

  function renderLoadingState() {
    panelEl.innerHTML = `
      <div class="panel-header"><h2>Loading…</h2></div>
      <div class="skeleton">
        <div class="skeleton-row"></div>
        <div class="skeleton-row"></div>
        <div class="skeleton-row"></div>
        <div class="skeleton-row"></div>
      </div>
    `;
  }

  async function refreshPanel() {
    if (!state.activeAccountId) {
      panelEl.innerHTML = `<p class="empty">Add a GitHub account to see its assigned issues.</p>`;
      return;
    }
    const accountId = state.activeAccountId;
    if (state.loadingAccountId === accountId) return;
    const hadCache = Boolean(state.cache[accountId]);
    state.loadingAccountId = accountId;
    try {
      const [issues, jobs] = await Promise.all([
        api(`/api/accounts/${accountId}/issues`),
        api(`/api/accounts/${accountId}/jobs`),
      ]);
      state.cache[accountId] = { issues, jobs };
      if (state.activeAccountId !== accountId) return; // user switched tabs meanwhile
      state.issues = issues;
      state.jobs = jobs;
      renderPanel();
    } catch (err) {
      if (state.activeAccountId === accountId && !hadCache) {
        panelEl.innerHTML = `<p class="error">${esc(err.message)}</p>`;
      }
    } finally {
      if (state.loadingAccountId === accountId) state.loadingAccountId = null;
    }
  }

  function renderPanel() {
    const activeAccount = state.accounts.find((account) => account.id === state.activeAccountId);
    const activeStatuses = new Set(["QUEUED", "PROCESSING", "WAITING_CI"]);
    const attentionStatuses = new Set(["FAILED", "NEEDS_REVIEW", "NEEDS_API_KEY", "NEEDS_TESTS"]);
    const solvedCount = state.jobs.filter((job) => job.status === "DONE").length;
    const activeCount = state.jobs.filter((job) => activeStatuses.has(job.status)).length;
    const attentionCount = state.jobs.filter((job) => attentionStatuses.has(job.status)).length;
    const prCount = state.jobs.filter((job) => Boolean(job.pr_url)).length;
    const issueRows = state.issues.map((issue) => {
      const labels = (issue.labels || [])
        .map((label) => `<span class="label-chip">${esc(label)}</span>`)
        .join("");
      const canFix = issue.status === "NOT_QUEUED" || (issue.status === "FAILED" && !issue.pr_url) ||
        (issue.status === "NEEDS_REVIEW" && !issue.pr_url);
      const canSkip = issue.status === "NOT_QUEUED";
      const canUnskip = issue.status === "SKIPPED_BY_USER";
      const canMarkReady = Boolean(issue.pr_url) && issue.status !== "DONE";
      const canRecheck = Boolean(issue.pr_url) && !activeStatuses.has(issue.status);
      const prLink = issue.pr_url
        ? `<a class="pr-link" href="${esc(safeHref(issue.pr_url))}" target="_blank" rel="noopener">Open PR ↗</a>` : "";
      const readyBtn = canMarkReady
        ? `<button class="btn btn-ready" data-ready="${esc(issue.repo)}|${issue.number}">Enable Ready for PR</button>` : "";
      const recheckBtn = canRecheck
        ? `<button class="btn btn-recheck" data-recheck="${esc(issue.repo)}|${issue.number}" title="Check the current PR and repair it automatically if CI is failing">Recheck PR</button>` : "";
      return `
        <tr>
          <td><a class="issue-link" href="${esc(safeHref(issue.url))}" target="_blank" rel="noopener">${esc(issue.title)}</a><span class="issue-number">#${esc(issue.number)}</span></td>
          <td><span class="repo-name">${esc(issue.repo)}</span></td>
          <td class="labels">${labels}</td>
          <td>${statusBadge(issue.status)} ${prLink}</td>
          <td class="row-actions">
            ${canFix ? `<button class="btn btn-primary" data-fix="${esc(issue.repo)}|${issue.number}|${esc(issue.title)}|${esc(issue.url)}">Fix</button>` : ""}
            ${recheckBtn}
            ${readyBtn}
            ${canSkip ? `<button class="btn" data-skip="${esc(issue.repo)}|${issue.number}|${esc(issue.title)}|${esc(issue.url)}">Skip</button>` : ""}
            ${canUnskip ? `<button class="btn" data-unskip="${esc(issue.repo)}|${issue.number}">Unskip</button>` : ""}
          </td>
        </tr>`;
    }).join("");

    const jobRows = state.jobs.map((job) => {
      const canMarkReady = Boolean(job.pr_url) && job.status !== "DONE";
      const canRecheck = Boolean(job.pr_url) && !activeStatuses.has(job.status);
      const readyBtn = canMarkReady
        ? `<button class="btn btn-ready" data-ready="${esc(job.repo)}|${job.number}">Enable Ready for PR</button>` : "";
      const recheckBtn = canRecheck
        ? `<button class="btn btn-recheck" data-recheck="${esc(job.repo)}|${job.number}" title="Check the current PR and repair it automatically if CI is failing">Recheck PR</button>` : "";
      return `
      <tr>
        <td><a class="issue-link" href="${esc(safeHref(job.issue_url))}" target="_blank" rel="noopener">${esc(job.title)}</a><span class="issue-number">#${esc(job.number)}</span></td>
        <td><span class="repo-name">${esc(job.repo)}</span></td>
        <td>${statusBadge(job.status)}</td>
        <td>${job.pr_url ? `<a class="pr-link" href="${esc(safeHref(job.pr_url))}" target="_blank" rel="noopener">Open PR ↗</a>` : "—"}</td>
        <td>${esc((job.last_error || "").slice(0, 140))}</td>
        <td class="row-actions">${recheckBtn}${readyBtn}</td>
      </tr>`;
    }).join("");

    panelEl.innerHTML = `
      <div class="workspace-heading">
        <div><span class="eyebrow">ACTIVE WORKSPACE</span><h2>@${esc(activeAccount ? activeAccount.github_username : "GitHub")}</h2><p>Assigned issues and solver progress update automatically.</p></div>
        <div class="actions">
          <button id="refresh-btn" class="btn">Refresh</button>
          <button id="fix-all-btn" class="btn btn-primary">Fix all assigned</button>
        </div>
      </div>
      <section class="stats-grid">
        <div class="stat-card"><span>Assigned</span><strong>${state.issues.length}</strong><small>open GitHub issues</small></div>
        <div class="stat-card stat-blue"><span>In progress</span><strong>${activeCount}</strong><small>queued, solving, or checking</small></div>
        <div class="stat-card stat-green"><span>Solved</span><strong>${solvedCount}</strong><small>ready pull requests</small></div>
        <div class="stat-card stat-amber"><span>Needs attention</span><strong>${attentionCount}</strong><small>${prCount} total PR${prCount === 1 ? "" : "s"} created</small></div>
      </section>
      <section class="data-section">
        <div class="section-heading"><div><h3>Assigned issues</h3><p>Choose what the solver should work on.</p></div><span class="count-label">${state.issues.length} issues</span></div>
        <div class="table-wrap"><table>
          <thead><tr><th>Issue</th><th>Repository</th><th>Labels</th><th>Status</th><th>Actions</th></tr></thead>
          <tbody>${issueRows || `<tr><td colspan="5"><div class="empty-state compact"><strong>No assigned issues</strong><span>New assigned issues will appear here.</span></div></td></tr>`}</tbody>
        </table></div>
      </section>
      <section class="data-section">
        <div class="section-heading"><div><h3>Solver history</h3><p>Recheck a PR after upstream merges; failed CI will return to automatic repair.</p></div><span class="count-label">${state.jobs.length} jobs</span></div>
        <div class="table-wrap"><table>
          <thead><tr><th>Issue</th><th>Repository</th><th>Status</th><th>Pull request</th><th>Latest note</th><th>Action</th></tr></thead>
          <tbody>${jobRows || `<tr><td colspan="6"><div class="empty-state compact"><strong>No solver jobs yet</strong><span>Click Fix on an issue to start.</span></div></td></tr>`}</tbody>
        </table></div>
      </section>
    `;

    const refreshBtn = document.getElementById("refresh-btn");
    refreshBtn.addEventListener("click", () => {
      // Force a fetch even if the poller just claimed the in-flight slot,
      // otherwise the spinner would appear and vanish without reloading.
      state.loadingAccountId = null;
      withBusy(refreshBtn, "Refreshing", () => refreshPanel());
    });
    const fixAllBtn = document.getElementById("fix-all-btn");
    fixAllBtn.addEventListener("click", () => onFixAll(fixAllBtn));
    panelEl.querySelectorAll("[data-fix]").forEach((btn) => {
      btn.addEventListener("click", () => onFix(btn.dataset.fix, btn));
    });
    panelEl.querySelectorAll("[data-skip]").forEach((btn) => {
      btn.addEventListener("click", () => onSkip(btn.dataset.skip, btn));
    });
    panelEl.querySelectorAll("[data-unskip]").forEach((btn) => {
      btn.addEventListener("click", () => onUnskip(btn.dataset.unskip, btn));
    });
    panelEl.querySelectorAll("[data-ready]").forEach((btn) => {
      btn.addEventListener("click", () => onMarkReady(btn.dataset.ready, btn));
    });
    panelEl.querySelectorAll("[data-recheck]").forEach((btn) => {
      btn.addEventListener("click", () => onRecheck(btn.dataset.recheck, btn));
    });
  }

  function parseKey(raw) {
    const [repo, number, title, url] = raw.split("|");
    return { repo, number: Number(number), title, url };
  }

  async function onFix(raw, btn) {
    const { repo, number, title, url } = parseKey(raw);
    await withBusy(btn, "Queueing", async () => {
      try {
        await api(`/api/accounts/${state.activeAccountId}/issues/fix`, {
          method: "POST",
          body: JSON.stringify({ repo, number, title, url }),
        });
        await refreshPanel();
      } catch (err) {
        alert(err.message);
      }
    });
  }

  async function onSkip(raw, btn) {
    const { repo, number, title, url } = parseKey(raw);
    await withBusy(btn, "Skipping", async () => {
      try {
        await api(`/api/accounts/${state.activeAccountId}/issues/skip`, {
          method: "POST",
          body: JSON.stringify({ repo, number, title, url }),
        });
        await refreshPanel();
      } catch (err) {
        alert(err.message);
      }
    });
  }

  async function onUnskip(raw, btn) {
    const [repo, number] = raw.split("|");
    await withBusy(btn, "Restoring", async () => {
      try {
        await api(`/api/accounts/${state.activeAccountId}/issues/unskip`, {
          method: "POST",
          body: JSON.stringify({ repo, number: Number(number) }),
        });
        await refreshPanel();
      } catch (err) {
        alert(err.message);
      }
    });
  }

  async function onMarkReady(raw, btn) {
    const [repo, number] = raw.split("|");
    if (!confirm("Enable Ready for PR now? This changes the GitHub pull request from Draft to Ready for review without waiting for CI.")) return;
    await withBusy(btn, "Updating", async () => {
      try {
        await api(`/api/accounts/${state.activeAccountId}/issues/mark-ready`, {
          method: "POST",
          body: JSON.stringify({ repo, number: Number(number) }),
        });
        await refreshPanel();
      } catch (err) {
        alert(err.message);
      }
    });
  }

  async function onRecheck(raw, btn) {
    const [repo, number] = raw.split("|");
    if (!confirm("Recheck this pull request now? If its current CI is failing, the solver will start a fresh automatic repair.")) return;
    await withBusy(btn, "Queueing check", async () => {
      try {
        await api(`/api/accounts/${state.activeAccountId}/issues/recheck`, {
          method: "POST",
          body: JSON.stringify({ repo, number: Number(number) }),
        });
        delete state.cache[state.activeAccountId];
        await refreshPanel();
      } catch (err) {
        alert(err.message);
      }
    });
  }

  async function onFixAll(btn) {
    if (!confirm("Queue every currently listed issue for this account?")) return;
    await withBusy(btn, "Queueing", async () => {
      try {
        const result = await api(`/api/accounts/${state.activeAccountId}/issues/fix-all`, {
          method: "POST",
        });
        alert(`Found ${result.discovered} issue(s); queued ${result.queued} new job(s).`);
        await refreshPanel();
      } catch (err) {
        alert(err.message);
      }
    });
  }

  function openModal() {
    modalEl.classList.remove("hidden");
    addErrorEl.classList.add("hidden");
    tokenInput.value = "";
    tokenInput.focus();
  }

  function closeModal() {
    modalEl.classList.add("hidden");
  }

  function providerLabel(providerId) {
    const match = providerCatalog.find((item) => item.id === providerId);
    return match ? match.name : providerId;
  }

  function populateProviderOptions(selectedProvider) {
    aiProviderSelect.innerHTML = providerCatalog.map((provider) => `
      <option value="${esc(provider.id)}" ${provider.id === selectedProvider ? "selected" : ""}>
        ${esc(provider.name)}
      </option>
    `).join("");
  }

  function populateModelOptions(providerId, selectedModel) {
    const provider = providerCatalog.find((item) => item.id === providerId);
    const models = provider ? provider.models : [];
    const fallback = provider ? provider.default_model : selectedModel;
    const chosen = selectedModel || fallback;
    aiModelSelect.innerHTML = models.map((model) => `
      <option value="${esc(model)}" ${model === chosen ? "selected" : ""}>${esc(model)}</option>
    `).join("");
    if (!models.includes(chosen) && chosen) {
      aiModelSelect.innerHTML += `<option value="${esc(chosen)}" selected>${esc(chosen)}</option>`;
    }
  }

  function updateAIStatus(connected, provider) {
    const label = providerLabel(provider);
    aiStatusEl.textContent = connected
      ? `Connected to ${label} — your solver jobs use your own key.`
      : `Not connected — add a ${label} key before starting solver jobs.`;
    aiStatusEl.className = `connection-status ${connected ? "connected" : "missing"}`;
    aiRemoveBtn.classList.toggle("hidden", !connected);
  }

  async function openAISettings() {
    aiErrorEl.classList.add("hidden");
    aiKeyInput.value = "";
    aiSettingsModal.classList.remove("hidden");
    aiStatusEl.textContent = "Checking connection...";
    try {
      const [catalog, settings] = await Promise.all([
        api("/api/providers"),
        api("/api/settings/ai"),
      ]);
      providerCatalog = catalog.providers || [];
      populateProviderOptions(settings.provider || "deepseek");
      populateModelOptions(settings.provider || "deepseek", settings.model);
      updateAIStatus(settings.connected, settings.provider || "deepseek");
    } catch (err) {
      aiErrorEl.textContent = err.message;
      aiErrorEl.classList.remove("hidden");
    }
  }

  function closeAISettings() {
    aiSettingsModal.classList.add("hidden");
  }

  async function saveAISettings() {
    aiErrorEl.classList.add("hidden");
    try {
      const result = await api("/api/settings/ai", {
        method: "POST",
        body: JSON.stringify({
          provider: aiProviderSelect.value,
          api_key: aiKeyInput.value.trim() || null,
          model: aiModelSelect.value,
          clear: false,
        }),
      });
      populateModelOptions(result.provider, result.model);
      updateAIStatus(result.connected, result.provider);
      aiKeyInput.value = "";
      delete state.cache[state.activeAccountId];
      await refreshPanel();
    } catch (err) {
      aiErrorEl.textContent = err.message;
      aiErrorEl.classList.remove("hidden");
    }
  }

  async function testAIConnection() {
    aiErrorEl.classList.add("hidden");
    try {
      const result = await api("/api/settings/ai/test", {
        method: "POST",
        body: JSON.stringify({
          provider: aiProviderSelect.value,
          api_key: aiKeyInput.value.trim() || null,
          model: aiModelSelect.value,
        }),
      });
      aiStatusEl.textContent = `${providerLabel(result.provider)} connection test passed with ${result.model}.`;
      aiStatusEl.className = "connection-status connected";
    } catch (err) {
      aiErrorEl.textContent = err.message;
      aiErrorEl.classList.remove("hidden");
    }
  }

  async function removeAIKey() {
    const provider = aiProviderSelect.value;
    if (!confirm(`Remove your ${providerLabel(provider)} key? New solver work will stop until you add another key.`)) return;
    try {
      await api("/api/settings/ai", {
        method: "POST",
        body: JSON.stringify({
          provider,
          model: aiModelSelect.value,
          clear: true,
        }),
      });
      updateAIStatus(false, provider);
    } catch (err) {
      aiErrorEl.textContent = err.message;
      aiErrorEl.classList.remove("hidden");
    }
  }

  async function submitAddAccount() {
    const token = tokenInput.value.trim();
    if (!token) return;
    try {
      const account = await api("/api/accounts", {
        method: "POST",
        body: JSON.stringify({ token }),
      });
      closeModal();
      state.activeAccountId = account.id;
      await loadAccounts();
    } catch (err) {
      addErrorEl.textContent = err.message;
      addErrorEl.classList.remove("hidden");
    }
  }

  const addAccountSubmitBtn = document.getElementById("add-account-submit");
  const aiSaveBtn = document.getElementById("ai-save");
  const logoutBtn = document.getElementById("logout-btn");
  const exitViewAsBtn = document.getElementById("exit-view-as-btn");

  document.getElementById("add-account-btn").addEventListener("click", openModal);
  document.getElementById("add-account-cancel").addEventListener("click", closeModal);
  addAccountSubmitBtn.addEventListener("click", () =>
    withBusy(addAccountSubmitBtn, "Adding", submitAddAccount));
  tokenInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") withBusy(addAccountSubmitBtn, "Adding", submitAddAccount);
  });
  document.getElementById("ai-settings-btn").addEventListener("click", openAISettings);
  document.getElementById("ai-cancel").addEventListener("click", closeAISettings);
  aiProviderSelect.addEventListener("change", () => {
    populateModelOptions(aiProviderSelect.value, null);
  });
  aiSaveBtn.addEventListener("click", () =>
    withBusy(aiSaveBtn, "Saving", saveAISettings));
  aiTestBtn.addEventListener("click", () =>
    withBusy(aiTestBtn, "Testing", testAIConnection));
  aiRemoveBtn.addEventListener("click", () =>
    withBusy(aiRemoveBtn, "Removing", removeAIKey));

  logoutBtn.addEventListener("click", () =>
    withBusy(logoutBtn, "Signing out", async () => {
      await api("/api/logout", { method: "POST" }).catch(() => {});
      window.location.href = "/dashboard/login";
    }));

  exitViewAsBtn.addEventListener("click", () =>
    withBusy(exitViewAsBtn, "Exiting", async () => {
      try {
        await api("/api/admin/exit-view-as", { method: "POST" });
        window.location.reload();
      } catch (err) {
        alert(err.message);
      }
    }));

  const changePasswordModal = document.getElementById("change-password-modal");
  const currentPasswordInput = document.getElementById("current-password");
  const newPasswordValueInput = document.getElementById("new-password-value");
  const changePasswordError = document.getElementById("change-password-error");

  const changePasswordBtn = document.getElementById("change-password-submit");
  changePasswordBtn.addEventListener("click", () =>
    withBusy(changePasswordBtn, "Saving", async () => {
      changePasswordError.classList.add("hidden");
      try {
        await api("/api/change-password", {
          method: "POST",
          body: JSON.stringify({
            current_password: currentPasswordInput.value,
            new_password: newPasswordValueInput.value,
          }),
        });
        changePasswordModal.classList.add("hidden");
      } catch (err) {
        changePasswordError.textContent = err.message;
        changePasswordError.classList.remove("hidden");
      }
    }));

  async function initSession() {
    const me = await api("/api/me");
    if (me.is_admin) {
      document.getElementById("admin-link").classList.remove("hidden");
    }
    if (me.impersonating) {
      document.getElementById("impersonation-text").textContent =
        `Viewing @${me.impersonating.username}'s dashboard`;
      document.getElementById("impersonation-banner").classList.remove("hidden");
      document.getElementById("ai-settings-btn").classList.add("hidden");
    }
    if (me.must_change_password) {
      changePasswordModal.classList.remove("hidden");
    }
  }

  initSession()
    .then(loadAccounts)
    .catch((err) => {
      panelEl.innerHTML = `<p class="error">${esc(err.message)}</p>`;
    });

  setInterval(() => {
    if (state.activeAccountId && modalEl.classList.contains("hidden")) {
      refreshPanel();
    }
  }, 6000);
})();
