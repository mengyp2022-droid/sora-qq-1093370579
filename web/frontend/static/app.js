const API_BASE = "";
let token = localStorage.getItem("admin_token");
let currentPage = 1;
let accountsTotal = 0;

function api(url, options = {}) {
  const headers = { "Content-Type": "application/json", ...options.headers };
  if (token) headers["Authorization"] = "Bearer " + token;
  return fetch(API_BASE + url, { ...options, headers }).then(async (r) => {
    if (r.status === 401) {
      if (!url.includes("/auth/login")) {
        localStorage.removeItem("admin_token");
        window.location.reload();
      }
      const text = await r.text();
      let msg = "Unauthorized";
      try {
        const j = JSON.parse(text);
        if (j.detail) msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
      } catch (_) {}
      throw new Error(msg);
    }
    if (!r.ok) throw new Error(await r.text());
    const ct = r.headers.get("content-type");
    if (ct && ct.includes("application/json")) return r.json();
    return r.text();
  });
}

function showPage(name) {
  document.querySelectorAll(".panel").forEach((el) => el.classList.add("hidden"));
  document.querySelectorAll(".nav a").forEach((a) => a.classList.remove("active"));
  const panel = document.getElementById("panel-" + name);
  const link = document.querySelector('.nav a[data-tab="' + name + '"]');
  if (panel) panel.classList.remove("hidden");
  if (link) link.classList.add("active");
  if (name === "accounts") loadAccounts();
  if (name === "emails") {
    loadEmails();
    api("/api/settings").then((d) => {
      const sel = document.getElementById("email-api-mail-type");
      if (sel && d.email_api_default_type) {
        if ([].some.call(sel.options, (o) => o.value === d.email_api_default_type)) sel.value = d.email_api_default_type;
      }
    }).catch(() => {});
  }
  if (name === "bank-cards") loadBankCards();
  if (name === "phones") loadPhones();
  if (name === "video") loadSoraVideoWorkspace();
  if (name === "keys") loadSoraKeyManagement();
  if (name === "logs") {
    loadDashboard();
    loadLogs();
    updateRegisterStatusOnce();
    startRegisterStatusPoll();
  } else {
    stopRegisterStatusPoll();
  }
  if (name === "settings") loadSettings();
}

var registerStatusPollTimer = null;
var REGISTER_POLL_INTERVAL_MS = 1500;

/** 状态来源：GET /api/register/status 的 running 字段（后端 _registration_running，重启后必为 false） */
function updateRegisterButtonFromStatus(s) {
  var btnStart = document.getElementById("btn-start-register");
  var btnStop = document.getElementById("btn-stop-register");
  var heartbeatEl = document.getElementById("register-status-heartbeat");
  if (!btnStart) return;
  var running = !!(s && s.running === true);
  if (running) {
    btnStart.textContent = "正在注册";
    btnStart.disabled = true;
    btnStart.classList.add("btn-dash-disabled");
    if (btnStop) { btnStop.style.display = ""; }
    if (heartbeatEl) {
      heartbeatEl.style.display = "";
      heartbeatEl.textContent = s.last_heartbeat ? "最后心跳时间 " + (s.last_heartbeat.replace("T", " ").replace("Z", "").slice(0, 19)) : "";
    }
  } else {
    btnStart.textContent = "开启注册";
    btnStart.disabled = false;
    btnStart.classList.remove("btn-dash-disabled");
    if (btnStop) { btnStop.style.display = "none"; }
    if (heartbeatEl) {
      heartbeatEl.style.display = "none";
      heartbeatEl.textContent = "";
    }
  }
}

function updateRegisterStatusOnce() {
  api("/api/register/status").then(function(s) {
    updateRegisterButtonFromStatus(s);
  }).catch(function() {
    updateRegisterButtonFromStatus({ running: false });
  });
}

function startRegisterStatusPoll() {
  stopRegisterStatusPoll();
  registerStatusPollTimer = setInterval(function() {
    api("/api/register/status").then(function(s) {
      updateRegisterButtonFromStatus(s);
      loadDashboard();
      loadLogs();
    }).catch(function() {
      updateRegisterButtonFromStatus({ running: false });
    });
  }, REGISTER_POLL_INTERVAL_MS);
}

document.addEventListener("visibilitychange", function() {
  if (document.visibilityState === "visible") {
    var panelLogs = document.getElementById("panel-logs");
    if (panelLogs && !panelLogs.classList.contains("hidden")) {
      updateRegisterStatusOnce();
    }
  }
});

function stopRegisterStatusPoll() {
  if (registerStatusPollTimer) {
    clearInterval(registerStatusPollTimer);
    registerStatusPollTimer = null;
  }
}

function showModal(html) {
  document.getElementById("modal-body").innerHTML = html;
  document.getElementById("modal").classList.remove("hidden");
}
function hideModal() {
  document.getElementById("modal").classList.add("hidden");
  var mc = document.querySelector(".modal-content");
  if (mc) mc.classList.remove("modal-content-wide");
}
document.querySelector(".modal-close").addEventListener("click", hideModal);
document.getElementById("modal").addEventListener("click", (e) => {
  if (e.target.id === "modal") hideModal();
});

function toast(msg, type) {
  type = type || "success";
  var container = document.getElementById("toast-container");
  var el = document.createElement("div");
  el.className = "toast " + type;
  el.textContent = msg;
  container.appendChild(el);
  setTimeout(function() {
    el.style.opacity = "0";
    el.style.transform = "translateX(100%)";
    setTimeout(function() { el.remove(); }, 250);
  }, 2500);
}
function confirmBox(msg, onConfirm) {
  showModal(
    '<div class="confirm-dialog">' +
      '<p class="confirm-msg">' + escapeHtml(msg) + '</p>' +
      '<div class="confirm-btns">' +
        '<button type="button" class="btn-default btn-cancel">取消</button>' +
        '<button type="button" class="btn-primary btn-ok">确定</button>' +
      '</div>' +
    '</div>'
  );
  document.querySelector(".btn-cancel").addEventListener("click", function() { hideModal(); });
  document.querySelector(".btn-ok").addEventListener("click", function() {
    hideModal();
    if (onConfirm) onConfirm();
  });
}

// Login
if (!token) {
  document.getElementById("login-page").classList.remove("hidden");
  document.getElementById("admin-page").classList.add("hidden");
} else {
  document.getElementById("login-page").classList.add("hidden");
  document.getElementById("admin-page").classList.remove("hidden");
  api("/api/auth/me").then((d) => {
    var u = document.getElementById("current-user"); if (u) { var t = u.querySelector(".user-name-text"); if (t) t.textContent = d.username; else u.textContent = d.username; }
  }).catch(() => {
    localStorage.removeItem("admin_token");
    window.location.reload();
  });
}

document.getElementById("login-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const username = document.getElementById("login-username").value.trim();
  const password = document.getElementById("login-password").value;
  const errEl = document.getElementById("login-error");
  errEl.textContent = "";
  api("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  })
    .then((d) => {
      if (!d || !d.token) {
        errEl.textContent = "登录返回异常，请重试";
        return;
      }
      token = d.token;
      localStorage.setItem("admin_token", token);
      document.getElementById("login-page").classList.add("hidden");
      document.getElementById("admin-page").classList.remove("hidden");
      var cu = document.getElementById("current-user"); if (cu) { var ct = cu.querySelector(".user-name-text"); if (ct) ct.textContent = d.username || username; else cu.textContent = d.username || username; }
      errEl.textContent = "";
      showPage("accounts");
    })
    .catch((err) => {
      errEl.textContent = err.message || "登录失败";
    });
});

document.getElementById("btn-logout").addEventListener("click", () => {
  localStorage.removeItem("admin_token");
  window.location.reload();
});

// 侧栏默认收起，可展开；状态存 localStorage；收起时用底部按钮，展开时用头部按钮
(function() {
  var sidebar = document.getElementById("sidebar");
  var key = "sidebarCollapsed";
  function toggleSidebar() {
    sidebar.classList.toggle("collapsed");
    localStorage.setItem(key, sidebar.classList.contains("collapsed") ? "1" : "0");
  }
  if (sidebar) {
    var saved = localStorage.getItem(key);
    if (saved === "0" || saved === "false") sidebar.classList.remove("collapsed");
    else sidebar.classList.add("collapsed");
    var btnHeader = document.getElementById("sidebar-toggle");
    var btnFooter = document.getElementById("sidebar-toggle-footer");
    if (btnHeader) btnHeader.addEventListener("click", toggleSidebar);
    if (btnFooter) btnFooter.addEventListener("click", toggleSidebar);
  }
})();

// Nav tabs
document.querySelectorAll('.nav a[data-tab]').forEach((a) => {
  a.addEventListener("click", (e) => {
    e.preventDefault();
    showPage(a.getAttribute("data-tab"));
  });
});

// Accounts
function setCurrentSoraAccountId(accountId) {
  var id = parseInt(accountId, 10) || 0;
  if (!id) return;
  var idInput = document.getElementById("sora-api-account-id");
  if (idInput) idInput.value = String(id);
  localStorage.setItem("sora_api_last_account_id", String(id));
}

function isSoraAccountUsable(account) {
  return !!(account && account.has_sora && account.sora_enabled && !account.sora_quota_exhausted && account.has_token);
}

function formatAccountStatus(status, account) {
  var text = status || "";
  if (text === "Registered+Sora" && account && account.has_sora) return "Registered+Sora2";
  return text;
}

function getSoraAccountAvailabilityMessage(account) {
  if (!account) return "尚未选择生成账号";
  if (!account.has_sora) return "该账号尚未开通 Sora";
  if (!account.sora_enabled) return "该账号已停用";
  if (account.sora_quota_exhausted) return "该账号已标记额度不足";
  if (!account.has_token) return "该账号缺少 token";
  return "当前账号可用于视频生成";
}

function renderSoraVideoAccountSummary(account) {
  var box = document.getElementById("sora-video-account-summary");
  if (!box) return;
  if (!account) {
    box.innerHTML = '<div class="sora-account-summary-empty">进入页面后会自动选择一个当前可用的 Sora 账号。</div>';
    return;
  }
  var quotaText = getSoraQuotaText(account);
  var quotaClass = account.sora_quota_exhausted ? "is-bad" : "is-ok";
  var enableClass = account.sora_enabled ? "is-ok" : "is-bad";
  var soraClass = account.has_sora ? "is-ok" : "is-warn";
  var tokenClass = account.has_token ? "is-ok" : "is-bad";
  var statusText = formatAccountStatus(account.status, account) || "未设置";
  var registeredAt = account.registered_at || "未记录";
  var lastError = account.sora_last_error || "";
  box.innerHTML =
    '<div class="sora-account-summary-grid">' +
      '<div class="sora-account-summary-item"><span class="sora-account-summary-label">账号</span><span class="sora-account-summary-value">ID ' + escapeHtml(String(account.id || "")) + "</span></div>" +
      '<div class="sora-account-summary-item"><span class="sora-account-summary-label">邮箱</span><span class="sora-account-summary-value">' + escapeHtml(account.email || "") + "</span></div>" +
      '<div class="sora-account-summary-item"><span class="sora-account-summary-label">状态</span><span class="sora-account-summary-value">' + escapeHtml(statusText) + "</span></div>" +
      '<div class="sora-account-summary-item"><span class="sora-account-summary-label">注册时间</span><span class="sora-account-summary-value">' + escapeHtml(registeredAt) + "</span></div>" +
    "</div>" +
    '<div class="sora-account-flags">' +
      '<span class="sora-account-flag ' + soraClass + '">Sora ' + (account.has_sora ? "已开通" : "未开通") + "</span>" +
      '<span class="sora-account-flag ' + enableClass + '">账号' + (account.sora_enabled ? "可用" : "已停用") + "</span>" +
      '<span class="sora-account-flag ' + tokenClass + '">Token ' + (account.has_token ? "已就绪" : "缺失") + "</span>" +
      '<span class="sora-account-flag ' + quotaClass + '">额度 ' + escapeHtml(quotaText) + "</span>" +
    "</div>" +
    (lastError ? ('<div class="sora-account-summary-empty">最近错误：' + escapeHtml(lastError) + "</div>") : "");
}

function loadSoraAccountDetails(accountId, options) {
  var id = parseInt(accountId, 10) || 0;
  var msgEl = document.getElementById("sora-api-msg");
  if (!id) {
    renderSoraVideoAccountSummary(null);
    if (!(options && options.silent) && msgEl) msgEl.textContent = "请先输入有效账号 ID";
    return Promise.reject(new Error("请先输入有效账号 ID"));
  }
  if (!(options && options.silent) && msgEl) msgEl.textContent = "加载账号状态...";
  return api("/api/accounts/" + id).then(function(d) {
    setCurrentSoraAccountId(d.id);
    renderSoraVideoAccountSummary(d);
    if (!(options && options.skipKeyList)) loadSoraApiKeyList(d.id);
    if (!(options && options.silent) && msgEl) msgEl.textContent = getSoraAccountAvailabilityMessage(d);
    return d;
  }).catch(function(err) {
    renderSoraVideoAccountSummary(null);
    if (!(options && options.silent) && msgEl) msgEl.textContent = "加载失败：" + parseApiErrorMessage(err);
    throw err;
  });
}

function pickNextAvailableSoraAccount(options) {
  var msgEl = document.getElementById("sora-api-msg");
  if (!(options && options.silent) && msgEl) msgEl.textContent = "切换中...";
  return api("/api/accounts/next-sora-available").then(function(d) {
    return loadSoraAccountDetails(d.id, { silent: true }).then(function(account) {
      var text = ((options && options.messagePrefix) || "已切换到可用账号") + " ID " + d.id + "（" + (d.email || "") + "）";
      if (msgEl) msgEl.textContent = text;
      if (options && options.toast) toast(text, options.toastType || "success");
      return account;
    });
  }).catch(function(err) {
    var message = parseApiErrorMessage(err);
    if (msgEl) msgEl.textContent = "切换失败：" + message;
    throw err;
  });
}

function ensureSoraVideoAccountReady() {
  var currentId = getSoraAccountIdFromInput();
  if (!currentId) {
    return pickNextAvailableSoraAccount({ messagePrefix: "已自动选择可用账号" }).catch(function() {
      return null;
    });
  }
  return loadSoraAccountDetails(currentId, { silent: true }).then(function(account) {
    if (isSoraAccountUsable(account)) {
      var msgEl = document.getElementById("sora-api-msg");
      if (msgEl) msgEl.textContent = "当前账号可用于视频生成";
      return account;
    }
    return pickNextAvailableSoraAccount({ messagePrefix: "当前账号不可用，已自动切换到可用账号" });
  }).catch(function() {
    return pickNextAvailableSoraAccount({ messagePrefix: "当前账号不存在或不可用，已自动切换到可用账号" }).catch(function() {
      return null;
    });
  });
}

function loadSoraVideoWorkspace() {
  ensureSoraVideoAccountReady();
}

function getSoraQuotaText(r) {
  if (!r || !r.sora_enabled) return "已停用";
  if (r.sora_quota_exhausted) {
    var note = (r.sora_quota_note || "额度不足");
    return "额度不足(" + note + ")";
  }
  return "可用";
}

function updateSoraAccountState(accountId, payload, successMsg) {
  var id = parseInt(accountId, 10) || 0;
  if (!id) return;
  api("/api/accounts/" + id + "/sora-state", {
    method: "POST",
    body: JSON.stringify(payload || {}),
  }).then(function () {
    if (successMsg) toast(successMsg);
    loadAccounts();
    loadSoraApiKeyList(id);
  }).catch(function (err) {
    toast("操作失败: " + parseApiErrorMessage(err), "error");
  });
}

function getSoraQuotaRecheckLabel(result) {
  var labels = {
    recovered: "已恢复并回池",
    recovered_busy: "已恢复，当前繁忙",
    still_exhausted: "仍然额度不足",
    probe_failed: "复检失败",
    auth_failed: "鉴权失败",
    skipped_no_token: "跳过，无 token",
    skipped_disabled: "跳过，已停用",
    skipped_no_sora: "跳过，未开通 Sora",
    already_available: "本来就在池中",
  };
  return labels[result] || result || "未知";
}

