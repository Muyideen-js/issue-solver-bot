(function () {
  "use strict";

  const state = {
    accounts: [],
    activeAccountId: null,
    issues: [],
    jobs: [],
    loading: false,
  };

  const tabsEl = document.getElementById("tabs");
  const panelEl = document.getElementById("panel");
  const modalEl = document.getElementById("add-account-modal");
  const tokenInput = document.getElementById("add-account-token");
  const addErrorEl = document.getElementById("add-account-error");

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function safeHref(url) {
    return typeof url === "string" && url.startsWith("https://") ? url : "#";
  }

  async function api(path, options) {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
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
      NEEDS_REVIEW: ["badge-warn", "Needs review"],
      NEEDS_TESTS: ["badge-warn", "Needs CI"],
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
    tabsEl.innerHTML = state.accounts.map((account) => `
      <div class="tab ${account.id === state.activeAccountId ? "active" : ""}"
           data-id="${account.id}">
        @${esc(account.github_username)}
        <span class="tab-close" data-remove="${account.id}" title="Remove account"> &times;</span>
      </div>
    `).join("");

    tabsEl.querySelectorAll(".tab").forEach((el) => {
      el.addEventListener("click", (event) => {
        if (event.target.dataset.remove) return;
        state.activeAccountId = Number(el.dataset.id);
        renderTabs();
        refreshPanel();
      });
    });
    tabsEl.querySelectorAll("[data-remove]").forEach((el) => {
      el.addEventListener("click", async (event) => {
        event.stopPropagation();
        const id = Number(el.dataset.remove);
        if (!confirm("Remove this account? This does not close any GitHub issues.")) return;
        try {
          await api(`/api/accounts/${id}`, { method: "DELETE" });
          await loadAccounts();
        } catch (err) {
          alert(err.message);
        }
      });
    });
  }

  async function refreshPanel() {
    if (!state.activeAccountId) {
      panelEl.innerHTML = `<p class="empty">Add a GitHub account to see its assigned issues.</p>`;
      return;
    }
    if (state.loading) return;
    state.loading = true;
    try {
      const [issues, jobs] = await Promise.all([
        api(`/api/accounts/${state.activeAccountId}/issues`),
        api(`/api/accounts/${state.activeAccountId}/jobs`),
      ]);
      state.issues = issues;
      state.jobs = jobs;
      renderPanel();
    } catch (err) {
      panelEl.innerHTML = `<p class="error">${esc(err.message)}</p>`;
    } finally {
      state.loading = false;
    }
  }

  function renderPanel() {
    const issueRows = state.issues.map((issue) => {
      const labels = (issue.labels || [])
        .map((label) => `<span class="label-chip">${esc(label)}</span>`)
        .join("");
      const canFix = issue.status === "NOT_QUEUED" || issue.status === "FAILED";
      const canSkip = issue.status === "NOT_QUEUED";
      const canUnskip = issue.status === "SKIPPED_BY_USER";
      const prLink = issue.pr_url
        ? `<a href="${esc(safeHref(issue.pr_url))}" target="_blank" rel="noopener">PR</a>` : "";
      return `
        <tr>
          <td><a href="${esc(safeHref(issue.url))}" target="_blank" rel="noopener">${esc(issue.title)}</a></td>
          <td>${esc(issue.repo)}#${esc(issue.number)}</td>
          <td class="labels">${labels}</td>
          <td>${statusBadge(issue.status)} ${prLink}</td>
          <td class="row-actions">
            ${canFix ? `<button class="btn btn-primary" data-fix="${esc(issue.repo)}|${issue.number}|${esc(issue.title)}|${esc(issue.url)}">Fix</button>` : ""}
            ${canSkip ? `<button class="btn" data-skip="${esc(issue.repo)}|${issue.number}|${esc(issue.title)}|${esc(issue.url)}">Skip</button>` : ""}
            ${canUnskip ? `<button class="btn" data-unskip="${esc(issue.repo)}|${issue.number}">Unskip</button>` : ""}
          </td>
        </tr>`;
    }).join("");

    const jobRows = state.jobs.map((job) => `
      <tr>
        <td><a href="${esc(safeHref(job.issue_url))}" target="_blank" rel="noopener">${esc(job.title)}</a></td>
        <td>${esc(job.repo)}#${esc(job.number)}</td>
        <td>${statusBadge(job.status)}</td>
        <td>${job.pr_url ? `<a href="${esc(safeHref(job.pr_url))}" target="_blank" rel="noopener">PR</a>` : ""}</td>
        <td>${esc((job.last_error || "").slice(0, 140))}</td>
      </tr>`).join("");

    panelEl.innerHTML = `
      <div class="panel-header">
        <h2>Assigned issues (${state.issues.length})</h2>
        <div class="actions">
          <button id="refresh-btn" class="btn">Refresh</button>
          <button id="fix-all-btn" class="btn btn-primary">Fix all</button>
        </div>
      </div>
      <table>
        <thead><tr><th>Issue</th><th>Repo</th><th>Labels</th><th>Status</th><th></th></tr></thead>
        <tbody>${issueRows || `<tr><td colspan="5" class="empty">No open issues assigned to this account.</td></tr>`}</tbody>
      </table>
      <div class="section-title">Job history</div>
      <table>
        <thead><tr><th>Issue</th><th>Repo</th><th>Status</th><th>PR</th><th>Last note</th></tr></thead>
        <tbody>${jobRows || `<tr><td colspan="5" class="empty">No jobs yet.</td></tr>`}</tbody>
      </table>
    `;

    document.getElementById("refresh-btn").addEventListener("click", refreshPanel);
    document.getElementById("fix-all-btn").addEventListener("click", onFixAll);
    panelEl.querySelectorAll("[data-fix]").forEach((btn) => {
      btn.addEventListener("click", () => onFix(btn.dataset.fix, btn));
    });
    panelEl.querySelectorAll("[data-skip]").forEach((btn) => {
      btn.addEventListener("click", () => onSkip(btn.dataset.skip, btn));
    });
    panelEl.querySelectorAll("[data-unskip]").forEach((btn) => {
      btn.addEventListener("click", () => onUnskip(btn.dataset.unskip, btn));
    });
  }

  function parseKey(raw) {
    const [repo, number, title, url] = raw.split("|");
    return { repo, number: Number(number), title, url };
  }

  async function onFix(raw, btn) {
    const { repo, number, title, url } = parseKey(raw);
    btn.disabled = true;
    try {
      await api(`/api/accounts/${state.activeAccountId}/issues/fix`, {
        method: "POST",
        body: JSON.stringify({ repo, number, title, url }),
      });
      await refreshPanel();
    } catch (err) {
      alert(err.message);
      btn.disabled = false;
    }
  }

  async function onSkip(raw, btn) {
    const { repo, number, title, url } = parseKey(raw);
    btn.disabled = true;
    try {
      await api(`/api/accounts/${state.activeAccountId}/issues/skip`, {
        method: "POST",
        body: JSON.stringify({ repo, number, title, url }),
      });
      await refreshPanel();
    } catch (err) {
      alert(err.message);
      btn.disabled = false;
    }
  }

  async function onUnskip(raw, btn) {
    const [repo, number] = raw.split("|");
    btn.disabled = true;
    try {
      await api(`/api/accounts/${state.activeAccountId}/issues/unskip`, {
        method: "POST",
        body: JSON.stringify({ repo, number: Number(number) }),
      });
      await refreshPanel();
    } catch (err) {
      alert(err.message);
      btn.disabled = false;
    }
  }

  async function onFixAll() {
    if (!confirm("Queue every currently listed issue for this account?")) return;
    try {
      const result = await api(`/api/accounts/${state.activeAccountId}/issues/fix-all`, {
        method: "POST",
      });
      alert(`Found ${result.discovered} issue(s); queued ${result.queued} new job(s).`);
      await refreshPanel();
    } catch (err) {
      alert(err.message);
    }
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

  document.getElementById("add-account-btn").addEventListener("click", openModal);
  document.getElementById("add-account-cancel").addEventListener("click", closeModal);
  document.getElementById("add-account-submit").addEventListener("click", submitAddAccount);
  tokenInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") submitAddAccount();
  });

  loadAccounts().catch((err) => {
    panelEl.innerHTML = `<p class="error">${esc(err.message)}</p>`;
  });

  setInterval(() => {
    if (state.activeAccountId && modalEl.classList.contains("hidden")) {
      refreshPanel();
    }
  }, 6000);
})();
