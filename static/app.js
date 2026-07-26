(() => {
  // id only — do NOT pass "#foo"
  const $ = (id) => document.getElementById(String(id).replace(/^#/, ""));

  const boot = window.__BOOT__ || {};
  const state = {
    devices: boot.devices || [],
    events: boot.events || [],
    stats: boot.stats || {},
    settings: boot.settings || {},
    editMac: null,
    statusFilter: "all", // all | online | offline
    detailMac: null,
  };

  const tbodyDev = $("devTable") && $("devTable").querySelector("tbody");
  const tbodyEvt = $("evtTable") && $("evtTable").querySelector("tbody");
  const filterEl = $("filter");
  const errBox = $("errBox");
  const okBox = $("okBox");
  const wsState = $("wsState");
  const keyState = $("keyState");
  const panelTitle = $("devicePanelTitle");
  const btnClearFilter = $("btnClearFilter");

  if (!tbodyDev || !tbodyEvt || !filterEl) {
    console.error("DOM missing: check element ids");
    return;
  }

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  function fmtBJ(raw) {
    // 北京时间：YYYY年MM月DD日 HH:mm（不要秒）
    if (raw == null || raw === "" || raw === "-" || raw === "0" || raw === "None") return "—";
    let s = String(raw).trim();
    // 已是中文格式 → 去掉尾部 :ss
    if (s.includes("年") && s.includes("月")) {
      const m2 = s.match(/^(\d{4}年\d{1,2}月\d{1,2}日\s+\d{1,2}:\d{2})(?::\d{2})?/);
      return m2 ? m2[1] : s;
    }
    const m = s.match(
      /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?/
    );
    if (m) {
      return `${m[1]}年${m[2]}月${m[3]}日 ${m[4]}:${m[5]}`;
    }
    try {
      const d = new Date(s);
      if (!Number.isNaN(d.getTime())) {
        const parts = new Intl.DateTimeFormat("zh-CN", {
          timeZone: "Asia/Shanghai",
          year: "numeric",
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
          hour12: false,
        }).formatToParts(d);
        const get = (t) => (parts.find((p) => p.type === t) || {}).value || "";
        return `${get("year")}年${get("month")}月${get("day")}日 ${get("hour")}:${get("minute")}`;
      }
    } catch (_) {}
    return s;
  }

  function showOk(msg) {
    if (!okBox) return;
    okBox.style.display = "block";
    okBox.textContent = msg;
    setTimeout(() => {
      if (okBox.textContent === msg) {
        okBox.style.display = "none";
        okBox.textContent = "";
      }
    }, 4000);
  }

  function renderKeyBadge() {
    if (!keyState) return;
    const s = state.settings || {};
    if (s.wskey_set) {
      keyState.textContent = "WSKEY " + (s.wskey_masked || "已配置");
      keyState.className = "badge ok";
    } else {
      keyState.textContent = "WSKEY 未配置";
      keyState.className = "badge bad";
    }
  }

  function setStatusFilter(f) {
    state.statusFilter = f || "all";
    document.querySelectorAll(".card.clickable").forEach((el) => {
      el.classList.toggle("active", el.dataset.filter === state.statusFilter);
    });
    if (panelTitle) {
      const map = { all: "设备表 · 全部", online: "设备表 · 在线", offline: "设备表 · 离线" };
      panelTitle.textContent = map[state.statusFilter] || "设备表";
    }
    if (btnClearFilter) {
      btnClearFilter.style.display = state.statusFilter === "all" ? "none" : "inline-block";
    }
    renderDevices();
    const panel = $("devicePanel");
    if (panel) panel.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderStats() {
    const s = state.stats || {};
    const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
    set("sOnline", s.devices_online ?? "-");
    set("sOffline", s.devices_offline ?? "-");
    set("sTotal", s.devices_total ?? "-");
    set("sPoll", s.last_poll ? fmtBJ(s.last_poll) : "尚未成功");
    if (errBox) {
      if (s.last_error) {
        errBox.style.display = "block";
        errBox.textContent = "错误: " + s.last_error;
      } else if (errBox.dataset.sticky !== "1") {
        errBox.style.display = "none";
        errBox.textContent = "";
      }
    }
    renderKeyBadge();
  }

  function filteredDevices() {
    const q = (filterEl.value || "").trim().toLowerCase();
    return (state.devices || []).filter((d) => {
      if (state.statusFilter === "online" && !d.online) return false;
      if (state.statusFilter === "offline" && d.online) return false;
      if (!q) return true;
      const blob = [d.display_name, d.alias, d.hostname, d.mac, d.ip, d.router_name, d.router_mac]
        .join(" ").toLowerCase();
      return blob.includes(q);
    });
  }

  function fmtDuration(on, lastOnline, lastOffline) {
    if (!lastOnline || lastOnline === "—") return "—";
    const now = new Date();
    const lo = new Date(lastOnline);
    if (isNaN(lo.getTime())) return "—";
    let end;
    if (on) {
      end = now;
    } else {
      if (!lastOffline || lastOffline === "—") return "—";
      end = new Date(lastOffline);
      if (isNaN(end.getTime())) return "—";
    }
    const diffMs = end - lo;
    if (diffMs < 0) return "—";
    const totalMinutes = Math.floor(diffMs / 60000);
    if (totalMinutes < 1) return "刚刚";
    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;
    const days = Math.floor(hours / 24);
    const remainHours = hours % 24;
    if (days > 0) return `${days}天${remainHours}小时`;
    if (hours > 0) return `${hours}小时${minutes ? minutes + "分" : ""}`;
    return `${minutes}分钟`;
  }

  function renderDevices() {
    const rows = filteredDevices();
    if (!rows.length) {
      tbodyDev.innerHTML = `<tr><td colspan="9" class="muted">无匹配设备</td></tr>`;
      return;
    }
    tbodyDev.innerHTML = rows.map((d) => `
      <tr data-mac="${esc(d.mac)}">
        <td><span class="pill ${d.online ? "on" : "off"}">${d.online ? "在线" : "离线"}</span></td>
        <td>
          <button type="button" class="name-link btn-detail" data-mac="${esc(d.mac)}">
            ${esc(d.display_name || d.mac)}
          </button>
        </td>
        <td>
          <div>${esc(d.hostname || "—")}</div>
          <div class="muted mono host-mac">${esc(d.mac)}</div>
        </td>
        <td class="mono">${esc(d.ip || "")}</td>
        <td>${esc(d.router_name || "")}<div class="muted mono">${esc(d.router_mac || "")}</div></td>
        <td class="muted">${esc(fmtBJ(d.last_online))}</td>
        <td>${fmtDuration(d.online, d.last_online, d.last_offline)}</td>
        <td class="muted">${esc(fmtBJ(d.last_offline))}</td>
        <td>
          <button class="linkish btn-detail" type="button" data-mac="${esc(d.mac)}">详情</button>
          <button class="linkish btn-alias" type="button" data-mac="${esc(d.mac)}" data-name="${esc(d.alias || d.display_name || "")}">改名</button>
        </td>
      </tr>
    `).join("");
  }

  function renderEvents() {
    tbodyEvt.innerHTML = (state.events || []).map((e) => `
      <tr>
        <td class="muted">${esc(fmtBJ(e.ts))}</td>
        <td><span class="pill ${e.event === "online" ? "ev-on" : "ev-off"}">${e.event === "online" ? "上线" : "离线"}</span></td>
        <td>
          <button type="button" class="name-link btn-detail" data-mac="${esc(e.mac)}">
            ${esc(e.display_name || e.hostname || e.mac)}
          </button>
        </td>
        <td class="mono">${esc(e.mac)}</td>
        <td class="mono">${esc(e.ip || "")}</td>
        <td class="muted">${esc(e.note || "")}</td>
      </tr>
    `).join("");
  }

  function renderAll() {
    renderStats();
    renderDevices();
    renderEvents();
  }

  async function openDeviceDetail(mac) {
    state.detailMac = mac;
    $("detailTitle").textContent = "加载中…";
    $("detailMac").textContent = mac;
    $("detailBody").innerHTML = "";
    $("detailEvtTable").querySelector("tbody").innerHTML = "";
    $("detailDlg").showModal();
    try {
      const r = await fetch("/api/device/" + encodeURIComponent(mac) + "?limit=500");
      const data = await r.json();
      if (!data.ok) throw new Error(data.error || r.status);
      const d = data.device;
      $("detailTitle").textContent = d.display_name || d.hostname || d.mac;
      $("detailMac").textContent = d.mac;
      $("detailBody").innerHTML = `
        <div class="kv">
          <div><span>状态</span><b class="${d.online ? "ok" : "bad"}">${d.online ? "在线" : "离线"}</b></div>
          <div><span>自定义名</span><b>${esc(d.alias || "（未设置）")}</b></div>
          <div><span>主机名</span><b>${esc(d.hostname || "—")}</b></div>
          <div><span>IP</span><b class="mono">${esc(d.ip || "—")}</b></div>
          <div><span>路由器</span><b>${esc(d.router_name || "—")}</b><div class="muted mono">${esc(d.router_mac || "")}</div></div>
          <div><span>最新上线时间</span><b>${esc(fmtBJ(d.last_online))}</b></div>
          <div><span>在线时长</span><b>${fmtDuration(d.online, d.last_online, d.last_offline)}</b></div>
          <div><span>最后离线</span><b>${esc(fmtBJ(d.last_offline))}</b></div>
          <div><span>首次发现</span><b>${esc(fmtBJ(d.first_seen))}</b></div>
          <div><span>最后探测</span><b>${esc(fmtBJ(d.last_seen))}</b></div>
          <div><span>记录保留</span><b>近 ${data.event_retention_days || 30} 天</b></div>
        </div>
      `;
      const tb = $("detailEvtTable").querySelector("tbody");
      const evs = data.events || [];
      if (!evs.length) {
        tb.innerHTML = `<tr><td colspan="4" class="muted">近 30 天无上下线事件</td></tr>`;
      } else {
        tb.innerHTML = evs.map((e) => `
          <tr>
            <td class="muted">${esc(fmtBJ(e.ts))}</td>
            <td><span class="pill ${e.event === "online" ? "ev-on" : "ev-off"}">${e.event === "online" ? "上线" : "离线"}</span></td>
            <td class="mono">${esc(e.ip || "")}</td>
            <td class="muted">${esc(e.note || "")}</td>
          </tr>
        `).join("");
      }
    } catch (e) {
      $("detailBody").innerHTML = `<p class="err">加载失败: ${esc(e)}</p>`;
    }
  }

  function fillSettingsForm() {
    const s = state.settings || {};
    if ($("wskeyStatus")) {
      $("wskeyStatus").textContent = s.wskey_set
        ? ("已配置 · " + (s.wskey_masked || ""))
        : "未配置";
    }
    if ($("fWskey")) $("fWskey").value = "";
    if ($("fClearWskey")) $("fClearWskey").checked = false;
    if ($("fPollInterval")) $("fPollInterval").value = s.poll_interval ?? 45;
    if ($("fOfflineMisses")) $("fOfflineMisses").value = s.offline_misses ?? 2;
    if ($("fWatchMacs")) $("fWatchMacs").value = s.watch_macs || "";
    if ($("fRouterMacs")) $("fRouterMacs").value = s.router_macs || "";
    if ($("fHttpProxy")) $("fHttpProxy").value = s.http_proxy || "";
    if ($("fHttpsProxy")) $("fHttpsProxy").value = s.https_proxy || "";
    if ($("fPollNow")) $("fPollNow").checked = true;
    if ($("fUiPassword")) $("fUiPassword").value = "";
    if ($("fClearUiPassword")) $("fClearUiPassword").checked = false;
    if ($("uiPassStatus")) {
      $("uiPassStatus").textContent = s.ui_password_set ? "已启用登录密码" : "未启用（任何人可访问）";
    }
  }

  // stats cards click
  document.querySelectorAll(".card.clickable").forEach((el) => {
    el.addEventListener("click", () => setStatusFilter(el.dataset.filter));
  });
  if (btnClearFilter) {
    btnClearFilter.addEventListener("click", () => setStatusFilter("all"));
  }

  // device table / events click (detail + alias)
  document.body.addEventListener("click", (ev) => {
    const detailBtn = ev.target.closest(".btn-detail");
    if (detailBtn) {
      openDeviceDetail(detailBtn.dataset.mac);
      return;
    }
    const aliasBtn = ev.target.closest(".btn-alias");
    if (aliasBtn) {
      state.editMac = aliasBtn.dataset.mac;
      $("aliasMac").textContent = state.editMac;
      $("aliasName").value = aliasBtn.dataset.name || "";
      $("aliasDlg").showModal();
    }
  });

  $("detailClose").addEventListener("click", () => $("detailDlg").close());
  $("detailClose2").addEventListener("click", () => $("detailDlg").close());
  $("detailAlias").addEventListener("click", () => {
    if (!state.detailMac) return;
    const d = (state.devices || []).find((x) => x.mac === state.detailMac);
    state.editMac = state.detailMac;
    $("aliasMac").textContent = state.editMac;
    $("aliasName").value = (d && (d.alias || d.display_name)) || "";
    $("aliasDlg").showModal();
  });

  $("aliasForm").addEventListener("submit", async (ev) => {
    const val = ev.submitter && ev.submitter.value;
    if (val !== "ok") return;
    ev.preventDefault();
    const name = $("aliasName").value || "";
    const fd = new FormData();
    fd.set("mac", state.editMac);
    fd.set("name", name);
    const r = await fetch("/api/alias", { method: "POST", body: fd });
    const data = await r.json();
    if (data.devices) state.devices = data.devices;
    $("aliasDlg").close();
    renderDevices();
    if (state.detailMac === state.editMac && $("detailDlg").open) {
      openDeviceDetail(state.detailMac);
    }
  });

  $("btnSettings").addEventListener("click", async () => {
    try {
      const r = await fetch("/api/settings");
      state.settings = await r.json();
    } catch (_) {}
    fillSettingsForm();
    $("settingsDlg").showModal();
  });

  if (!(state.settings && state.settings.wskey_set)) {
    setTimeout(() => {
      fillSettingsForm();
      try { $("settingsDlg").showModal(); } catch (_) {}
    }, 300);
  }

  $("settingsForm").addEventListener("submit", async (ev) => {
    const val = ev.submitter && ev.submitter.value;
    if (val !== "ok") return;
    ev.preventDefault();
    const fd = new FormData();
    fd.set("wskey", ($("fWskey") && $("fWskey").value) || "");
    fd.set("clear_wskey", $("fClearWskey") && $("fClearWskey").checked ? "1" : "0");
    fd.set("poll_interval", ($("fPollInterval") && $("fPollInterval").value) || "");
    fd.set("offline_misses", ($("fOfflineMisses") && $("fOfflineMisses").value) || "");
    fd.set("watch_macs", ($("fWatchMacs") && $("fWatchMacs").value) || "");
    fd.set("router_macs", ($("fRouterMacs") && $("fRouterMacs").value) || "");
    fd.set("http_proxy", ($("fHttpProxy") && $("fHttpProxy").value) || "");
    fd.set("https_proxy", ($("fHttpsProxy") && $("fHttpsProxy").value) || "");
    fd.set("poll_now", $("fPollNow") && $("fPollNow").checked ? "1" : "0");
    fd.set("ui_password", ($("fUiPassword") && $("fUiPassword").value) || "");
    fd.set("clear_ui_password", $("fClearUiPassword") && $("fClearUiPassword").checked ? "1" : "0");

    $("settingsSave").disabled = true;
    try {
      const r = await fetch("/api/settings", { method: "POST", body: fd });
      const data = await r.json();
      if (!data.ok) throw new Error(data.error || "保存失败");
      if (data.settings) state.settings = data.settings;
      if (data.stats) state.stats = data.stats;
      if (data.devices) state.devices = data.devices;
      if (data.events) state.events = data.events;
      renderAll();
      $("settingsDlg").close();
      if (data.poll_error) {
        errBox.style.display = "block";
        errBox.dataset.sticky = "1";
        errBox.textContent = "已保存，但轮询失败: " + data.poll_error;
      } else {
        errBox.dataset.sticky = "0";
        showOk(data.polled ? "已保存，并完成一次轮询" : "已保存");
      }
    } catch (e) {
      errBox.style.display = "block";
      errBox.dataset.sticky = "1";
      errBox.textContent = "保存失败: " + e;
    } finally {
      $("settingsSave").disabled = false;
    }
  });

  filterEl.addEventListener("input", renderDevices);

  $("btnPoll").addEventListener("click", async () => {
    $("btnPoll").disabled = true;
    try {
      const r = await fetch("/api/poll", { method: "POST" });
      const data = await r.json();
      if (data.ok) {
        state.devices = data.devices || state.devices;
        state.stats = data.stats || state.stats;
        if (data.events) {
          state.events = data.events;
        } else {
          const er = await fetch("/api/events?limit=100");
          const ed = await er.json();
          state.events = ed.events || state.events;
        }
        errBox.dataset.sticky = "0";
        renderAll();
        showOk("刷新完成");
      } else {
        errBox.style.display = "block";
        errBox.dataset.sticky = "1";
        errBox.textContent = "刷新失败: " + (data.error || r.status);
        if (data.stats) {
          state.stats = data.stats;
          renderStats();
        }
      }
    } catch (e) {
      errBox.style.display = "block";
      errBox.dataset.sticky = "1";
      errBox.textContent = "刷新失败: " + e;
    } finally {
      $("btnPoll").disabled = false;
    }
  });

  const btnLogout = $("btnLogout");
  if (btnLogout) {
    btnLogout.addEventListener("click", async () => {
      await fetch("/logout", { method: "POST" });
      location.href = "/login";
    });
  }

  function connectWs() {
    if (!wsState) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    wsState.textContent = "WS 连接中";
    wsState.className = "badge";
    ws.onopen = () => {
      wsState.textContent = "WS 实时";
      wsState.className = "badge ok";
    };
    ws.onclose = () => {
      wsState.textContent = "WS 断开 · 重连中";
      wsState.className = "badge bad";
      setTimeout(connectWs, 3000);
    };
    ws.onerror = () => ws.close();
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.settings) state.settings = msg.settings;
      if (msg.type === "ping") {
        if (msg.stats) state.stats = msg.stats;
        renderStats();
        return;
      }
      if (msg.type === "error") {
        if (msg.stats) state.stats = msg.stats;
        renderStats();
        return;
      }
      if (msg.type === "snapshot") {
        if (msg.devices) state.devices = msg.devices;
        if (msg.events) state.events = msg.events;
        if (msg.stats) state.stats = msg.stats;
        renderAll();
      }
    };
  }

  async function bootstrapIfEmpty() {
    if ((state.devices || []).length) return;
    try {
      const [dr, sr, er] = await Promise.all([
        fetch("/api/devices"),
        fetch("/api/stats"),
        fetch("/api/events?limit=100"),
      ]);
      const dd = await dr.json();
      state.devices = dd.devices || [];
      if (dd.stats) state.stats = dd.stats;
      else state.stats = await sr.json();
      const ed = await er.json();
      state.events = ed.events || [];
      renderAll();
    } catch (e) {
      console.error(e);
    }
  }

  renderAll();
  bootstrapIfEmpty();
  connectWs();
})();