function showSoraQuotaRecheckReport(report) {
  var items = Array.isArray(report && report.items) ? report.items : [];
  var rows = items.length
    ? items.map(function(item) {
        return (
          "<tr>" +
            "<td>" + escapeHtml(String(item.account_id || "")) + "</td>" +
            "<td>" + escapeHtml(item.email || "") + "</td>" +
            "<td>" + escapeHtml(getSoraQuotaRecheckLabel(item.result)) + "</td>" +
            "<td>" + escapeHtml(item.message || "") + "</td>" +
            "<td>" + escapeHtml(item.task_id || "-") + "</td>" +
          "</tr>"
        );
      }).join("")
    : '<tr><td colspan="5">没有可展示的复检结果</td></tr>';
  showModal(
    '<div class="quota-recheck-report">' +
      '<h3>额度复检结果</h3>' +
      '<p class="modal-tip">' + escapeHtml(report.message || "") + '</p>' +
      '<div class="table-wrap">' +
        '<table class="data-table">' +
          '<thead><tr><th>ID</th><th>邮箱</th><th>结果</th><th>说明</th><th>探针任务</th></tr></thead>' +
          '<tbody>' + rows + '</tbody>' +
        '</table>' +
      '</div>' +
    '</div>'
  );
}

function runSoraQuotaRecheck(options) {
  var opts = options || {};
  var payload = {
    limit: opts.accountId ? 1 : 10,
    auto_cancel: true,
  };
  if (opts.accountId) payload.account_id = parseInt(opts.accountId, 10) || 0;
  var button = opts.button || null;
  var originalText = button ? button.textContent : "";
  if (button) {
    button.disabled = true;
    button.textContent = "复检中...";
  }
  return api("/api/accounts/sora-quota/recheck", {
    method: "POST",
    body: JSON.stringify(payload),
  }).then(function(report) {
    showSoraQuotaRecheckReport(report);
    toast(report.message || "额度复检已完成");
    loadAccounts();
    var currentId = getSoraAccountIdFromInput();
    if (currentId) loadSoraAccountDetails(currentId, { silent: true }).catch(function() {});
    return report;
  }).catch(function(err) {
    toast("额度复检失败: " + parseApiErrorMessage(err), "error");
    throw err;
  }).finally(function() {
    if (button) {
      button.disabled = false;
      button.textContent = originalText;
    }
  });
}

function loadAccounts() {
  const status = document.getElementById("filter-status").value;
  const sora = document.getElementById("filter-sora").value;
  const plus = document.getElementById("filter-plus").value;
  const params = new URLSearchParams({ page: currentPage, page_size: 20 });
  if (status) params.set("status", status);
  if (sora) params.set("has_sora", sora);
  if (plus) params.set("has_plus", plus);
  api("/api/debug/db-info").then(function (info) {
    const el = document.getElementById("accounts-db-hint");
    if (el) el.textContent = "共 " + (info.accounts_count != null ? info.accounts_count : "?") + " 条。若用脚本注册，请用相同 DATA_DIR 启动本后端，否则新账号不会出现在本列表。";
  }).catch(function () {});
  api("/api/accounts?" + params).then((d) => {
    accountsTotal = d.total;
    const tbody = document.getElementById("accounts-tbody");
    tbody.innerHTML = d.items
      .map(
        (r) =>
          `<tr>
        <td>${r.id}</td>
        <td>${escapeHtml(r.email)}</td>
        <td>${escapeHtml(r.password || "")}</td>
        <td>${escapeHtml(formatAccountStatus(r.status, r) || "")}</td>
        <td>${r.has_sora ? "是" : "否"}</td>
        <td>${r.has_plus ? "是" : "否"}</td>
        <td>${r.phone_bound ? "是" : "否"}</td>
        <td title="${escapeHtml(r.refresh_token || "")}">${escapeHtml((r.refresh_token || "").slice(0, 24))}${(r.refresh_token || "").length > 24 ? "…" : ""}</td>
        <td>${escapeHtml(r.registered_at || r.created_at || "")}</td>
        <td title="${escapeHtml((r.sora_quota_note || '') + ((r.sora_quota_updated_at || '') ? (' @ ' + r.sora_quota_updated_at) : ''))}">${escapeHtml(getSoraQuotaText(r))}</td>
        <td>
          <button type="button" class="btn-op btn-use-sora-account" data-id="${r.id}">使用</button>
          <button type="button" class="btn-op ${r.sora_enabled ? "danger" : "btn-op-view"} btn-toggle-sora-account" data-id="${r.id}" data-enable="${r.sora_enabled ? "0" : "1"}">${r.sora_enabled ? "停用" : "启用"}</button>
          ${r.sora_quota_exhausted ? `<button type="button" class="btn-op btn-op-view btn-probe-sora-quota" data-id="${r.id}">复检额度</button><button type="button" class="btn-op btn-op-view btn-reset-sora-quota" data-id="${r.id}">重置额度</button>` : ""}
          <button type="button" class="btn-op btn-op-view btn-create-sora-key" data-id="${r.id}">生成 Key</button>
          <button type="button" class="btn-op btn-list-sora-key" data-id="${r.id}">查看 Key</button>
        </td>
      </tr>`
      )
      .join("");
    const pag = document.getElementById("accounts-pagination");
    const totalPages = Math.ceil(d.total / d.page_size) || 1;
    pag.innerHTML = `共 ${d.total} 条 ` + (totalPages > 1 ? `<button type="button" data-page="prev">上一页</button> <span>${currentPage}/${totalPages}</span> <button type="button" data-page="next">下一页</button>` : "");
    pag.querySelectorAll("button").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (btn.dataset.page === "prev" && currentPage > 1) currentPage--;
        if (btn.dataset.page === "next" && currentPage < totalPages) currentPage++;
        loadAccounts();
      });
    });
    tbody.querySelectorAll(".btn-create-sora-key").forEach((btn) => {
      btn.addEventListener("click", () => {
        var id = parseInt(btn.dataset.id, 10) || 0;
        if (!id) return;
        setCurrentSoraAccountId(id);
        createSoraApiKey(id);
      });
    });
    tbody.querySelectorAll(".btn-list-sora-key").forEach((btn) => {
      btn.addEventListener("click", () => {
        var id = parseInt(btn.dataset.id, 10) || 0;
        if (!id) return;
        setCurrentSoraAccountId(id);
        loadSoraApiKeyList(id);
      });
    });
    tbody.querySelectorAll(".btn-use-sora-account").forEach((btn) => {
      btn.addEventListener("click", () => {
        var id = parseInt(btn.dataset.id, 10) || 0;
        if (!id) return;
        setCurrentSoraAccountId(id);
        loadSoraAccountDetails(id, { silent: true }).catch(function() {});
        var msgEl = document.getElementById("sora-api-msg");
        if (msgEl) msgEl.textContent = "已切换到账号 ID " + id + "，可去左侧“视频生成”直接创建任务";
        toast("已切换到视频生成账号 ID " + id);
      });
    });
    tbody.querySelectorAll(".btn-reset-sora-quota").forEach((btn) => {
      btn.addEventListener("click", () => {
        var id = parseInt(btn.dataset.id, 10) || 0;
        if (!id) return;
        updateSoraAccountState(id, { reset_quota: true }, "已重置额度状态");
      });
    });
    tbody.querySelectorAll(".btn-probe-sora-quota").forEach((btn) => {
      btn.addEventListener("click", () => {
        var id = parseInt(btn.dataset.id, 10) || 0;
        if (!id) return;
        confirmBox(
          "这会对账号 ID " + id + " 发起一个最小视频探针，创建成功后会立即取消，并在额度恢复时自动回池。继续吗？",
          function() { runSoraQuotaRecheck({ accountId: id, button: btn }); }
        );
      });
    });
    tbody.querySelectorAll(".btn-toggle-sora-account").forEach((btn) => {
      btn.addEventListener("click", () => {
        var id = parseInt(btn.dataset.id, 10) || 0;
        var enable = String(btn.dataset.enable || "1") === "1";
        if (!id) return;
        updateSoraAccountState(id, { sora_enabled: enable }, enable ? "账号已启用" : "账号已停用");
      });
    });
  });
}
document.getElementById("filter-status").addEventListener("change", () => { currentPage = 1; loadAccounts(); });
document.getElementById("filter-sora").addEventListener("change", () => { currentPage = 1; loadAccounts(); });
document.getElementById("filter-plus").addEventListener("change", () => { currentPage = 1; loadAccounts(); });
document.getElementById("btn-recheck-sora-quota").addEventListener("click", function() {
  var btn = this;
  confirmBox(
    "这会对当前被标记“额度不足”的账号逐个发起最小视频探针，创建成功后立即取消，并自动让已恢复账号重新回池。继续吗？",
    function() { runSoraQuotaRecheck({ button: btn }); }
  );
});

document.getElementById("btn-export-accounts").addEventListener("click", () => {
  const status = document.getElementById("filter-status").value;
  const sora = document.getElementById("filter-sora").value;
  const plus = document.getElementById("filter-plus").value;
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  if (sora) params.set("has_sora", sora);
  if (plus) params.set("has_plus", plus);
  fetch(API_BASE + "/api/accounts/export?" + params, { headers: { Authorization: "Bearer " + token } })
    .then((r) => { if (!r.ok) throw new Error(r.statusText); return r.blob(); })
    .then((blob) => {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "accounts.csv";
      a.click();
      URL.revokeObjectURL(a.href);
    })
    .catch((err) => toast("导出失败: " + err.message, "error"));
});

function parseApiErrorMessage(err) {
  var msg = (err && err.message) ? err.message : "请求错误";
  try {
    var obj = JSON.parse(msg);
    if (obj && obj.detail) {
      return typeof obj.detail === "string" ? obj.detail : JSON.stringify(obj.detail);
    }
  } catch (_) {}
  return msg;
}

function copyTextToClipboard(text, successMsg) {
  var value = (text == null) ? "" : String(text);
  var done = function() { toast(successMsg || "已复制"); };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(value).then(done).catch(function() {});
    return;
  }
  var helper = document.createElement("textarea");
  helper.value = value;
  document.body.appendChild(helper);
  helper.select();
  try { document.execCommand("copy"); done(); } catch (_) {}
  helper.remove();
}

function getSoraAccountIdFromInput() {
  var raw = (document.getElementById("sora-api-account-id").value || "").trim();
  var id = parseInt(raw, 10);
  if (!id || id < 1) return null;
  localStorage.setItem("sora_api_last_account_id", String(id));
  return id;
}

var SORA_KEY_SCOPE_TEXT = "text_to_video";
var SORA_KEY_SCOPE_IMAGE = "image_to_video";
var SORA_KEY_SCOPE_ALL = "all_video";
var SORA_TASK_FAMILY_VIDEO_GEN = "video_gen";
var SORA_TASK_FAMILY_NF2 = "nf2";

function normalizeSoraKeyScope(scope) {
  var value = (scope || "").toString().trim().toLowerCase();
  var aliases = {
    text: SORA_KEY_SCOPE_TEXT,
    text_to_video: SORA_KEY_SCOPE_TEXT,
    "text-video": SORA_KEY_SCOPE_TEXT,
    text2video: SORA_KEY_SCOPE_TEXT,
    image: SORA_KEY_SCOPE_IMAGE,
    image_to_video: SORA_KEY_SCOPE_IMAGE,
    "image-video": SORA_KEY_SCOPE_IMAGE,
    image2video: SORA_KEY_SCOPE_IMAGE,
    all: SORA_KEY_SCOPE_ALL,
    all_video: SORA_KEY_SCOPE_ALL,
    both: SORA_KEY_SCOPE_ALL,
    combined: SORA_KEY_SCOPE_ALL,
    hybrid: SORA_KEY_SCOPE_ALL,
  };
  return aliases[value] || SORA_KEY_SCOPE_TEXT;
}

function getSoraKeyScopeLabel(scope) {
  var normalized = normalizeSoraKeyScope(scope);
  if (normalized === SORA_KEY_SCOPE_IMAGE) return "图生视频";
  if (normalized === SORA_KEY_SCOPE_ALL) return "文生+图生";
  return "文生视频";
}

function getSoraKeyScopeClass(scope) {
  var normalized = normalizeSoraKeyScope(scope);
  if (normalized === SORA_KEY_SCOPE_IMAGE) return "key-chip-scope-image";
  if (normalized === SORA_KEY_SCOPE_ALL) return "key-chip-scope-all";
  return "key-chip-scope-text";
}

function getSoraKeyModeLabel(mode, accountId) {
  var normalizedMode = (mode || "").toString().trim().toLowerCase();
  if (!normalizedMode) normalizedMode = (parseInt(accountId, 10) === 0 ? "pool" : "bound");
  return normalizedMode === "pool" ? "轮换池" : "账号绑定";
}

function syncSoraKeyScopeOptions() {
  document.querySelectorAll(".key-scope-option").forEach(function(option) {
    var input = option.querySelector('input[type="radio"]');
    option.classList.toggle("is-selected", !!(input && input.checked));
  });
}

function renderSoraApiKeyList(items) {
  var listEl = document.getElementById("sora-key-list");
  if (!listEl) return;
  var rows = items || [];
  if (!rows.length) {
    listEl.innerHTML = "该账号暂无可用 API Key";
    return;
  }
  listEl.innerHTML = rows
    .map(function (r) {
      var when = r.created_at ? ("创建于 " + escapeHtml(r.created_at)) : "";
      var used = r.last_used_at ? ("，最近调用 " + escapeHtml(r.last_used_at)) : "";
      var name = r.name ? ("<span class=\"key-tag\">" + escapeHtml(r.name) + "</span>") : "";
      var scope = '<span class="key-chip ' + getSoraKeyScopeClass(r.scope) + '">' + escapeHtml(r.scope_label || getSoraKeyScopeLabel(r.scope)) + "</span>";
      return "<div class=\"key-item\">" + name + scope + "<span>" + escapeHtml(r.key_mask || "") + "</span><span>" + when + used + "</span></div>";
    })
    .join("");
}

function loadSoraApiKeyList(accountId) {
  var id = parseInt(accountId, 10) || 0;
  var listEl = document.getElementById("sora-key-list");
  if (!id) {
    if (listEl) listEl.innerHTML = "";
    return;
  }
  if (listEl) listEl.innerHTML = "加载中...";
  api("/api/sora-keys?account_id=" + id + "&active_only=true")
    .then(function (d) {
      renderSoraApiKeyList((d && d.items) || []);
    })
    .catch(function (err) {
      if (listEl) listEl.textContent = "查询失败：" + parseApiErrorMessage(err);
    });
}

function showCreatedSoraApiKey(result) {
  var raw = (result && result.api_key) ? result.api_key : "";
  var email = (result && result.email) ? result.email : "";
  var accountId = parseInt((result && result.account_id) || 0, 10) || 0;
  var scopeLabel = (result && result.scope_label) ? result.scope_label : getSoraKeyScopeLabel(result && result.scope);
  var modeLabel = getSoraKeyModeLabel(result && result.key_mode, accountId);
  var accountText = accountId === 0 ? "[自动轮换池]" : (email + " (ID " + String(accountId || "") + ")");
  showModal(
    '<div class="email-view-card">' +
      '<h3 style="margin-top:0;">API Key 生成成功</h3>' +
      '<p>模式：' + escapeHtml(modeLabel) + "｜类型：" + escapeHtml(scopeLabel) + "</p>" +
      '<p>目标：' + escapeHtml(accountText) + "</p>" +
      '<p style="margin:0.35rem 0;">' +
        '<code id="sora-api-key-raw" style="display:inline-block;word-break:break-all;">' + escapeHtml(raw) + "</code>" +
      "</p>" +
      '<div style="display:flex;gap:0.5rem;flex-wrap:wrap;margin-top:0.5rem;">' +
        '<button type="button" id="btn-copy-sora-api-key">复制 Key</button>' +
      "</div>" +
      '<p class="email-api-msg" style="margin-top:0.65rem;">调用时可用 Header：Authorization: Bearer ' + escapeHtml(raw) + "</p>" +
    "</div>"
  );
  var btnCopy = document.getElementById("btn-copy-sora-api-key");
  if (!btnCopy) return;
  btnCopy.addEventListener("click", function () {
    copyTextToClipboard(raw, "API Key 已复制");
  });
}

