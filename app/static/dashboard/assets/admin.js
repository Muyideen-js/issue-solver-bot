(function () {
  "use strict";

  const usersBody = document.getElementById("users-body");
  const addUserModal = document.getElementById("add-user-modal");
  const newUsernameInput = document.getElementById("new-username");
  const newPasswordInput = document.getElementById("new-password");
  const addUserError = document.getElementById("add-user-error");
  const resetModal = document.getElementById("reset-password-modal");
  const resetHint = document.getElementById("reset-password-hint");
  const resetValueInput = document.getElementById("reset-password-value");
  const resetError = document.getElementById("reset-password-error");

  let resetTargetId = null;

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
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

  async function loadUsers() {
    try {
      const users = await api("/api/admin/users");
      renderUsers(users);
    } catch (err) {
      usersBody.innerHTML = `<tr><td colspan="4" class="error">${esc(err.message)}</td></tr>`;
    }
  }

  function renderUsers(users) {
    if (!users.length) {
      usersBody.innerHTML = `<tr><td colspan="4" class="empty">No people yet.</td></tr>`;
      return;
    }
    usersBody.innerHTML = users.map((user) => `
      <tr>
        <td>${esc(user.username)}</td>
        <td>${user.must_change_password
          ? '<span class="badge badge-warn">Must set password</span>'
          : '<span class="badge badge-ok">Active</span>'}</td>
        <td>${esc((user.created_at || "").slice(0, 10))}</td>
        <td class="row-actions">
          <button class="btn" data-view-as="${user.id}">View as</button>
          <button class="btn" data-reset="${user.id}|${esc(user.username)}">Reset password</button>
          <button class="btn btn-danger" data-delete="${user.id}|${esc(user.username)}">Delete</button>
        </td>
      </tr>`).join("");

    usersBody.querySelectorAll("[data-view-as]").forEach((btn) => {
      btn.addEventListener("click", () => onViewAs(Number(btn.dataset.viewAs), btn));
    });
    usersBody.querySelectorAll("[data-reset]").forEach((btn) => {
      btn.addEventListener("click", () => openResetModal(btn.dataset.reset));
    });
    usersBody.querySelectorAll("[data-delete]").forEach((btn) => {
      btn.addEventListener("click", () => onDelete(btn.dataset.delete, btn));
    });
  }

  async function onViewAs(userId, btn) {
    btn.disabled = true;
    try {
      await api(`/api/admin/users/${userId}/view-as`, { method: "POST" });
      window.location.href = "/dashboard";
    } catch (err) {
      alert(err.message);
      btn.disabled = false;
    }
  }

  function openResetModal(raw) {
    const [id, username] = raw.split("|");
    resetTargetId = Number(id);
    resetHint.textContent = `Set a new temporary password for @${username}. They'll be asked to change it on next login.`;
    resetValueInput.value = "";
    resetError.classList.add("hidden");
    resetModal.classList.remove("hidden");
    resetValueInput.focus();
  }

  async function submitReset() {
    const password = resetValueInput.value;
    try {
      await api(`/api/admin/users/${resetTargetId}/reset-password`, {
        method: "POST",
        body: JSON.stringify({ temporary_password: password }),
      });
      resetModal.classList.add("hidden");
      await loadUsers();
    } catch (err) {
      resetError.textContent = err.message;
      resetError.classList.remove("hidden");
    }
  }

  async function onDelete(raw, btn) {
    const [id, username] = raw.split("|");
    if (!confirm(`Delete @${username}? This removes their login, GitHub accounts, and job history.`)) return;
    btn.disabled = true;
    try {
      await api(`/api/admin/users/${id}`, { method: "DELETE" });
      await loadUsers();
    } catch (err) {
      alert(err.message);
      btn.disabled = false;
    }
  }

  function openAddUserModal() {
    newUsernameInput.value = "";
    newPasswordInput.value = "";
    addUserError.classList.add("hidden");
    addUserModal.classList.remove("hidden");
    newUsernameInput.focus();
  }

  async function submitAddUser() {
    try {
      await api("/api/admin/users", {
        method: "POST",
        body: JSON.stringify({
          username: newUsernameInput.value.trim(),
          temporary_password: newPasswordInput.value,
        }),
      });
      addUserModal.classList.add("hidden");
      await loadUsers();
    } catch (err) {
      addUserError.textContent = err.message;
      addUserError.classList.remove("hidden");
    }
  }

  document.getElementById("add-user-btn").addEventListener("click", openAddUserModal);
  document.getElementById("add-user-cancel").addEventListener("click", () => {
    addUserModal.classList.add("hidden");
  });
  document.getElementById("add-user-submit").addEventListener("click", submitAddUser);
  document.getElementById("reset-password-cancel").addEventListener("click", () => {
    resetModal.classList.add("hidden");
  });
  document.getElementById("reset-password-submit").addEventListener("click", submitReset);
  document.getElementById("logout-btn").addEventListener("click", async () => {
    await api("/api/logout", { method: "POST" }).catch(() => {});
    window.location.href = "/dashboard/login";
  });

  loadUsers();
})();