function createSoraApiKey(accountId, options) {
  var opts = options || {};
  var id = parseInt(accountId, 10);
  var allowPool = !!opts.allowPool;
  var msgEl = opts.msgEl || document.getElementById(opts.messageElementId || "sora-api-msg");
  if ((isNaN(id) || id < 1) && !allowPool) {
    if (msgEl) msgEl.textContent = "请先输入有效账号 ID";
    toast("请先输入有效账号 ID", "info");
    return Promise.resolve(null);
  }
  if (allowPool && (isNaN(id) || id < 0)) id = 0;
  var scope = normalizeSoraKeyScope(opts.scope || SORA_KEY_SCOPE_TEXT);
  var name = (opts.name || "").trim();
  if (msgEl) msgEl.textContent = id === 0 ? "正在生成轮换池 API Key..." : "正在生成 API Key...";
  if (id > 0) setCurrentSoraAccountId(id);
  return api("/api/sora-keys", {
    method: "POST",
    body: JSON.stringify({ account_id: id, name: name, scope: scope }),
  }).then(function(d) {
    if (msgEl) msgEl.textContent = "API Key 已生成（可在弹窗中复制）";
    showCreatedSoraApiKey(d || {});
    if (id > 0) loadSoraApiKeyList(id);
    if (typeof opts.onSuccess === "function") opts.onSuccess(d || {});
    return d || {};
  }).catch(function(err) {
    if (msgEl) msgEl.textContent = "生成失败：" + parseApiErrorMessage(err);
    throw err;
  });
}

function buildSoraKeyStats(items) {
  var rows = items || [];
  var stats = {
    total: rows.length,
    active: 0,
    pool: 0,
    text: 0,
    image: 0,
    all: 0,
  };
  rows.forEach(function(item) {
    var scope = normalizeSoraKeyScope(item.scope);
    if (item.is_active) stats.active += 1;
    if ((item.key_mode || "") === "pool" || parseInt(item.account_id || 0, 10) === 0) stats.pool += 1;
    if (scope === SORA_KEY_SCOPE_IMAGE) stats.image += 1;
    else if (scope === SORA_KEY_SCOPE_ALL) stats.all += 1;
    else stats.text += 1;
  });
  return stats;
}

function renderSoraKeyStats(items) {
  var wrap = document.getElementById("sora-key-stats");
  if (!wrap) return;
  var stats = buildSoraKeyStats(items);
  wrap.innerHTML = [
    ['全部 Key', stats.total],
    ['启用中', stats.active],
    ['轮换池 Key', stats.pool],
    ['文生视频', stats.text],
    ['图生视频', stats.image],
    ['文生+图生', stats.all],
  ].map(function(entry) {
    return '<div class="key-stat-card"><span>' + escapeHtml(entry[0]) + '</span><strong>' + escapeHtml(String(entry[1])) + '</strong></div>';
  }).join("");
}

function renderSoraKeyManagementTable(items) {
  var tbody = document.getElementById("sora-key-table-body");
  if (!tbody) return;
  var rows = items || [];
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="10" class="key-empty">当前条件下没有找到 Key</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(function(item) {
    var accountId = parseInt(item.account_id || 0, 10) || 0;
    var scopeLabel = item.scope_label || getSoraKeyScopeLabel(item.scope);
    var modeLabel = getSoraKeyModeLabel(item.key_mode, accountId);
    var accountText = accountId === 0
      ? '<span class="key-chip key-chip-pool">自动轮换池</span>'
      : ('<div class="key-cell-stack"><strong>ID ' + String(accountId) + '</strong><span>' + escapeHtml(item.email || "") + '</span></div>');
    var statusBadge = item.is_active
      ? '<span class="key-chip key-chip-rotate">启用中</span>'
      : '<span class="key-chip key-chip-inactive">已停用</span>';
    var actions = item.is_active
      ? '<button type="button" class="key-btn-danger" data-action="disable-key" data-key-id="' + String(item.id) + '">停用</button>'
      : '<span class="key-chip key-chip-inactive">已停用</span>';
    return '' +
      '<tr>' +
        '<td>' + String(item.id) + '</td>' +
        '<td>' +
          '<div class="key-cell-stack">' +
            '<strong>' + escapeHtml(item.name || "未命名 Key") + '</strong>' +
            '<span>创建人 ' + escapeHtml(item.created_by || "-") + '</span>' +
          '</div>' +
        '</td>' +
        '<td><span class="key-chip ' + getSoraKeyScopeClass(item.scope) + '">' + escapeHtml(scopeLabel) + '</span></td>' +
        '<td><span class="key-chip ' + (accountId === 0 ? 'key-chip-pool' : 'key-chip-local') + '">' + escapeHtml(modeLabel) + '</span></td>' +
        '<td>' + accountText + '</td>' +
        '<td><code>' + escapeHtml(item.key_mask || "") + '</code></td>' +
        '<td>' + statusBadge + '</td>' +
        '<td>' + escapeHtml(item.last_used_at || "未调用") + '</td>' +
        '<td>' + escapeHtml(item.created_at || "") + '</td>' +
        '<td><div class="key-row-actions">' + actions + '</div></td>' +
      '</tr>';
  }).join("");
}

function loadSoraKeyManagement() {
  var msgEl = document.getElementById("sora-key-manager-msg");
  var mode = (document.getElementById("sora-key-filter-mode").value || "").trim();
  var scopeInput = (document.getElementById("sora-key-filter-scope").value || "").trim();
  var status = (document.getElementById("sora-key-filter-status").value || "all").trim();
  var qs = ["active_only=false"];
  if (mode) qs.push("key_mode=" + encodeURIComponent(mode));
  if (scopeInput) qs.push("scope=" + encodeURIComponent(normalizeSoraKeyScope(scopeInput)));
  if (msgEl) msgEl.textContent = "正在加载 Key 列表...";
  api("/api/sora-keys?" + qs.join("&"))
    .then(function(d) {
      var items = (d && d.items) || [];
      if (status === "active") items = items.filter(function(item) { return !!item.is_active; });
      if (status === "inactive") items = items.filter(function(item) { return !item.is_active; });
      renderSoraKeyStats(items);
      renderSoraKeyManagementTable(items);
      if (msgEl) msgEl.textContent = "已加载 " + String(items.length) + " 条 Key";
    })
    .catch(function(err) {
      renderSoraKeyStats([]);
      renderSoraKeyManagementTable([]);
      if (msgEl) msgEl.textContent = "加载失败：" + parseApiErrorMessage(err);
    });
}

function createSoraPoolApiKey() {
  var name = (document.getElementById("sora-key-create-name").value || "").trim();
  var selected = document.querySelector('input[name="sora-key-scope"]:checked');
  var scope = selected ? selected.value : SORA_KEY_SCOPE_TEXT;
  createSoraApiKey(0, {
    allowPool: true,
    name: name,
    scope: scope,
    messageElementId: "sora-key-manager-msg",
    onSuccess: function() {
      loadSoraKeyManagement();
    },
  }).catch(function() {});
}

function disableSoraApiKey(keyId) {
  var numericId = parseInt(keyId, 10);
  if (!numericId || numericId < 1) return;
  confirmBox("确定停用这把 API Key？停用后会立刻失效。", function() {
    api("/api/sora-keys/" + numericId, { method: "DELETE" })
      .then(function() {
        toast("API Key 已停用");
        loadSoraKeyManagement();
      })
      .catch(function(err) {
        toast("停用失败：" + parseApiErrorMessage(err), "error");
      });
  });
}

(function initSoraAccountInput() {
  var saved = localStorage.getItem("sora_api_last_account_id");
  var id = parseInt(saved || "", 10);
  if (!id || id < 1) return;
  var input = document.getElementById("sora-api-account-id");
  if (input && !input.value) input.value = String(id);
})();

document.getElementById("sora-api-account-id").addEventListener("change", function() {
  var id = parseInt(this.value || "", 10);
  if (!id || id < 1) return;
  setCurrentSoraAccountId(id);
  loadSoraAccountDetails(id, { silent: true }).catch(function() {});
});

document.getElementById("btn-sora-me").addEventListener("click", function() {
  var id = getSoraAccountIdFromInput();
  var msgEl = document.getElementById("sora-api-msg");
  if (!id) {
    msgEl.textContent = "请先输入有效账号 ID";
    toast("请先输入有效账号 ID", "info");
    return;
  }
  msgEl.textContent = "请求中...";
  api("/api/sora-api/me", {
    method: "POST",
    body: JSON.stringify({ account_id: id }),
  }).then(function(d) {
    var me = d.me || {};
    var uname = me.username ? ("username=" + me.username) : "未设置 username";
    msgEl.textContent = "调用成功，" + uname;
    toast("Sora API 调用成功");
  }).catch(function(err) {
    msgEl.textContent = "失败：" + parseApiErrorMessage(err);
  });
});

document.getElementById("btn-sora-pick-next").addEventListener("click", function() {
  pickNextAvailableSoraAccount({ toast: true }).catch(function() {});
});

document.getElementById("btn-sora-key-create").addEventListener("click", function() {
  var id = getSoraAccountIdFromInput();
  createSoraApiKey(id, { scope: SORA_KEY_SCOPE_TEXT }).catch(function() {});
});

document.getElementById("btn-sora-key-list").addEventListener("click", function() {
  var id = getSoraAccountIdFromInput();
  if (!id) {
    document.getElementById("sora-api-msg").textContent = "请先输入有效账号 ID";
    return;
  }
  loadSoraApiKeyList(id);
});

document.getElementById("btn-sora-activate").addEventListener("click", function() {
  var id = getSoraAccountIdFromInput();
  var msgEl = document.getElementById("sora-api-msg");
  if (!id) {
    msgEl.textContent = "请先输入有效账号 ID";
    toast("请先输入有效账号 ID", "info");
    return;
  }
  msgEl.textContent = "激活中...";
  api("/api/sora-api/activate", {
    method: "POST",
    body: JSON.stringify({ account_id: id }),
  }).then(function(d) {
    var uname = d.username ? ("username=" + d.username) : "未获取到 username";
    msgEl.textContent = "激活成功，" + uname;
    toast("Sora 激活成功");
    loadAccounts();
  }).catch(function(err) {
    msgEl.textContent = "失败：" + parseApiErrorMessage(err);
  });
});

document.getElementById("btn-key-manager-create").addEventListener("click", createSoraPoolApiKey);
document.getElementById("btn-key-manager-refresh").addEventListener("click", loadSoraKeyManagement);
document.getElementById("btn-sora-key-filter-refresh").addEventListener("click", loadSoraKeyManagement);
document.getElementById("sora-key-filter-mode").addEventListener("change", loadSoraKeyManagement);
document.getElementById("sora-key-filter-scope").addEventListener("change", loadSoraKeyManagement);
document.getElementById("sora-key-filter-status").addEventListener("change", loadSoraKeyManagement);
document.querySelectorAll('input[name="sora-key-scope"]').forEach(function(input) {
  input.addEventListener("change", syncSoraKeyScopeOptions);
});
document.getElementById("sora-key-table-body").addEventListener("click", function(e) {
  var btn = e.target.closest('button[data-action="disable-key"]');
  if (!btn) return;
  disableSoraApiKey(btn.getAttribute("data-key-id"));
});
syncSoraKeyScopeOptions();

var soraVideoTasks = [];
var soraVideoSelectedTaskId = localStorage.getItem("sora_video_selected_task_id") || "";
var soraVideoPollers = {};
var soraVideoUiClock = null;
var soraVideoCreateInFlight = false;
var soraVideoSnapshotRefreshInFlight = {};
var soraVideoMediaReloadAttempts = {};
var SORA_VIDEO_TASKS_STORAGE_KEY = "sora_video_workspace_tasks_v2";
var SORA_VIDEO_AUTO_ROTATE_STORAGE_KEY = "sora_video_auto_rotate";

function normalizeSoraVideoStatus(status) {
  var value = (status || "").toString().trim().toLowerCase();
  if (!value) return "";
  var aliases = {
    complete: "succeeded",
    completed: "succeeded",
    done: "succeeded",
    success: "succeeded",
    succeed: "succeeded",
    succeeded: "succeeded",
    canceled: "cancelled",
    cancelled: "cancelled",
    in_progress: "running",
    inprogress: "running",
    processing: "running"
  };
  return aliases[value] || value;
}

function isSoraVideoSuccessStatus(status) {
  return normalizeSoraVideoStatus(status) === "succeeded";
}

function isSoraVideoTerminalStatus(status) {
  var value = normalizeSoraVideoStatus(status);
  return ["succeeded", "failed", "cancelled", "rejected", "expired", "error"].indexOf(value) >= 0;
}

function parseNumeric(value) {
  var num = Number(value);
  return Number.isFinite(num) ? num : null;
}

function parseSoraDateMs(value) {
  var raw = (value || "").toString().trim();
  if (!raw) return 0;
  var normalized = raw.indexOf("T") >= 0 ? raw : raw.replace(" ", "T");
  var ms = Date.parse(normalized);
  return Number.isFinite(ms) ? ms : 0;
}

function formatSoraDateTime(value) {
  var ms = parseSoraDateMs(value);
  if (!ms) return value || "--";
  return new Date(ms).toLocaleString("zh-CN", { hour12: false });
}

function formatDuration(totalSeconds) {
  var seconds = Math.max(0, Math.round(Number(totalSeconds) || 0));
  var hours = Math.floor(seconds / 3600);
  var minutes = Math.floor((seconds % 3600) / 60);
  var remain = seconds % 60;
  if (hours > 0) return String(hours) + ":" + String(minutes).padStart(2, "0") + ":" + String(remain).padStart(2, "0");
  return String(minutes).padStart(2, "0") + ":" + String(remain).padStart(2, "0");
}

function trimVideoPrompt(value, maxLength) {
  var text = (value || "").toString().trim();
  if (!text) return "";
  var max = Math.max(8, parseInt(maxLength || "48", 10) || 48);
  return text.length > max ? text.slice(0, max - 1) + "…" : text;
}

function pickSoraPreviewUrl(videoUrls) {
  var urls = Array.isArray(videoUrls) ? videoUrls.slice() : [];
  if (!urls.length) return "";
  function getMatchText(url) {
    var text = (url || "").toLowerCase();
    try {
      return decodeURIComponent(text);
    } catch (_) {
      return text;
    }
  }
  function findByKeyword(keyword) {
    for (var i = 0; i < urls.length; i += 1) {
      if (getMatchText(urls[i]).indexOf(keyword) >= 0) return urls[i];
    }
    return "";
  }
  return findByKeyword("no_watermark") ||
    findByKeyword("downloadable") ||
    findByKeyword("/src.mp4") ||
    findByKeyword("/source.mp4") ||
    findByKeyword("/source_wm.mp4") ||
    findByKeyword("original") ||
    findByKeyword("/hd.mp4") ||
    findByKeyword("/md.mp4") ||
    findByKeyword("/ld.mp4") ||
    findByKeyword("watermarked.mp4") ||
    urls[0] ||
    "";
}

function getSoraVideoView(result) {
  if (!result || typeof result !== "object") return {};
  return result.final_result && typeof result.final_result === "object" ? result.final_result : result;
}

function setSoraVideoTaskId(taskId) {
  var value = (taskId || "").toString().trim();
  var input = document.getElementById("sora-video-task-id");
  if (input) input.value = value;
  if (value) localStorage.setItem("sora_video_last_task_id", value);
  else localStorage.removeItem("sora_video_last_task_id");
}

function getSoraVideoTaskId() {
  return (document.getElementById("sora-video-task-id").value || "").trim();
}

function isSoraVideoAutoRotateEnabled() {
  var el = document.getElementById("sora-video-auto-rotate");
  return !!(el && el.checked);
}

function setSoraVideoAutoRotateEnabled(enabled) {
  var checked = !!enabled;
  var el = document.getElementById("sora-video-auto-rotate");
  if (el) el.checked = checked;
  localStorage.setItem(SORA_VIDEO_AUTO_ROTATE_STORAGE_KEY, checked ? "1" : "0");
}

function getSoraVideoTaskMode() {
  return normalizeSoraKeyScope((document.getElementById("sora-video-task-mode").value || "text_to_video").trim());
}

function getSoraVideoTaskFamily() {
  var el = document.getElementById("sora-video-task-family");
  var value = ((el && el.value) || SORA_TASK_FAMILY_VIDEO_GEN).trim().toLowerCase();
  return value === SORA_TASK_FAMILY_NF2 ? SORA_TASK_FAMILY_NF2 : SORA_TASK_FAMILY_VIDEO_GEN;
}

function getSoraVideoSelectedImageFile() {
  var input = document.getElementById("sora-video-image-file");
  return input && input.files && input.files[0] ? input.files[0] : null;
}

function updateSoraVideoImageMeta() {
  var metaEl = document.getElementById("sora-video-image-meta");
  var file = getSoraVideoSelectedImageFile();
  if (!metaEl) return;
  if (!file) {
    metaEl.textContent = "上传一张图片作为视频的起始画面。";
    return;
  }
  var sizeKb = Math.max(1, Math.round((Number(file.size || 0) / 1024)));
  metaEl.textContent = file.name + " · " + sizeKb + " KB";
}

function updateSoraVideoComposerMode() {
  var mode = getSoraVideoTaskMode();
  var imageField = document.getElementById("sora-video-image-field");
  var audioFields = document.getElementById("sora-video-audio-fields");
  var familyField = document.getElementById("sora-video-task-family-field");
  var promptEl = document.getElementById("sora-video-prompt");
  if (imageField) imageField.classList.toggle("hidden", mode !== SORA_KEY_SCOPE_IMAGE);
  if (audioFields) audioFields.classList.toggle("hidden", mode !== SORA_KEY_SCOPE_TEXT);
  if (familyField) familyField.classList.toggle("hidden", mode !== SORA_KEY_SCOPE_TEXT);
  if (promptEl && !promptEl.value.trim()) {
    promptEl.placeholder = mode === SORA_KEY_SCOPE_IMAGE
      ? "例如：让画面中的人物轻轻转头，头发和光线自然摆动。"
      : "例如：A cinematic shot of ocean waves at sunrise.";
  }
  updateSoraVideoImageMeta();
}

function apiForm(url, formData, extraOptions) {
  var options = extraOptions || {};
  var headers = Object.assign({}, options.headers || {});
  if (token) headers.Authorization = "Bearer " + token;
  return fetch(API_BASE + url, {
    method: options.method || "POST",
    body: formData,
    headers: headers
  }).then(async function(r) {
    if (r.status === 401) {
      if (!url.includes("/auth/login")) {
        localStorage.removeItem("admin_token");
        window.location.reload();
      }
      throw new Error(await r.text());
    }
    if (!r.ok) throw new Error(await r.text());
    var ct = r.headers.get("content-type");
    if (ct && ct.includes("application/json")) return r.json();
    return r.text();
  });
}

function getSoraVideoPollOptions() {
  return {
    pollIntervalSeconds: Math.max(1, parseInt(document.getElementById("sora-video-poll-interval").value || "5", 10) || 5),
    timeoutSeconds: Math.max(30, parseInt(document.getElementById("sora-video-timeout").value || "900", 10) || 900)
  };
}

function getSoraVideoComposerPayload() {
  var prompt = (document.getElementById("sora-video-prompt").value || "").trim();
  if (!prompt) throw new Error("请输入视频 prompt");
  var taskMode = getSoraVideoTaskMode();
  var autoRotate = isSoraVideoAutoRotateEnabled();
  var accountId = getSoraAccountIdFromInput();
  if (!autoRotate && !accountId) {
    throw new Error("关闭自动轮换时，请先选择一个有效账号");
  }
  var imageFile = getSoraVideoSelectedImageFile();
  if (taskMode === SORA_KEY_SCOPE_IMAGE && !imageFile) {
    throw new Error("图生视频模式下请先上传参考图");
  }
  return {
    prompt: prompt,
    taskMode: taskMode,
    taskFamily: taskMode === SORA_KEY_SCOPE_IMAGE ? SORA_TASK_FAMILY_VIDEO_GEN : getSoraVideoTaskFamily(),
    imageFile: imageFile,
    autoRotate: autoRotate,
    account_id: accountId,
    audio_caption: taskMode === SORA_KEY_SCOPE_TEXT ? (document.getElementById("sora-video-audio-caption").value || "").trim() : "",
    audio_transcript: taskMode === SORA_KEY_SCOPE_TEXT ? (document.getElementById("sora-video-audio-transcript").value || "").trim() : "",
    batchCount: Math.max(1, Math.min(parseInt(document.getElementById("sora-video-batch-count").value || "1", 10) || 1, 6)),
    n_variants: Math.max(1, Math.min(parseInt(document.getElementById("sora-video-variants").value || "1", 10) || 1, 4)),
    n_frames: Math.max(60, parseInt(document.getElementById("sora-video-frames").value || "300", 10) || 300),
    resolution: Math.max(360, parseInt(document.getElementById("sora-video-resolution").value || "360", 10) || 360),
    orientation: (document.getElementById("sora-video-orientation").value || "wide").trim(),
    pollIntervalSeconds: getSoraVideoPollOptions().pollIntervalSeconds,
    timeoutSeconds: getSoraVideoPollOptions().timeoutSeconds
  };
}

function serializeSoraVideoTask(task) {
  return {
    task_id: task.task_id,
    prompt: task.prompt || "",
    task_mode: task.task_mode || SORA_KEY_SCOPE_TEXT,
    task_family: task.task_family || "",
    status: task.status || "",
    normalized_status: task.normalized_status || "",
    is_terminal: !!task.is_terminal,
    is_success: !!task.is_success,
    used_account_id: task.used_account_id || null,
    used_email: task.used_email || "",
    created_local_at: task.created_local_at || "",
    remote_created_at: task.remote_created_at || "",
    last_update_at: task.last_update_at || "",
    progress_pct: task.progress_pct,
    progress_pos_in_queue: task.progress_pos_in_queue,
    estimated_queue_wait_time: task.estimated_queue_wait_time,
    video_urls: Array.isArray(task.video_urls) ? task.video_urls.slice(0, 4) : [],
    poll_interval_seconds: task.poll_interval_seconds || 5,
    timeout_seconds: task.timeout_seconds || 900,
    error_message: task.error_message || "",
    auto_rotate: !!task.auto_rotate,
    source_image_media_id: task.source_image_media_id || "",
    source_image_name: task.source_image_name || ""
  };
}

function persistSoraVideoWorkspace() {
  localStorage.setItem(SORA_VIDEO_TASKS_STORAGE_KEY, JSON.stringify(soraVideoTasks.slice(0, 18).map(serializeSoraVideoTask)));
  if (soraVideoSelectedTaskId) localStorage.setItem("sora_video_selected_task_id", soraVideoSelectedTaskId);
  else localStorage.removeItem("sora_video_selected_task_id");
}

function loadPersistedSoraVideoTasks() {
  var raw = localStorage.getItem(SORA_VIDEO_TASKS_STORAGE_KEY);
  if (!raw) return [];
  try {
    var parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(function(task) {
      return task && typeof task.task_id === "string" && task.task_id.trim();
    }).map(function(task) {
      return {
        task_id: task.task_id.trim(),
        prompt: task.prompt || "",
        task_mode: normalizeSoraKeyScope(task.task_mode || SORA_KEY_SCOPE_TEXT),
        task_family: (task.task_family || "").trim(),
        status: task.status || "",
        normalized_status: task.normalized_status || "",
        is_terminal: !!task.is_terminal,
        is_success: !!task.is_success,
        used_account_id: task.used_account_id || null,
        used_email: task.used_email || "",
        created_local_at: task.created_local_at || "",
        remote_created_at: task.remote_created_at || "",
        last_update_at: task.last_update_at || "",
        progress_pct: task.progress_pct,
        progress_pos_in_queue: task.progress_pos_in_queue,
        estimated_queue_wait_time: task.estimated_queue_wait_time,
        video_urls: Array.isArray(task.video_urls) ? task.video_urls : [],
        poll_interval_seconds: task.poll_interval_seconds || 5,
        timeout_seconds: task.timeout_seconds || 900,
        error_message: task.error_message || "",
        auto_rotate: !!task.auto_rotate,
        source_image_media_id: task.source_image_media_id || "",
        source_image_name: task.source_image_name || "",
        polling: false,
        raw_result: null
      };
    });
  } catch (_) {
    return [];
  }
}

function getSoraVideoTaskIndex(taskId) {
  for (var i = 0; i < soraVideoTasks.length; i += 1) {
    if (soraVideoTasks[i].task_id === taskId) return i;
  }
  return -1;
}

function getSoraVideoTask(taskId) {
  var index = getSoraVideoTaskIndex(taskId);
  return index >= 0 ? soraVideoTasks[index] : null;
}

function ensureSelectedSoraVideoTask() {
  if (soraVideoSelectedTaskId && getSoraVideoTask(soraVideoSelectedTaskId)) return;
  soraVideoSelectedTaskId = soraVideoTasks.length ? soraVideoTasks[0].task_id : "";
}

function extractSoraVideoResultMessage(result) {
  var data = (result && result.data) || {};
  var error = (data && data.error) || {};
  if (typeof data.message === "string" && data.message.trim()) return data.message.trim();
  if (typeof error.message === "string" && error.message.trim()) return error.message.trim();
  if (typeof error.code === "string" && error.code.trim()) return error.code.trim();
  if (typeof result.message === "string" && result.message.trim()) return result.message.trim();
  return "";
}

function buildSoraVideoTaskFromResult(result, meta) {
  var seed = meta || {};
  var view = getSoraVideoView(result);
  var data = (view && view.data) || (result && result.data) || {};
  var normalizedStatus = normalizeSoraVideoStatus(view.normalized_status || result.normalized_status || data.status || result.status || seed.normalized_status || "");
  var rawStatus = (view.status || result.status || data.status || seed.status || "").toString().trim();
  var isSuccess = typeof view.is_success === "boolean" ? view.is_success : (typeof result.is_success === "boolean" ? result.is_success : isSoraVideoSuccessStatus(normalizedStatus || rawStatus));
  var isTerminal = typeof view.is_terminal === "boolean" ? view.is_terminal : (typeof result.is_terminal === "boolean" ? result.is_terminal : isSoraVideoTerminalStatus(normalizedStatus || rawStatus));
  var progressPct = parseNumeric(data.progress_pct);
  if (isSuccess) progressPct = 100;
  return {
    task_id: (result.task_id || view.task_id || seed.task_id || "").trim(),
    prompt: data.prompt || seed.prompt || "",
    task_mode: normalizeSoraKeyScope((seed.task_mode || result.task_mode || view.task_mode || (seed.source_image_media_id || result.source_image_media_id ? SORA_KEY_SCOPE_IMAGE : SORA_KEY_SCOPE_TEXT))),
    task_family: (result.task_family || view.task_family || seed.task_family || "").trim(),
    status: rawStatus,
    normalized_status: normalizedStatus || rawStatus,
    is_terminal: !!isTerminal,
    is_success: !!isSuccess,
    used_account_id: result.used_account_id || view.used_account_id || seed.used_account_id || null,
    used_email: result.used_email || view.used_email || seed.used_email || "",
    created_local_at: seed.created_local_at || new Date().toISOString(),
    remote_created_at: data.created_at || seed.remote_created_at || "",
    last_update_at: new Date().toISOString(),
    progress_pct: progressPct,
    progress_pos_in_queue: parseNumeric(data.progress_pos_in_queue),
    estimated_queue_wait_time: parseNumeric(data.estimated_queue_wait_time),
    video_urls: Array.isArray(view.video_urls) ? view.video_urls : (Array.isArray(result.video_urls) ? result.video_urls : []),
    poll_interval_seconds: seed.poll_interval_seconds || 5,
    timeout_seconds: seed.timeout_seconds || 900,
    error_message: !result.ok ? (extractSoraVideoResultMessage(result) || ("HTTP " + String(result.status_code || ""))) : "",
    auto_rotate: !!seed.auto_rotate,
    source_image_media_id: result.source_image_media_id || view.source_image_media_id || seed.source_image_media_id || "",
    source_image_name: seed.source_image_name || "",
    polling: !isTerminal,
    raw_result: result
  };
}

function upsertSoraVideoTask(taskPatch) {
  var patch = taskPatch || {};
  if (!patch.task_id) return null;
  var index = getSoraVideoTaskIndex(patch.task_id);
  var current = index >= 0 ? soraVideoTasks[index] : null;
  var next = {
    task_id: patch.task_id,
    prompt: patch.prompt || (current ? current.prompt : ""),
    task_mode: patch.task_mode || (current ? current.task_mode : SORA_KEY_SCOPE_TEXT),
    task_family: patch.task_family != null ? patch.task_family : (current ? current.task_family : ""),
    status: patch.status || (current ? current.status : ""),
    normalized_status: patch.normalized_status || (current ? current.normalized_status : ""),
    is_terminal: typeof patch.is_terminal === "boolean" ? patch.is_terminal : (current ? current.is_terminal : false),
    is_success: typeof patch.is_success === "boolean" ? patch.is_success : (current ? current.is_success : false),
    used_account_id: patch.used_account_id != null ? patch.used_account_id : (current ? current.used_account_id : null),
    used_email: patch.used_email || (current ? current.used_email : ""),
    created_local_at: patch.created_local_at || (current ? current.created_local_at : new Date().toISOString()),
    remote_created_at: patch.remote_created_at || (current ? current.remote_created_at : ""),
    last_update_at: patch.last_update_at || new Date().toISOString(),
    progress_pct: patch.progress_pct != null ? patch.progress_pct : (current ? current.progress_pct : null),
    progress_pos_in_queue: patch.progress_pos_in_queue != null ? patch.progress_pos_in_queue : (current ? current.progress_pos_in_queue : null),
    estimated_queue_wait_time: patch.estimated_queue_wait_time != null ? patch.estimated_queue_wait_time : (current ? current.estimated_queue_wait_time : null),
    video_urls: Array.isArray(patch.video_urls) ? patch.video_urls : (current ? current.video_urls : []),
    poll_interval_seconds: patch.poll_interval_seconds || (current ? current.poll_interval_seconds : 5),
    timeout_seconds: patch.timeout_seconds || (current ? current.timeout_seconds : 900),
    error_message: patch.error_message != null ? patch.error_message : (current ? current.error_message : ""),
    auto_rotate: typeof patch.auto_rotate === "boolean" ? patch.auto_rotate : (current ? current.auto_rotate : false),
    source_image_media_id: patch.source_image_media_id != null ? patch.source_image_media_id : (current ? current.source_image_media_id : ""),
    source_image_name: patch.source_image_name != null ? patch.source_image_name : (current ? current.source_image_name : ""),
    polling: typeof patch.polling === "boolean" ? patch.polling : (current ? current.polling : false),
    raw_result: patch.raw_result || (current ? current.raw_result : null)
  };
  if (index >= 0) soraVideoTasks[index] = next;
  else soraVideoTasks.unshift(next);
  soraVideoTasks = soraVideoTasks.slice(0, 18);
  ensureSelectedSoraVideoTask();
  persistSoraVideoWorkspace();
  return next;
}

function upsertSoraVideoTaskFromResult(result, meta) {
  return upsertSoraVideoTask(buildSoraVideoTaskFromResult(result, meta));
}

function getSoraVideoTaskElapsedSeconds(task) {
  var startedMs = parseSoraDateMs((task && task.remote_created_at) || (task && task.created_local_at) || "");
  if (!startedMs) return 0;
  return Math.max(0, Math.round((Date.now() - startedMs) / 1000));
}

function getSoraVideoTaskProgressPercent(task) {
  if (!task) return 0;
  if (parseNumeric(task.progress_pct) != null) return Math.max(0, Math.min(100, parseNumeric(task.progress_pct)));
  if (task.is_success) return 100;
  if (task.is_terminal) return 100;
  if (task.normalized_status === "running") return 62;
  if (task.normalized_status === "queued") return 18;
  return 8;
}

function getSoraVideoTaskProgressText(task) {
  if (!task) return "0%";
  if (parseNumeric(task.progress_pct) != null) return String(Math.round(parseNumeric(task.progress_pct))) + "%";
  if (task.is_success) return "100%";
  if (task.normalized_status === "running") return "生成中";
  if (task.normalized_status === "queued") return "排队中";
  return task.normalized_status || "等待中";
}

function getSoraVideoTaskQueueText(task) {
  if (!task) return "等待中";
  var parts = [];
  if (task.progress_pos_in_queue != null) parts.push("队列 #" + String(task.progress_pos_in_queue));
  if (task.estimated_queue_wait_time != null) parts.push("预计 " + formatDuration(task.estimated_queue_wait_time));
  if (parts.length) return parts.join(" · ");
  if (task.is_success) return "已完成";
  if (task.is_terminal) return "已结束";
  if (task.normalized_status === "running") return "正在生成";
  return "等待中";
}

function setSelectedSoraVideoTask(taskId) {
  soraVideoSelectedTaskId = (taskId || "").trim();
  ensureSelectedSoraVideoTask();
  persistSoraVideoWorkspace();
  renderSoraVideoWorkspace();
}

function renderSoraVideoOverview() {
  var box = document.getElementById("sora-video-overview");
  if (!box) return;
  var activeCount = soraVideoTasks.filter(function(task) { return !task.is_terminal; }).length;
  var successCount = soraVideoTasks.filter(function(task) { return task.is_success; }).length;
  var pollingCount = Object.keys(soraVideoPollers).length;
  box.innerHTML = [
    { label: "任务总数", value: String(soraVideoTasks.length) },
    { label: "并行中", value: String(activeCount) },
    { label: "轮询中", value: String(pollingCount) },
    { label: "自动轮换", value: isSoraVideoAutoRotateEnabled() ? "已开启" : "手动账号" }
  ].map(function(item) {
    return '<div class="video-overview-item"><span>' + escapeHtml(item.label) + '</span><strong>' + escapeHtml(item.value) + '</strong></div>';
  }).join("");
}

function renderSoraVideoStage() {
  var mediaEl = document.getElementById("sora-video-stage-media");
  var titleEl = document.getElementById("sora-video-stage-title");
  var promptEl = document.getElementById("sora-video-stage-prompt");
  var statusEl = document.getElementById("sora-video-stage-status");
  var progressFillEl = document.getElementById("sora-video-stage-progress-fill");
  var progressTextEl = document.getElementById("sora-video-stage-progress-text");
  var elapsedEl = document.getElementById("sora-video-stage-elapsed");
  var queueEl = document.getElementById("sora-video-stage-queue");
  var accountEl = document.getElementById("sora-video-stage-account");
  var createdEl = document.getElementById("sora-video-stage-created");
  var hintEl = document.getElementById("sora-video-stage-hint");
  var kickerEl = document.getElementById("sora-video-stage-kicker");
  var task = getSoraVideoTask(soraVideoSelectedTaskId);
  if (!task) {
    if (mediaEl) {
      mediaEl.innerHTML = '<div class="video-stage-placeholder"><div class="video-stage-placeholder-icon">Sora</div><p>还没有任务，点击右上角黄色按钮开始生成。</p></div>';
      mediaEl.setAttribute("data-task-id", "");
      mediaEl.setAttribute("data-preview-url", "");
    }
    if (titleEl) titleEl.textContent = "选择一个任务进行预览";
    if (promptEl) promptEl.textContent = "生成完成后，视频会显示在这个大框里；任务未完成时会显示当前进度和时间。";
    if (statusEl) { statusEl.textContent = "idle"; statusEl.className = "sora-status-badge is-pending"; }
    if (progressFillEl) progressFillEl.style.width = "0%";
    if (progressTextEl) progressTextEl.textContent = "0%";
    if (elapsedEl) elapsedEl.textContent = "00:00";
    if (queueEl) queueEl.textContent = "等待中";
    if (accountEl) accountEl.textContent = "自动选择";
    if (createdEl) createdEl.textContent = "创建时间 --";
    if (hintEl) hintEl.textContent = "支持多任务并行轮询";
    if (kickerEl) kickerEl.textContent = "等待任务";
    return;
  }
  var previewUrl = pickSoraPreviewUrl(task.video_urls);
  if (mediaEl) {
    if (previewUrl) {
      var renderedTaskId = mediaEl.getAttribute("data-task-id") || "";
      var renderedPreviewUrl = mediaEl.getAttribute("data-preview-url") || "";
      var videoEl = mediaEl.querySelector("video");
      if (renderedTaskId !== task.task_id || renderedPreviewUrl !== previewUrl || !videoEl) {
        mediaEl.innerHTML = '<video controls preload="auto" playsinline crossorigin="anonymous" src="' + escapeHtml(previewUrl) + '"></video>';
        mediaEl.setAttribute("data-task-id", task.task_id);
        mediaEl.setAttribute("data-preview-url", previewUrl);
        videoEl = mediaEl.querySelector("video");
        if (videoEl) {
          videoEl.addEventListener("loadeddata", function() {
            delete soraVideoMediaReloadAttempts[task.task_id];
          }, { once: true });
          videoEl.addEventListener("error", function() {
            var attempt = soraVideoMediaReloadAttempts[task.task_id] || 0;
            if (attempt >= 1) return;
            soraVideoMediaReloadAttempts[task.task_id] = attempt + 1;
            refreshSoraVideoTaskSnapshot(task.task_id, {
              message: "视频预览地址已刷新，正在重新加载...",
              resetMediaRetry: true
            }).catch(function() {});
          }, { once: true });
        }
      }
    } else {
      mediaEl.innerHTML = '<div class="video-stage-placeholder"><div class="video-stage-placeholder-icon">' + escapeHtml(task.normalized_status || "task") + '</div><p>' + escapeHtml(task.prompt || "任务已创建，正在等待更多进度。") + '</p></div>';
      mediaEl.setAttribute("data-task-id", task.task_id);
      mediaEl.setAttribute("data-preview-url", "");
    }
  }
  if (titleEl) titleEl.textContent = trimVideoPrompt(task.prompt || task.task_id, 54) || task.task_id;
  if (promptEl) promptEl.textContent = task.prompt || "这个任务当前还没有返回 prompt。";
  if (statusEl) {
    statusEl.textContent = task.normalized_status || "unknown";
    statusEl.className = "sora-status-badge " + (task.is_success ? "is-success" : (task.is_terminal ? "is-failed" : "is-pending"));
  }
  if (progressFillEl) progressFillEl.style.width = String(getSoraVideoTaskProgressPercent(task)) + "%";
  if (progressTextEl) progressTextEl.textContent = getSoraVideoTaskProgressText(task);
  if (elapsedEl) elapsedEl.textContent = formatDuration(getSoraVideoTaskElapsedSeconds(task));
  if (queueEl) queueEl.textContent = getSoraVideoTaskQueueText(task);
  if (accountEl) accountEl.textContent = task.used_email ? (String(task.used_account_id || "--") + " · " + task.used_email) : (task.used_account_id ? String(task.used_account_id) : "自动选择");
  if (createdEl) createdEl.textContent = "创建时间 " + formatSoraDateTime(task.remote_created_at || task.created_local_at || "");
  if (hintEl) hintEl.textContent = task.error_message || (task.is_success ? "预览默认优先使用更顺畅的中低码率流" : (task.is_terminal ? "任务已结束" : (task.polling ? "后台自动轮询中" : "点击任务卡可继续轮询")));
  if (kickerEl) {
    var modeLabel = task.task_mode === SORA_KEY_SCOPE_IMAGE ? "图生视频" : "文生视频";
    var familyLabel = task.task_family === "nf2" ? "官方 App" : "旧链路";
    kickerEl.textContent = (task.is_success ? "预览就绪" : (task.is_terminal ? "任务结束" : "实时进度")) + " · " + modeLabel + " · " + familyLabel;
  }
}

function renderSoraVideoTaskList() {
  var listEl = document.getElementById("sora-video-task-list");
  if (!listEl) return;
  if (!soraVideoTasks.length) {
    listEl.innerHTML = '<div class="video-task-empty">还没有任务。点击右上角黄色按钮创建后，会自动进入这里并并行轮询。</div>';
    return;
  }
  var tasks = soraVideoTasks.slice().sort(function(a, b) {
    if (!!a.is_terminal !== !!b.is_terminal) return a.is_terminal ? 1 : -1;
    return parseSoraDateMs(b.remote_created_at || b.created_local_at || "") - parseSoraDateMs(a.remote_created_at || a.created_local_at || "");
  });
  listEl.innerHTML = tasks.map(function(task) {
    var progress = getSoraVideoTaskProgressPercent(task);
    var statusClass = task.is_success ? "is-success" : (task.is_terminal ? "is-failed" : "is-pending");
    var familyLabel = task.task_family === "nf2" ? "官方 App" : "旧链路";
    return (
      '<div class="video-task-card' + (task.task_id === soraVideoSelectedTaskId ? ' is-active' : '') + '" data-video-task-select="' + escapeHtml(task.task_id) + '">' +
        '<div class="video-task-card-head">' +
          '<div>' +
            '<p class="video-task-card-title">' + escapeHtml(trimVideoPrompt(task.prompt || task.task_id, 28) || task.task_id) + '</p>' +
            '<p class="video-task-card-subtitle">' + escapeHtml(task.task_mode === SORA_KEY_SCOPE_IMAGE ? '图生视频' : '文生视频') + ' · ' + escapeHtml(familyLabel) + ' · task_id ' + escapeHtml(task.task_id) + '</p>' +
          '</div>' +
          '<span class="sora-status-badge ' + statusClass + '">' + escapeHtml(task.normalized_status || 'unknown') + '</span>' +
        '</div>' +
        '<p class="video-task-prompt">' + escapeHtml(task.prompt || "这个任务当前还没有返回 prompt。") + '</p>' +
        '<div class="video-task-progress-track"><span class="video-task-progress-fill" style="width:' + String(progress) + '%;"></span></div>' +
        '<div class="video-task-progress-meta"><span>进度 ' + escapeHtml(getSoraVideoTaskProgressText(task)) + '</span><span>耗时 ' + escapeHtml(formatDuration(getSoraVideoTaskElapsedSeconds(task))) + '</span></div>' +
        '<div class="video-task-meta"><span>' + escapeHtml(getSoraVideoTaskQueueText(task)) + '</span><span>账号 ' + escapeHtml(task.used_account_id ? String(task.used_account_id) : '--') + '</span></div>' +
        '<div class="video-task-actions">' +
          '<button type="button" class="is-primary" data-video-task-focus="' + escapeHtml(task.task_id) + '">预览</button>' +
          '<button type="button" data-video-task-copy="' + escapeHtml(task.task_id) + '">复制 ID</button>' +
          '<button type="button" data-video-task-toggle="' + escapeHtml(task.task_id) + '">' + (task.polling ? "暂停刷新" : "继续刷新") + '</button>' +
        '</div>' +
      '</div>'
    );
  }).join("");
  listEl.querySelectorAll("[data-video-task-select], [data-video-task-focus]").forEach(function(node) {
    node.addEventListener("click", function(event) {
      var target = event.currentTarget.getAttribute("data-video-task-select") || event.currentTarget.getAttribute("data-video-task-focus") || "";
      if (target) setSelectedSoraVideoTask(target);
    });
  });
  listEl.querySelectorAll("[data-video-task-copy]").forEach(function(node) {
    node.addEventListener("click", function(event) {
      event.stopPropagation();
      var taskId = event.currentTarget.getAttribute("data-video-task-copy") || "";
      copyTextToClipboard(taskId, "task_id 已复制");
    });
  });
  listEl.querySelectorAll("[data-video-task-toggle]").forEach(function(node) {
    node.addEventListener("click", function(event) {
      event.stopPropagation();
      var taskId = event.currentTarget.getAttribute("data-video-task-toggle") || "";
      var task = getSoraVideoTask(taskId);
      if (!task) return;
      if (task.polling) stopSoraVideoTaskPolling(taskId, { message: "已暂停 " + taskId });
      else startSoraVideoTaskPolling(taskId);
    });
  });
}

function renderSoraVideoWorkspace() {
  ensureSelectedSoraVideoTask();
  renderSoraVideoOverview();
  renderSoraVideoStage();
  renderSoraVideoTaskList();
}

function startSoraVideoUiClock() {
  if (soraVideoUiClock) return;
  soraVideoUiClock = window.setInterval(function() {
    if (soraVideoTasks.length) renderSoraVideoWorkspace();
  }, 1000);
}

function stopSoraVideoTaskPolling(taskId, options) {
  var timer = soraVideoPollers[taskId];
  if (timer) {
    clearTimeout(timer);
    delete soraVideoPollers[taskId];
  }
  var task = getSoraVideoTask(taskId);
  if (task) {
    upsertSoraVideoTask({
      task_id: taskId,
      polling: false,
      error_message: options && options.message ? options.message : task.error_message
    });
  }
  if (options && options.toast) toast(options.toast, options.type || "info");
  if (options && options.message) {
    var msgEl = document.getElementById("sora-video-msg");
    if (msgEl) msgEl.textContent = options.message;
  }
  renderSoraVideoWorkspace();
}

function stopAllSoraVideoTaskPolling(options) {
  Object.keys(soraVideoPollers).forEach(function(taskId) {
    stopSoraVideoTaskPolling(taskId);
  });
  if (options && options.message) {
    var msgEl = document.getElementById("sora-video-msg");
    if (msgEl) msgEl.textContent = options.message;
  }
  if (options && options.toast) toast(options.toast, options.type || "info");
}

function fetchSoraVideoTask(taskId) {
  var accountId = getSoraAccountIdFromInput();
  function request(body) {
    return api("/api/sora-api/video-gen/get", {
      method: "POST",
      body: JSON.stringify(body)
    });
  }
  return request({ task_id: taskId }).catch(function(err) {
    var message = parseApiErrorMessage(err);
    if (accountId && (message.indexOf("缺少 access_token") >= 0 || message.indexOf("refresh_token") >= 0)) {
      return request({
        account_id: accountId,
        task_id: taskId
      });
    }
    throw err;
  });
}

function refreshSoraVideoTaskSnapshot(taskId, options) {
  var cleanTaskId = (taskId || "").trim();
  if (!cleanTaskId) return Promise.resolve(null);
  if (soraVideoSnapshotRefreshInFlight[cleanTaskId]) return Promise.resolve(getSoraVideoTask(cleanTaskId));
  soraVideoSnapshotRefreshInFlight[cleanTaskId] = true;
  var current = getSoraVideoTask(cleanTaskId) || { task_id: cleanTaskId };
  return fetchSoraVideoTask(cleanTaskId).then(function(result) {
    var nextTask = upsertSoraVideoTaskFromResult(result, current);
    if (nextTask && nextTask.used_account_id) setCurrentSoraAccountId(nextTask.used_account_id);
    if (options && options.resetMediaRetry) delete soraVideoMediaReloadAttempts[cleanTaskId];
    if (options && options.message) {
      var msgEl = document.getElementById("sora-video-msg");
      if (msgEl) msgEl.textContent = options.message;
    }
    renderSoraVideoWorkspace();
    return nextTask;
  }).catch(function(err) {
    var message = parseApiErrorMessage(err);
    upsertSoraVideoTask({ task_id: cleanTaskId, error_message: message });
    renderSoraVideoWorkspace();
    throw err;
  }).finally(function() {
    delete soraVideoSnapshotRefreshInFlight[cleanTaskId];
  });
}

function startSoraVideoTaskPolling(taskId) {
  var cleanTaskId = (taskId || "").trim();
  if (!cleanTaskId) return;
  var task = getSoraVideoTask(cleanTaskId);
  if (!task || task.is_terminal) {
    stopSoraVideoTaskPolling(cleanTaskId);
    return;
  }
  stopSoraVideoTaskPolling(cleanTaskId);
  upsertSoraVideoTask({ task_id: cleanTaskId, polling: true, error_message: "" });
  renderSoraVideoWorkspace();
  function tick() {
    var current = getSoraVideoTask(cleanTaskId);
    if (!current) return stopSoraVideoTaskPolling(cleanTaskId);
    var timeoutMs = Math.max(30000, Math.round((current.timeout_seconds || 900) * 1000));
    if (getSoraVideoTaskElapsedSeconds(current) * 1000 >= timeoutMs) {
      stopSoraVideoTaskPolling(cleanTaskId, { message: "任务 " + cleanTaskId + " 轮询超时" });
      return;
    }
    fetchSoraVideoTask(cleanTaskId).then(function(result) {
      var nextTask = upsertSoraVideoTaskFromResult(result, current);
      if (nextTask && nextTask.used_account_id) {
        setCurrentSoraAccountId(nextTask.used_account_id);
      }
      renderSoraVideoWorkspace();
      if (nextTask && nextTask.is_terminal) {
        stopSoraVideoTaskPolling(cleanTaskId, {
          message: nextTask.is_success ? ("任务 " + cleanTaskId + " 已成功出片") : ("任务 " + cleanTaskId + " 已结束"),
          toast: nextTask.is_success ? "有任务生成完成" : "",
          type: nextTask.is_success ? "success" : "info"
        });
        return;
      }
      var currentTask = getSoraVideoTask(cleanTaskId);
      if (!currentTask) return;
      soraVideoPollers[cleanTaskId] = window.setTimeout(tick, Math.max(1000, Math.round((currentTask.poll_interval_seconds || 5) * 1000)));
    }).catch(function(err) {
      var message = parseApiErrorMessage(err);
      upsertSoraVideoTask({ task_id: cleanTaskId, polling: true, error_message: message });
      renderSoraVideoWorkspace();
      var currentTask = getSoraVideoTask(cleanTaskId);
      if (!currentTask) return;
      soraVideoPollers[cleanTaskId] = window.setTimeout(tick, Math.max(1000, Math.round((currentTask.poll_interval_seconds || 5) * 1000)));
    });
  }
  tick();
}

function refreshAllSoraVideoTasks() {
  var pending = soraVideoTasks.filter(function(task) { return !task.is_terminal; });
  var completed = soraVideoTasks.filter(function(task) { return task.is_terminal; });
  if (!pending.length && !completed.length) {
    var msgEl = document.getElementById("sora-video-msg");
    if (msgEl) msgEl.textContent = "当前没有可刷新的任务";
    return;
  }
  pending.forEach(function(task) {
    startSoraVideoTaskPolling(task.task_id);
  });
  completed.forEach(function(task) {
    refreshSoraVideoTaskSnapshot(task.task_id, { resetMediaRetry: true }).catch(function() {});
  });
  var msgEl = document.getElementById("sora-video-msg");
  if (msgEl) msgEl.textContent = "已刷新全部任务，未完成任务继续轮询，已完成任务会重新获取预览地址";
}

function clearFinishedSoraVideoTasks() {
  soraVideoTasks = soraVideoTasks.filter(function(task) { return !task.is_terminal; });
  ensureSelectedSoraVideoTask();
  persistSoraVideoWorkspace();
  renderSoraVideoWorkspace();
}

function openSoraVideoComposer() {
  var overlay = document.getElementById("sora-video-compose-overlay");
  if (overlay) overlay.classList.remove("hidden");
  updateSoraVideoComposerMode();
  var promptEl = document.getElementById("sora-video-prompt");
  if (promptEl) window.setTimeout(function() { promptEl.focus(); }, 30);
}

function closeSoraVideoComposer() {
  var overlay = document.getElementById("sora-video-compose-overlay");
  if (overlay) overlay.classList.add("hidden");
  var msgEl = document.getElementById("sora-video-compose-msg");
  if (msgEl) msgEl.textContent = "";
}

function createSoraVideoRequestBody(payload) {
  var body = {
    prompt: payload.prompt,
    auto_rotate: payload.autoRotate,
    task_family: payload.taskFamily || SORA_TASK_FAMILY_VIDEO_GEN,
    n_variants: payload.n_variants,
    n_frames: payload.n_frames,
    resolution: payload.resolution,
    orientation: payload.orientation,
    source_image_media_id: payload.source_image_media_id || ""
  };
  if (payload.audio_caption) body.audio_caption = payload.audio_caption;
  if (payload.audio_transcript) body.audio_transcript = payload.audio_transcript;
  if (!payload.autoRotate) body.account_id = payload.account_id;
  return body;
}

function createSoraVideoTasks() {
  var composeMsgEl = document.getElementById("sora-video-compose-msg");
  var globalMsgEl = document.getElementById("sora-video-msg");
  var buttonEl = document.getElementById("btn-sora-video-create");
  var payload;
  if (soraVideoCreateInFlight) return;
  try {
    payload = getSoraVideoComposerPayload();
  } catch (err) {
    var message = parseApiErrorMessage(err);
    if (composeMsgEl) composeMsgEl.textContent = message;
    toast(message, "info");
    return;
  }
  soraVideoCreateInFlight = true;
  if (buttonEl) buttonEl.disabled = true;
  if (composeMsgEl) composeMsgEl.textContent = "正在发起 " + payload.batchCount + " 条任务...";
  if (globalMsgEl) globalMsgEl.textContent = "正在创建任务并加入并行队列...";
  var requests = [];
  for (var i = 0; i < payload.batchCount; i += 1) {
    if (payload.taskMode === SORA_KEY_SCOPE_IMAGE) {
      var form = new FormData();
      form.append("prompt", payload.prompt);
      form.append("auto_rotate", payload.autoRotate ? "true" : "false");
      form.append("n_variants", String(payload.n_variants));
      form.append("n_frames", String(payload.n_frames));
      form.append("resolution", String(payload.resolution));
      form.append("orientation", payload.orientation);
      form.append("file", payload.imageFile, payload.imageFile.name || ("image-" + String(i + 1) + ".png"));
      if (!payload.autoRotate && payload.account_id) form.append("account_id", String(payload.account_id));
      requests.push(apiForm("/api/sora-api/video-gen/create-with-image", form));
    } else {
      requests.push(api("/api/sora-api/video-gen/create", {
        method: "POST",
        body: JSON.stringify(createSoraVideoRequestBody(payload))
      }));
    }
  }
  Promise.allSettled(requests).then(function(results) {
    var successCount = 0;
    var failures = [];
    results.forEach(function(entry) {
      if (entry.status !== "fulfilled") {
        failures.push(parseApiErrorMessage(entry.reason));
        return;
      }
      var result = entry.value || {};
      var taskId = (result.task_id || "").trim();
      if (!result.ok || !taskId) {
        failures.push(extractSoraVideoResultMessage(result) || ("HTTP " + String(result.status_code || "")));
        return;
      }
      var task = upsertSoraVideoTaskFromResult(result, {
        task_id: taskId,
        prompt: payload.prompt,
        task_mode: payload.taskMode,
        used_account_id: result.used_account_id || null,
        used_email: result.used_email || "",
        created_local_at: new Date().toISOString(),
        poll_interval_seconds: payload.pollIntervalSeconds,
        timeout_seconds: payload.timeoutSeconds,
        auto_rotate: payload.autoRotate,
        source_image_media_id: result.source_image_media_id || "",
        source_image_name: payload.imageFile ? payload.imageFile.name : ""
      });
      if (task && task.used_account_id) {
        setCurrentSoraAccountId(task.used_account_id);
        loadSoraAccountDetails(task.used_account_id, { silent: true, skipKeyList: true }).catch(function() {});
      }
      if (successCount === 0 && task) setSelectedSoraVideoTask(task.task_id);
      if (task) startSoraVideoTaskPolling(task.task_id);
      successCount += 1;
    });
    renderSoraVideoWorkspace();
    if (successCount > 0) {
      if (composeMsgEl) composeMsgEl.textContent = "已创建 " + successCount + " 条任务，正在并行轮询...";
      if (globalMsgEl) globalMsgEl.textContent = "已创建 " + successCount + " 条任务，任务墙会继续显示进度和耗时";
      toast("已创建 " + successCount + " 条任务", "success");
      if (!failures.length) window.setTimeout(closeSoraVideoComposer, 250);
    } else {
      if (composeMsgEl) composeMsgEl.textContent = "创建失败：" + failures.join("；");
      if (globalMsgEl) globalMsgEl.textContent = "创建失败：" + failures.join("；");
    }
    if (failures.length) {
      toast(failures[0], "error");
    }
  }).finally(function() {
    soraVideoCreateInFlight = false;
    if (buttonEl) buttonEl.disabled = false;
  });
}

function importSoraVideoTask() {
  var taskId = getSoraVideoTaskId();
  var msgEl = document.getElementById("sora-video-msg");
  if (!taskId) {
    if (msgEl) msgEl.textContent = "请先输入 task_id";
    toast("请先输入 task_id", "info");
    return;
  }
  upsertSoraVideoTask({
    task_id: taskId,
    prompt: "",
    created_local_at: new Date().toISOString(),
    poll_interval_seconds: getSoraVideoPollOptions().pollIntervalSeconds,
    timeout_seconds: getSoraVideoPollOptions().timeoutSeconds,
    polling: true
  });
  setSelectedSoraVideoTask(taskId);
  startSoraVideoTaskPolling(taskId);
  if (msgEl) msgEl.textContent = "已把 " + taskId + " 加入任务墙并开始轮询";
}

(function initSoraVideoTool() {
  renderSoraVideoAccountSummary(null);
  setSoraVideoAutoRotateEnabled(localStorage.getItem(SORA_VIDEO_AUTO_ROTATE_STORAGE_KEY) !== "0");
  soraVideoTasks = loadPersistedSoraVideoTasks();
  ensureSelectedSoraVideoTask();
  renderSoraVideoWorkspace();
  startSoraVideoUiClock();
  var savedTaskId = localStorage.getItem("sora_video_last_task_id");
  if (savedTaskId) setSoraVideoTaskId(savedTaskId);
  soraVideoTasks.forEach(function(task) {
    if (!task.is_terminal) startSoraVideoTaskPolling(task.task_id);
  });
  var selectedTask = getSoraVideoTask(soraVideoSelectedTaskId);
  if (selectedTask && selectedTask.is_terminal) {
    refreshSoraVideoTaskSnapshot(selectedTask.task_id, { resetMediaRetry: true }).catch(function() {});
  }
  document.getElementById("sora-video-task-id").addEventListener("change", function() {
    setSoraVideoTaskId(this.value || "");
  });
  document.getElementById("sora-video-auto-rotate").addEventListener("change", function() {
    setSoraVideoAutoRotateEnabled(this.checked);
    renderSoraVideoOverview();
  });
  document.getElementById("sora-video-task-mode").addEventListener("change", updateSoraVideoComposerMode);
  document.getElementById("sora-video-image-file").addEventListener("change", updateSoraVideoImageMeta);
  document.getElementById("btn-sora-video-open-composer").addEventListener("click", openSoraVideoComposer);
  document.getElementById("btn-sora-video-compose-close").addEventListener("click", closeSoraVideoComposer);
  document.getElementById("btn-sora-video-compose-backdrop").addEventListener("click", closeSoraVideoComposer);
  document.getElementById("btn-sora-video-create").addEventListener("click", createSoraVideoTasks);
  document.getElementById("btn-sora-video-import-task").addEventListener("click", importSoraVideoTask);
  document.getElementById("btn-sora-video-refresh-all").addEventListener("click", refreshAllSoraVideoTasks);
  document.getElementById("btn-sora-video-stop-all").addEventListener("click", function() {
    stopAllSoraVideoTaskPolling({
      message: "已暂停全部任务轮询",
      toast: "已暂停全部轮询",
      type: "info"
    });
  });
  document.getElementById("btn-sora-video-clear-finished").addEventListener("click", clearFinishedSoraVideoTasks);
  updateSoraVideoComposerMode();
})();

document.getElementById("btn-go-video-page").addEventListener("click", function() {
  showPage("video");
});

// Emails
function loadEmails() {
  document.getElementById("email-api-balance").textContent = "--";
  document.getElementById("email-api-msg").textContent = "";
  api("/api/email-api/balance").then((d) => {
    document.getElementById("email-api-balance").textContent = String(d.balance);
  }).catch(() => {
    document.getElementById("email-api-balance").textContent = "未配置或请求失败";
  });
  api("/api/emails").then((d) => {
    document.getElementById("emails-tbody").innerHTML = (d.items || [])
      .map(
        (r) =>
          `<tr>
        <td>${r.id}</td>
        <td>${escapeHtml(r.email)}</td>
        <td>
          <span>${escapeHtml(r.password || "")}</span>
          ${r.password ? `<button type="button" class="btn-op btn-copy-email-password" data-password="${encodeURIComponent(r.password || "")}">复制</button>` : ""}
        </td>
        <td>${escapeHtml((r.uuid || "").slice(0, 12))}</td>
        <td>${r.registered ? '<span class="status-registered">已注册</span>' : '<span class="status-unregistered">未注册</span>'}</td>
        <td>
          <button type="button" class="btn-op btn-op-view" data-id="${r.id}">查看邮件</button>
          <button type="button" class="btn-op danger" data-id="${r.id}">删除邮箱</button>
        </td>
      </tr>`
      )
      .join("");
    document.getElementById("emails-tbody").querySelectorAll(".btn-op-view").forEach((btn) => {
      btn.addEventListener("click", () => {
        const id = btn.dataset.id;
        showModal('<div class="email-view-card"><p>正在获取邮件列表…</p></div>');
        var modalContent = document.querySelector(".modal-content");
        if (modalContent) modalContent.classList.add("modal-content-wide");
        api("/api/email-api/mail-list?email_id=" + encodeURIComponent(id))
          .then((d) => {
            var list = d.list || [];
            function renderMailDetail(mail) {
              var isObj = mail && typeof mail === "object" && !Array.isArray(mail);
              var subject = isObj && (mail.subject != null || mail.title != null) ? (mail.subject ?? mail.title) : "";
              var body = isObj && (mail.body != null || mail.content != null || mail.text != null || mail.Text != null) ? (mail.body ?? mail.content ?? mail.text ?? mail.Text) : "";
              var from = isObj && mail.from != null ? mail.from : "";
              var date = isObj && mail.date != null ? mail.date : "";
              var html = isObj && (mail.html != null || mail.Html != null) ? (mail.html ?? mail.Html) : "";
              var previewHtml = "";
              if (html) previewHtml = "<div class=\"email-body-html\">" + html + "</div>";
              else if (body) previewHtml = "<pre class=\"email-body\">" + escapeHtml(String(body)) + "</pre>";
              else if (isObj) previewHtml = "<pre class=\"email-body\">" + escapeHtml(JSON.stringify(mail, null, 2)) + "</pre>";
              else previewHtml = "<pre class=\"email-body\">" + escapeHtml(String(mail)) + "</pre>";
              var rawHtml = "<pre class=\"email-body\">" + escapeHtml(JSON.stringify(mail, null, 2)) + "</pre>";
              return '<p><strong>发件人</strong> ' + escapeHtml(String(from)) + '</p><p><strong>主题</strong> ' + escapeHtml(String(subject)) + (date ? '</p><p><strong>时间</strong> ' + escapeHtml(String(date)) : '') + '</p><div class="email-view-tabs"><button type="button" class="email-tab active" data-tab="preview">正常显示</button><button type="button" class="email-tab" data-tab="raw">源文件</button></div><div class="email-tab-panel" id="email-panel-preview">' + previewHtml + '</div><div class="email-tab-panel hidden" id="email-panel-raw">' + rawHtml + "</div>";
            }
            function bindTabSwitch() {
              document.querySelectorAll(".email-view-detail .email-tab").forEach(function(tab) {
                tab.onclick = function() {
                  document.querySelectorAll(".email-view-detail .email-tab").forEach(function(t) { t.classList.remove("active"); });
                  document.querySelectorAll(".email-view-detail .email-tab-panel").forEach(function(p) { p.classList.add("hidden"); });
                  this.classList.add("active");
                  var pid = "email-panel-" + this.getAttribute("data-tab");
                  var panel = document.getElementById(pid);
                  if (panel) panel.classList.remove("hidden");
                };
              });
            }
            var listHtml = '<div class="email-view-list"><div class="email-view-list-title">邮件列表</div><div class="email-view-list-inner">';
            if (list.length === 0) {
              listHtml += '<p class="email-view-empty">收件箱暂无邮件或 API 未返回</p>';
            } else {
              list.forEach(function(m, i) {
                var subj = (m.subject != null || m.title != null) ? (m.subject ?? m.title) : "(无主题)";
                var fr = m.from != null ? m.from : "";
                var dt = m.date != null ? m.date : "";
                listHtml += '<div class="email-view-list-item' + (i === 0 ? ' active' : '') + '" data-index="' + i + '"><div class="email-view-list-item-subject">' + escapeHtml(String(subj).slice(0, 28)) + (String(subj).length > 28 ? "…" : "") + '</div><div class="email-view-list-item-meta">' + escapeHtml(String(fr).slice(0, 20)) + (String(fr).length > 20 ? "…" : "") + (dt ? " · " + escapeHtml(String(dt).slice(0, 12)) : "") + '</div></div>';
              });
            }
            listHtml += "</div></div>";
            var detailHtml = '<div class="email-view-detail"><div class="email-view-detail-inner">';
            if (list.length === 0) {
              detailHtml += '<p class="email-view-empty">收件箱暂无邮件，或该邮箱尚未收到新邮件；当前仅能拉取最新 1 封，更多请登录 Outlook 查看。</p>';
            } else {
              detailHtml += renderMailDetail(list[0]);
            }
            detailHtml += '<p class="email-view-fallback"><a href="https://outlook.live.com" target="_blank" rel="noopener">在 Outlook 登录</a> 可查看全部邮件</p></div></div>';
            document.getElementById("modal-body").innerHTML = '<div class="email-view-card email-view-layout">' + listHtml + detailHtml + "</div>";
            bindTabSwitch();
            document.querySelectorAll(".email-view-list-item").forEach(function(item) {
              item.addEventListener("click", function() {
                var idx = parseInt(this.getAttribute("data-index"), 10);
                var mail = list[idx];
                if (!mail) return;
                document.querySelectorAll(".email-view-list-item").forEach(function(el) { el.classList.remove("active"); });
                this.classList.add("active");
                var inner = document.querySelector(".email-view-detail-inner");
                if (inner) {
                  inner.innerHTML = renderMailDetail(mail) + '<p class="email-view-fallback"><a href="https://outlook.live.com" target="_blank" rel="noopener">在 Outlook 登录</a> 可查看全部邮件</p>';
                  bindTabSwitch();
                }
              });
            });
          })
          .catch((err) => {
            if (modalContent) modalContent.classList.remove("modal-content-wide");
            document.getElementById("modal-body").innerHTML =
              '<div class="email-view-card"><p class="error">' + escapeHtml(err.message || "获取失败") + '</p><p><a href="https://outlook.live.com" target="_blank" rel="noopener">在 Outlook 登录</a> 查看邮件</p></div>';
          });
      });
    });
    document.getElementById("emails-tbody").querySelectorAll(".btn-op.danger").forEach((btn) => {
      btn.addEventListener("click", () => {
        confirmBox("确定删除该邮箱？", function() {
            api("/api/emails/" + btn.dataset.id, { method: "DELETE" }).then(() => { toast("已删除"); loadEmails(); });
          });
      });
    });
    document.getElementById("emails-tbody").querySelectorAll(".btn-copy-email-password").forEach((btn) => {
      btn.addEventListener("click", () => {
        var pwd = decodeURIComponent(btn.dataset.password || "");
        copyTextToClipboard(pwd, "邮箱密码已复制");
      });
    });
  });
}
document.getElementById("btn-add-email").addEventListener("click", () => {
  showModal(`
    <form id="email-form">
      <label>邮箱 <input type="text" name="email" required /></label>
      <label>密码 <input type="password" name="password" /></label>
      <label>UUID <input type="text" name="uuid" /></label>
      <label>Token <input type="text" name="token" /></label>
      <label>备注 <input type="text" name="remark" /></label>
      <button type="submit">添加</button>
    </form>
  `);
  document.getElementById("email-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    api("/api/emails", {
      method: "POST",
      body: JSON.stringify({
        email: fd.get("email"),
        password: fd.get("password"),
        uuid: fd.get("uuid"),
        token: fd.get("token"),
        remark: fd.get("remark"),
      }),
    }).then(() => { hideModal(); loadEmails(); });
  });
});
document.getElementById("btn-batch-import-email").addEventListener("click", () => {
  showModal(`
    <p>每行一条：邮箱----密码----UUID----Token</p>
    <textarea id="email-import-lines" rows="12" style="width:100%;background:#2c3036;border:1px solid #3d4248;color:#e4e6e8;padding:0.5rem;font-family:monospace;"></textarea>
    <button type="button" id="email-import-submit">导入</button>
  `);
  document.getElementById("email-import-submit").addEventListener("click", () => {
    const lines = document.getElementById("email-import-lines").value;
    api("/api/emails/batch-import", { method: "POST", body: JSON.stringify({ lines }) }).then((d) => {
      hideModal();
      toast("已导入 " + d.added + " 条");
      loadEmails();
    });
  });
});
document.getElementById("btn-batch-export-email").addEventListener("click", () => {
  api("/api/emails/export").then((d) => {
    const items = d.items || [];
    const lines = items.map((r) => [r.email, r.password || "", r.uuid || "", r.token || ""].join("----"));
    const blob = new Blob([lines.join("\n")], { type: "text/plain;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "emails-" + new Date().toISOString().slice(0, 10) + ".txt";
    a.click();
    URL.revokeObjectURL(a.href);
    toast("已导出 " + items.length + " 条");
  }).catch((err) => toast("导出失败: " + (err.message || "请求错误"), "error"));
});
document.getElementById("link-to-settings").addEventListener("click", function(e) {
  e.preventDefault();
  showPage("settings");
});
document.getElementById("btn-email-api-stock").addEventListener("click", function() {
  const mailType = document.getElementById("email-api-mail-type").value;
  const msg = document.getElementById("email-api-msg");
  msg.textContent = "查询中...";
  api("/api/email-api/stock?mailType=" + encodeURIComponent(mailType)).then((d) => {
    msg.textContent = "库存：" + d.stock + "（" + (d.mail_type || "全部") + "）";
  }).catch((err) => {
    msg.textContent = "失败：" + (err.message || "请求错误");
  });
});
document.getElementById("btn-email-api-fetch").addEventListener("click", function() {
  const mailType = document.getElementById("email-api-mail-type").value;
  const quantity = parseInt(document.getElementById("email-api-quantity").value, 10) || 1;
  const msg = document.getElementById("email-api-msg");
  msg.textContent = "拉取中...";
  api("/api/email-api/fetch-mail", {
    method: "POST",
    body: JSON.stringify({ mail_type: mailType, quantity, import_to_emails: true }),
  }).then((d) => {
    msg.textContent = "拉取 " + d.count + " 条，已导入 " + d.imported + " 条";
    if (d.imported) loadEmails();
  }).catch((err) => {
    msg.textContent = "失败：" + (err.message || "请求错误");
  });
});

// Bank cards
function loadBankCards() {
  api("/api/bank-cards").then((d) => {
    document.getElementById("cards-tbody").innerHTML = (d.items || [])
      .map(
        (r) =>
          `<tr>
        <td><input type="checkbox" class="card-id" value="${r.id}" /></td>
        <td>${r.id}</td>
        <td>${escapeHtml(r.card_number_masked || "")}</td>
        <td>${r.used_count}/${r.max_use_count}</td>
        <td>${escapeHtml(r.remark || "")}</td>
        <td><button type="button" class="btn-link danger" data-id="${r.id}">删除</button></td>
      </tr>`
      )
      .join("");
    document.getElementById("cards-tbody").querySelectorAll(".btn-link.danger").forEach((btn) => {
      btn.addEventListener("click", () => {
        confirmBox("确定删除该银行卡？", function() {
            api("/api/bank-cards/" + btn.dataset.id, { method: "DELETE" }).then(() => { toast("已删除"); loadBankCards(); });
          });
      });
    });
  });
}
document.getElementById("btn-add-card").addEventListener("click", () => {
  showModal(`
    <form id="card-form">
      <label>卡号(掩码) <input type="text" name="card_number_masked" placeholder="****1234" /></label>
      <label>使用次数上限 <input type="number" name="max_use_count" value="1" min="1" /></label>
      <label>备注 <input type="text" name="remark" /></label>
      <button type="submit">添加</button>
    </form>
  `);
  document.getElementById("card-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    api("/api/bank-cards", {
      method: "POST",
      body: JSON.stringify({
        card_number_masked: fd.get("card_number_masked"),
        card_data: fd.get("card_number_masked"),
        max_use_count: parseInt(fd.get("max_use_count") || 1, 10),
        remark: fd.get("remark"),
      }),
    }).then(() => { hideModal(); loadBankCards(); });
  });
});
document.getElementById("btn-batch-import-card").addEventListener("click", () => {
  showModal(`
    <p>每行一条卡信息（掩码或后四位），使用次数从系统设置读取</p>
    <textarea id="card-import-lines" rows="12" style="width:100%;background:#2c3036;border:1px solid #3d4248;color:#e4e6e8;padding:0.5rem;font-family:monospace;"></textarea>
    <button type="button" id="card-import-submit">导入</button>
  `);
  document.getElementById("card-import-submit").addEventListener("click", () => {
    const lines = document.getElementById("card-import-lines").value;
    api("/api/bank-cards/batch-import", { method: "POST", body: JSON.stringify({ lines }) }).then((d) => {
      hideModal();
      toast("已导入 " + d.added + " 条");
      loadBankCards();
    });
  });
});
document.getElementById("btn-batch-delete-card").addEventListener("click", () => {
  const ids = Array.from(document.querySelectorAll(".card-id:checked")).map((c) => parseInt(c.value, 10));
  if (!ids.length) { toast("请先勾选要删除的卡", "info"); return; }
  confirmBox("确定删除已选 " + ids.length + " 条银行卡？", function() {
    api("/api/bank-cards/batch-delete", { method: "POST", body: JSON.stringify({ ids }) }).then(() => {
      toast("已删除");
      loadBankCards();
    });
  });
});

// Phones
document.getElementById("link-to-settings-phones").addEventListener("click", function(e) {
  e.preventDefault();
  showPage("settings");
});
function refreshSmsApiSummary() {
  var balanceEl = document.getElementById("sms-api-balance");
  var countEl = document.getElementById("sms-api-openai-count");
  var msgEl = document.getElementById("sms-api-msg");
  balanceEl.textContent = "--";
  countEl.textContent = "--";
  msgEl.textContent = "";
  api("/api/sms-api/openai-availability").then(function(d) {
    balanceEl.textContent = String(d.balance != null ? d.balance : 0);
    countEl.textContent = String(d.total_count != null ? d.total_count : 0);
    if (d.service_hint && d.service_hint.length) {
      msgEl.textContent = "当前服务代号不被支持。可用代号: " + d.service_hint.join(", ") + "，请到系统设置修改「OpenAI 服务 ID」";
    }
  }).catch(function() {
    balanceEl.textContent = "未配置或失败";
    countEl.textContent = "--";
  });
}
function formatExpiredAtLocal(utcStr) {
  if (!utcStr) return "—";
  var s = String(utcStr).trim();
  if (s.indexOf("Z") === -1 && s.indexOf("+") === -1 && s.indexOf("-") >= 0) s = s.replace(" ", "T") + "Z";
  var d = new Date(s);
  if (isNaN(d.getTime())) return utcStr;
  return d.toLocaleString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}
function loadPhones() {
  refreshSmsApiSummary();
  var tbody = document.getElementById("phones-tbody");
  api("/api/phones?_=" + Date.now()).then((d) => {
    var items = d.items || [];
    tbody.innerHTML = items
      .map(
        (r) =>
          "<tr>" +
          "<td><input type=\"checkbox\" class=\"phone-id\" value=\"" + r.id + "\" /></td>" +
          "<td>" + r.id + "</td>" +
          "<td>" + escapeHtml(r.phone || "") + "</td>" +
          "<td>" + (r.used_count != null ? r.used_count : 0) + "/" + (r.max_use_count != null ? r.max_use_count : 1) + "</td>" +
          "<td>" + escapeHtml(formatExpiredAtLocal(r.expired_at)) + "</td>" +
          "<td>" + escapeHtml(r.remark || "") + "</td>" +
          "<td>" +
          "<button type=\"button\" class=\"btn-op sms-code\" data-id=\"" + r.id + "\">收码</button> " +
          "<button type=\"button\" class=\"btn-op release-phone\" data-id=\"" + r.id + "\">销毁</button> " +
          "<button type=\"button\" class=\"btn-op danger\" data-id=\"" + r.id + "\">删除</button>" +
          "</td>" +
          "</tr>"
      )
      .join("");
    tbody.querySelectorAll(".btn-op.sms-code").forEach(function(btn) {
      btn.addEventListener("click", function() {
        var id = btn.dataset.id;
        btn.disabled = true;
        btn.textContent = "查询中...";
        api("/api/phones/" + id + "/sms-code").then(function(d) {
          btn.disabled = false;
          btn.textContent = "收码";
          if (d.code) showModal("<p><strong>短信验证码</strong></p><p style=\"font-size:1.5rem;letter-spacing:0.2em;font-weight:600;\">" + escapeHtml(d.code) + "</p><p style=\"color:var(--text-muted);font-size:12px;\">" + (d.message || "") + "</p>");
          else toast(d.message || "等待短信中", "info");
        }).catch(function(e) {
          btn.disabled = false;
          btn.textContent = "收码";
          toast(e.message || "失败", "info");
        });
      });
    });
    tbody.querySelectorAll(".btn-op.release-phone").forEach(function(btn) {
      btn.addEventListener("click", function() {
        confirmBox("确定销毁该号码？将通知接码平台取消并从列表移除。", function() {
          api("/api/phones/" + btn.dataset.id + "/release", { method: "POST" }).then(function() {
            toast("已销毁");
            loadPhones();
          }).catch(function(e) { toast(e.message || "失败", "info"); });
        });
      });
    });
    tbody.querySelectorAll(".btn-op.danger").forEach(function(btn) {
      btn.addEventListener("click", function() {
        confirmBox("确定删除该手机号？", function() {
          api("/api/phones/" + btn.dataset.id, { method: "DELETE" }).then(function() {
            toast("已删除");
            loadPhones();
          });
        });
      });
    });
  }).catch(function(err) {
    tbody.innerHTML = "<tr><td colspan=\"7\">加载失败：" + escapeHtml(err.message || "请求错误") + "</td></tr>";
  });
}
document.getElementById("btn-add-phone").addEventListener("click", function() {
  showModal(
    "<form id=\"phone-form\">" +
      "<label>手机号 <input type=\"text\" name=\"phone\" required placeholder=\"+86 或 国家码+号码\" /></label>" +
      "<label>可绑定次数 <input type=\"number\" name=\"max_use_count\" value=\"1\" min=\"1\" /></label>" +
      "<label>备注 <input type=\"text\" name=\"remark\" /></label>" +
      "<button type=\"submit\">添加</button>" +
    "</form>"
  );
  document.getElementById("phone-form").addEventListener("submit", function(e) {
    e.preventDefault();
    var fd = new FormData(e.target);
    api("/api/phones", {
      method: "POST",
      body: JSON.stringify({
        phone: fd.get("phone"),
        max_use_count: parseInt(fd.get("max_use_count") || 1, 10),
        remark: fd.get("remark"),
      }),
    }).then(function() { hideModal(); toast("已添加"); loadPhones(); });
  });
});
document.getElementById("btn-batch-import-phone").addEventListener("click", function() {
  showModal(
    "<p>每行一个手机号，可绑定次数使用系统设置中的「手机号绑定数」。</p>" +
    "<textarea id=\"phone-import-lines\" rows=\"12\" style=\"width:100%;padding:0.5rem;font-family:monospace;background:var(--bg-input);border:1px solid var(--border);border-radius:8px;\"></textarea>" +
    "<button type=\"button\" id=\"phone-import-submit\">导入</button>"
  );
  document.getElementById("phone-import-submit").addEventListener("click", function() {
    var lines = document.getElementById("phone-import-lines").value;
    api("/api/phones/batch-import", { method: "POST", body: JSON.stringify({ lines }) }).then(function(d) {
      hideModal();
      toast("已导入 " + d.added + " 条");
      loadPhones();
    });
  });
});
document.getElementById("btn-batch-delete-phone").addEventListener("click", function() {
  var ids = Array.from(document.querySelectorAll(".phone-id:checked")).map(function(c) { return parseInt(c.value, 10); });
  if (!ids.length) { toast("请先勾选要删除的手机号", "info"); return; }
  confirmBox("确定删除已选 " + ids.length + " 个手机号？", function() {
    api("/api/phones/batch-delete", { method: "POST", body: JSON.stringify({ ids }) }).then(function() {
      toast("已删除");
      loadPhones();
    });
  });
});
document.getElementById("btn-sms-api-test").addEventListener("click", function() {
  var msgEl = document.getElementById("sms-api-msg");
  msgEl.textContent = "测试中...";
  api("/api/sms-api/balance").then(function(d) {
    msgEl.textContent = "接口正常，余额：" + d.balance;
  }).catch(function(err) {
    msgEl.textContent = "失败：" + (err.message || "请求错误");
  });
});
document.getElementById("btn-sms-api-refresh-openai").addEventListener("click", function() {
  refreshSmsApiSummary();
});
document.getElementById("btn-sms-api-debug-prices").addEventListener("click", function() {
  var msgEl = document.getElementById("sms-api-msg");
  msgEl.textContent = "加载中...";
  api("/api/sms-api/openai-availability?debug=1").then(function(d) {
    msgEl.textContent = "";
    var raw = d.prices_raw;
    var text = raw === undefined ? "(无 prices_raw)" : JSON.stringify(raw, null, 2);
    var desc = "<p class=\"modal-desc\">接码平台 <strong>getPrices</strong> 接口的原始返回（当前「OpenAI 服务 ID」下的价格/库存）。若「OpenAI 可用数量」一直为 0，可据此核对返回结构或到系统设置中修改服务代号。</p>";
    showModal(desc + "<pre style=\"max-height:70vh;overflow:auto;white-space:pre-wrap;word-break:break-all;font-size:12px;\">" + escapeHtml(text) + "</pre>");
  }).catch(function(err) {
    msgEl.textContent = "失败：" + (err.message || "请求错误");
  });
});
document.getElementById("btn-sms-api-services").addEventListener("click", function() {
  var msgEl = document.getElementById("sms-api-msg");
  var country = parseInt(document.getElementById("sms-api-country").value, 10) || 0;
  msgEl.textContent = "加载中...";
  api("/api/sms-api/services?country=" + country).then(function(d) {
    msgEl.textContent = "";
    var list = d.services || [];
    var text = list.length ? JSON.stringify(list, null, 2) : "(空)，请检查 API 与 country";
    showModal("<p>接码平台服务列表（country=" + country + "），请找到 OpenAI 对应的 id 或 shortName 填到系统设置「OpenAI 服务 ID」：</p><pre style=\"max-height:70vh;overflow:auto;white-space:pre-wrap;word-break:break-all;font-size:12px;\">" + escapeHtml(text) + "</pre>");
  }).catch(function(err) {
    msgEl.textContent = "失败：" + (err.message || "请求错误");
  });
});
document.getElementById("btn-sms-api-get-numbers").addEventListener("click", function() {
  var msgEl = document.getElementById("sms-api-msg");
  var quantity = parseInt(document.getElementById("sms-api-get-quantity").value, 10) || 1;
  var country = parseInt(document.getElementById("sms-api-country").value, 10) || 0;
  msgEl.textContent = "获取中...";
  api("/api/sms-api/get-numbers", {
    method: "POST",
    body: JSON.stringify({ country: country, quantity: quantity }),
  }).then(function(d) {
    if (d.got) {
      msgEl.textContent = "已获取 " + d.got + " 个号码并加入列表";
    } else {
      var errMsg = (d.errors && d.errors[0]) ? ("获取失败：" + d.errors[0]) : "已获取 0 个号码并加入列表";
      if (d.errors && d.errors[0] === "BAD_SERVICE") errMsg += "（请到系统设置将「OpenAI 服务 ID」改为 dr 并保存）";
      msgEl.textContent = errMsg;
    }
    loadPhones();
  }).catch(function(err) {
    msgEl.textContent = "失败：" + (err.message || "请求错误");
  });
});

// 批量注册 - 仪表盘与日志
function loadDashboard() {
  api("/api/dashboard").then(function(d) {
    document.getElementById("dash-today").textContent = d.today_registered != null ? d.today_registered : 0;
    document.getElementById("dash-total").textContent = d.total_registered != null ? d.total_registered : 0;
    document.getElementById("dash-phone").textContent = d.phone_bound_count != null ? d.phone_bound_count : 0;
    document.getElementById("dash-plus").textContent = d.plus_count != null ? d.plus_count : 0;
    document.getElementById("dash-sora-available").textContent = d.sora_available_count != null ? d.sora_available_count : 0;
    document.getElementById("dash-sora-daily-capacity").textContent = d.today_generatable_videos != null ? d.today_generatable_videos : 0;
    document.getElementById("dash-sora-generated-today").textContent = d.today_generated_videos != null ? d.today_generated_videos : 0;
    document.getElementById("dash-success").textContent = d.success_count != null ? d.success_count : 0;
    document.getElementById("dash-fail").textContent = d.fail_count != null ? d.fail_count : 0;
    document.getElementById("dash-email-api").textContent = d.email_api_set ? "已设置" : "未设置";
    document.getElementById("dash-sms-api").textContent = d.sms_api_set ? "已设置" : "未设置";
    document.getElementById("dash-bank-api").textContent = d.bank_api_set ? "已设置" : "未设置";
    document.getElementById("dash-captcha-api").textContent = d.captcha_api_set ? "已设置" : "未设置";
    document.getElementById("dash-threads").textContent = d.thread_count != null ? d.thread_count : "1";
    api("/api/phone-bind/status").then(function(s) {
      var stopBtn = document.getElementById("btn-stop-bind-phone");
      if (stopBtn) stopBtn.style.display = (s && s.running) ? "" : "none";
    }).catch(function() {});
  }).catch(function() {
    document.getElementById("dash-today").textContent = "—";
    document.getElementById("dash-total").textContent = "—";
    document.getElementById("dash-phone").textContent = "—";
    document.getElementById("dash-plus").textContent = "—";
    document.getElementById("dash-sora-available").textContent = "—";
    document.getElementById("dash-sora-daily-capacity").textContent = "—";
    document.getElementById("dash-sora-generated-today").textContent = "—";
    document.getElementById("dash-success").textContent = "—";
    document.getElementById("dash-fail").textContent = "—";
    document.getElementById("dash-email-api").textContent = "—";
    document.getElementById("dash-sms-api").textContent = "—";
    document.getElementById("dash-bank-api").textContent = "—";
    document.getElementById("dash-captcha-api").textContent = "—";
    document.getElementById("dash-threads").textContent = "—";
  });
}
var currentLogLimit = 20;
function loadLogs(limit) {
  if (limit != null && limit !== undefined) currentLogLimit = Math.min(Math.max(Number(limit) || 20, 1), 100);
  limit = currentLogLimit;
  api("/api/logs?page=1&page_size=" + limit).then(function(d) {
    var list = document.getElementById("log-list");
    var items = d.items || [];
    var total = d.total || 0;
    var titleEl = document.getElementById("log-panel-title");
    var expandEl = document.getElementById("log-expand-area");
    if (titleEl) titleEl.textContent = "最近 " + limit + " 条日志";
    list.classList.toggle("log-list-expanded", limit > 20);
    list.innerHTML = items.length ? items.map(function(r) {
      var levelClass = (r.level === "error") ? " log-line--error" : " log-line--info";
      return "<div class=\"log-line" + levelClass + "\"><span class=\"ts\">" + escapeHtml(r.created_at) + "</span> " + escapeHtml(r.message) + "</div>";
    }).join("") : "<div class=\"log-line log-line--info\">暂无日志</div>";
    if (expandEl) {
      if (limit < 100 && total > 20) {
        expandEl.innerHTML = "<button type=\"button\" id=\"btn-expand-logs\" class=\"log-panel-expand-btn\"><span class=\"log-panel-expand-icon\" aria-hidden=\"true\">▼</span> 展开更多（最多 100 条）</button>";
        expandEl.style.display = "";
        expandEl.className = "log-panel-expand";
        document.getElementById("btn-expand-logs").addEventListener("click", function() { loadLogs(100); });
      } else if (limit === 100 && total > 20) {
        expandEl.innerHTML = "<span class=\"log-panel-expand-done\">已显示最多 100 条</span> <button type=\"button\" id=\"btn-collapse-logs\" class=\"log-panel-collapse-btn\">收起</button>";
        expandEl.style.display = "";
        expandEl.className = "log-panel-expand log-panel-expand--done";
        document.getElementById("btn-collapse-logs").addEventListener("click", function() { loadLogs(20); });
      } else {
        expandEl.innerHTML = "";
        expandEl.style.display = "none";
        expandEl.className = "log-panel-expand";
      }
    }
  }).catch(function() {
    document.getElementById("log-list").innerHTML = "<div class=\"log-line log-line--error\">加载失败</div>";
    var expandEl = document.getElementById("log-expand-area");
    if (expandEl) { expandEl.innerHTML = ""; expandEl.style.display = "none"; }
  });
}
document.getElementById("btn-start-register").addEventListener("click", function() {
  if (this.disabled) return;
  api("/api/register/start", { method: "POST" }).then(function(d) {
    if (d && d.ok) {
      toast(d.message || "已启动注册任务", "success");
      updateRegisterStatusOnce();
      loadDashboard();
      loadLogs();
    } else {
      toast(d.message || "启动失败", "error");
    }
  }).catch(function(err) {
    toast(err.message || "请求失败", "error");
  });
});
document.getElementById("btn-stop-register").addEventListener("click", function() {
  api("/api/register/stop", { method: "POST" }).then(function(d) {
    if (d && d.ok) {
      toast(d.message || "已请求停止", "info");
      updateRegisterStatusOnce();
    } else {
      toast(d.message || "操作失败", "error");
    }
  }).catch(function(err) {
    toast(err.message || "请求失败", "error");
  });
});
document.getElementById("btn-start-bind-phone").addEventListener("click", function() {
  var btn = this;
  var stopBtn = document.getElementById("btn-stop-bind-phone");
  var bindCountEl = document.getElementById("phone-bind-max-count");
  var bindCountRaw = bindCountEl ? String(bindCountEl.value || "").trim() : "";
  var bindCount = null;
  var url = "/api/phone-bind/start";
  if (bindCountRaw !== "") {
    bindCount = parseInt(bindCountRaw, 10);
    if (!isFinite(bindCount) || bindCount < 1) {
      toast("绑定数量必须是大于 0 的整数", "error");
      return;
    }
    bindCount = Math.min(bindCount, 100);
    if (bindCountEl) bindCountEl.value = String(bindCount);
    url += "?max_count=" + encodeURIComponent(bindCount);
  }
  btn.disabled = true;
  api(url, { method: "POST" }).then(function(d) {
    if (d.ok) {
      var msg = "绑定任务已启动";
      if (bindCount != null) msg += "，按 " + bindCount + " 并发执行，目标成功 " + bindCount + " 个";
      msg += "，task_id: " + (d.task_id || "");
      toast(msg, "success");
      if (stopBtn) stopBtn.style.display = "";
      loadDashboard();
      loadLogs();
    } else {
      toast(d.message || "启动失败", "info");
    }
  }).catch(function(err) {
    toast(err.message || "请求失败", "error");
  }).finally(function() {
    btn.disabled = false;
  });
});
document.getElementById("btn-stop-bind-phone").addEventListener("click", function() {
  api("/api/phone-bind/stop", { method: "POST" }).then(function(d) {
    toast(d.message || "已请求停止", "info");
  }).catch(function(err) {
    toast(err.message || "请求失败", "error");
  });
});
document.getElementById("btn-start-plus").addEventListener("click", function() {
  toast("开始开通 Plus 功能开发中", "info");
});
document.getElementById("btn-refresh-dashboard").addEventListener("click", function() {
  loadDashboard();
  loadLogs();
});
document.getElementById("btn-clear-logs").addEventListener("click", function() {
  confirmBox("确定清空所有日志？", function() {
    api("/api/logs", { method: "DELETE" }).then(function(d) {
      toast(d.message || "已清空日志", "success");
      loadDashboard();
      loadLogs();
    }).catch(function(err) {
      toast(err.message || "清空失败", "error");
    });
  });
});

// Settings
var SETTINGS_KEYS = [
  "sms_api_url", "sms_api_key", "sms_openai_service", "sms_max_price", "thread_count", "proxy_url", "proxy_api_url",
  "bank_card_api_url", "bank_card_api_key", "bank_card_api_platform", "email_api_url", "email_api_key", "email_api_default_type",
  "captcha_api_url", "captcha_api_key", "oauth_client_id", "oauth_redirect_uri",
  "retry_count", "card_use_limit", "phone_bind_limit"
];
function loadSettings() {
  api("/api/settings").then((d) => {
    const form = document.getElementById("settings-form");
    SETTINGS_KEYS.forEach((k) => {
      const el = form.querySelector(`[name="${k}"]`);
      if (el) el.value = d[k] != null ? d[k] : "";
    });
  });
}
document.getElementById("settings-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const body = {};
  SETTINGS_KEYS.forEach((k) => { body[k] = fd.get(k) || ""; });
  api("/api/settings", { method: "PUT", body: JSON.stringify(body) })
    .then(() => {
      toast("已保存");
      loadSettings();
    })
    .catch((err) => {
      toast(err && err.message ? err.message : "保存失败");
    });
});

function escapeHtml(s) {
  if (s == null) return "";
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

// 点击用户名弹出修改账号密码
document.getElementById("current-user").addEventListener("click", function() {
  showModal(`
    <div class="login-update-modal">
      <h3 class="login-update-title">修改登录账号</h3>
      <p class="login-update-desc">保存后需重新登录。</p>
      <form id="login-update-form">
        <label>新账号 <input type="text" name="admin_username" placeholder="请输入新登录账号" required autocomplete="username" /></label>
        <label>新密码 <input type="password" name="admin_password" placeholder="请输入新密码" required autocomplete="new-password" /></label>
        <div class="login-update-actions">
          <button type="submit" class="login-update-btn">保存</button>
        </div>
      </form>
    </div>
  `);
  document.getElementById("login-update-form").addEventListener("submit", function(e) {
    e.preventDefault();
    var fd = new FormData(this);
    var username = (fd.get("admin_username") || "").toString().trim();
    var password = (fd.get("admin_password") || "").toString();
    if (!username || !password) {
      toast("账号与密码均不能为空", "error");
      return;
    }
    api("/api/settings/login", { method: "PUT", body: JSON.stringify({ admin_username: username, admin_password: password }) })
      .then(function() {
        hideModal();
        toast("已修改，请重新登录");
        localStorage.removeItem("admin_token");
        window.location.reload();
      })
      .catch(function(err) {
        toast(err.message || "保存失败", "error");
      });
  });
});

// Default tab（登录后默认打开批量注册）
if (token) showPage("logs");
