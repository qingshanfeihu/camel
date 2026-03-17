const state = {
  activeView: "knowledge",
  chatSessionId: null,
  currentRunId: null,
  ws: null,
};

async function api(path, options = {}) {
  const resp = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const contentType = resp.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await resp.json()
    : await resp.text();
  if (!resp.ok) {
    const detail = typeof payload === "string" ? payload : JSON.stringify(payload);
    throw new Error(`HTTP ${resp.status}: ${detail}`);
  }
  return payload;
}

function toast(msg, isError = false) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  el.style.borderLeftColor = isError ? "#dc2626" : "#0f766e";
  setTimeout(() => el.classList.add("hidden"), 3200);
}

function setupTabs() {
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const view = btn.dataset.view;
      state.activeView = view;
      document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
      document.getElementById(`view-${view}`).classList.add("active");
    });
  });
}

function fmtSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function escapeHtml(raw) {
  return String(raw || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function setQuickPill(id, ok, label) {
  const pill = document.getElementById(id);
  pill.textContent = `${label}: ${ok ? "OK" : "DOWN"}`;
  pill.classList.toggle("ok", !!ok);
  pill.classList.toggle("bad", !ok);
}

async function refreshQuickStatus() {
  try {
    const s = await api("/system/status");
    setQuickPill("pill-gateway", s.llm_gateway, "Gateway");
    setQuickPill("pill-rag", s.rag_ready, "RAG");
    setQuickPill("pill-device", s.nsae_device, "Device");
  } catch (e) {
    setQuickPill("pill-gateway", false, "Gateway");
    setQuickPill("pill-rag", false, "RAG");
    setQuickPill("pill-device", false, "Device");
  }
}

async function initKnowledgeView() {
  document.getElementById("btn-upload-doc").addEventListener("click", uploadDocument);
  document.getElementById("btn-refresh-docs").addEventListener("click", refreshDocuments);
  document.getElementById("btn-refresh-chunks").addEventListener("click", refreshChunks);
  document.querySelectorAll("[data-db-action]").forEach((btn) => {
    btn.addEventListener("click", () => runDbAction(btn.dataset.dbAction));
  });

  await refreshKBStats();
  await refreshDocuments();
  await refreshChunks();
}

async function refreshKBStats() {
  const stats = await api("/knowledge/stats");
  document.getElementById("kb-total-chunks").textContent = stats.total_chunks;
  document.getElementById("kb-module-count").textContent = Object.keys(stats.modules || {}).length;
  document.getElementById("kb-protocol-count").textContent = Object.keys(stats.protocols || {}).length;
  document.getElementById("kb-graphrag-ready").textContent = stats.graphrag_ready ? "Ready" : "Not Ready";
}

async function uploadDocument() {
  const input = document.getElementById("doc-file");
  const file = input.files && input.files[0];
  if (!file) {
    toast("请先选择 PDF 文件", true);
    return;
  }
  const fd = new FormData();
  fd.append("file", file);

  const resp = await fetch("/api/knowledge/upload", { method: "POST", body: fd });
  if (!resp.ok) {
    toast("上传失败", true);
    return;
  }
  toast("上传成功");
  input.value = "";
  await refreshDocuments();
}

async function refreshDocuments() {
  const docs = await api("/knowledge/documents");
  const tbody = document.querySelector("#docs-table tbody");
  tbody.innerHTML = "";
  docs.forEach((d) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(d.name)}</td>
      <td>${fmtSize(d.size)}</td>
      <td><button class="btn danger" data-del="${escapeHtml(d.name)}">删除</button></td>
    `;
    tbody.appendChild(tr);
  });

  tbody.querySelectorAll("button[data-del]").forEach((b) => {
    b.addEventListener("click", async () => {
      await api(`/knowledge/documents/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" });
      toast(`已删除 ${b.dataset.del}`);
      await refreshDocuments();
    });
  });
}

async function runDbAction(action) {
  const out = document.getElementById("db-action-log");
  out.textContent = `执行 ${action}...`;
  try {
    const resp = await api("/knowledge/db", {
      method: "POST",
      body: JSON.stringify({ action }),
    });

    // 轻量操作 (status/delete) 直接返回结果
    if (!resp.details || !resp.details.task_id) {
      out.textContent = resp.message || "完成";
      toast(`DB ${action} 完成`);
      await refreshKBStats();
      await refreshQuickStatus();
      return;
    }

    // 重量操作 — 后台任务已启动, 通过 WebSocket 接收实时日志
    const taskId = resp.details.task_id;
    out.textContent = `[后台任务 ${taskId}] 已启动, 等待日志...\n`;
    toast(`${action} 任务已启动: ${taskId}`);
    _openKbTaskWs(taskId, out);
  } catch (e) {
    out.textContent = e.message;
    toast(`DB ${action} 失败`, true);
  }
}

/** 知识库构建任务: WebSocket 实时日志流 */
function _openKbTaskWs(taskId, logEl) {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  let retries = 0;

  function connect() {
    const ws = new WebSocket(`${scheme}://${location.host}/api/knowledge/ws/${taskId}`);

    ws.onmessage = (evt) => {
      if (evt.data) {
        logEl.textContent += evt.data;
        logEl.scrollTop = logEl.scrollHeight;
      }
    };
    ws.onerror = () => {
      toast("知识库任务日志连接异常", true);
    };
    ws.onclose = (evt) => {
      if (evt.code !== 1000 && retries < 3) {
        retries++;
        setTimeout(connect, 2000 * retries);
      } else {
        // 任务结束 — 刷新状态
        refreshKBStats().catch(() => {});
        refreshQuickStatus().catch(() => {});
      }
    };
  }
  connect();
}

async function refreshChunks() {
  const module = document.getElementById("chunk-module").value.trim();
  const protocol = document.getElementById("chunk-protocol").value.trim();
  const search = document.getElementById("chunk-search").value.trim();
  const q = new URLSearchParams({ limit: "50", offset: "0" });
  if (module) q.set("module", module);
  if (protocol) q.set("protocol", protocol);
  if (search) q.set("search", search);

  const chunks = await api(`/knowledge/chunks?${q.toString()}`);
  const tbody = document.querySelector("#chunks-table tbody");
  tbody.innerHTML = "";

  chunks.forEach((c) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(c.id)}</td>
      <td>${escapeHtml(c.product_module || "-")}</td>
      <td>${escapeHtml(c.protocol_type || "-")}</td>
      <td>${escapeHtml(c.step_type || "-")}</td>
      <td>${escapeHtml(c.text || "")}</td>
    `;
    tbody.appendChild(tr);
  });
}

async function initChatView() {
  document.getElementById("btn-refresh-sessions").addEventListener("click", loadSessions);
  document.getElementById("btn-send-chat").addEventListener("click", sendChat);
  document.getElementById("btn-clear-chat").addEventListener("click", () => {
    document.getElementById("chat-window").innerHTML = "";
    document.getElementById("chat-config-output").textContent = "-";
    document.getElementById("chat-verify-output").textContent = "-";
  });
  await loadSessions();
}

function appendMsg(role, content) {
  const box = document.getElementById("chat-window");
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  el.innerHTML = escapeHtml(content);
  box.appendChild(el);
  box.scrollTop = box.scrollHeight;
  return el;
}

function createStreamingMsg() {
  const box = document.getElementById("chat-window");
  const wrapper = document.createElement("div");
  wrapper.className = "msg assistant";

  const thinkEl = document.createElement("div");
  thinkEl.className = "think-steps";
  thinkEl.style.cssText = "color:#6b7280;font-size:0.85em;margin-bottom:6px;";
  wrapper.appendChild(thinkEl);

  const contentEl = document.createElement("div");
  contentEl.className = "stream-content";
  wrapper.appendChild(contentEl);

  box.appendChild(wrapper);
  box.scrollTop = box.scrollHeight;
  return { wrapper, thinkEl, contentEl };
}

async function loadSessions() {
  const sessions = await api("/chat/sessions");
  const list = document.getElementById("session-list");
  list.innerHTML = "";
  sessions.forEach((s) => {
    const item = document.createElement("div");
    item.className = `list-item ${state.chatSessionId === s.id ? "active" : ""}`;
    item.innerHTML = `<strong>${escapeHtml(s.title || s.id)}</strong><div class="hint">${s.message_count} 条消息</div>`;
    item.addEventListener("click", async () => {
      state.chatSessionId = s.id;
      await loadSessionMessages(s.id);
      await loadSessions();
    });
    list.appendChild(item);
  });
}

async function loadSessionMessages(sessionId) {
  const messages = await api(`/chat/sessions/${encodeURIComponent(sessionId)}/messages`);
  const box = document.getElementById("chat-window");
  box.innerHTML = "";
  messages.forEach((m) => appendMsg(m.role === "user" ? "user" : "assistant", m.content));
}

async function sendChat() {
  const input = document.getElementById("chat-input");
  const mode = document.getElementById("chat-mode").value;
  const msg = input.value.trim();
  if (!msg) return;

  appendMsg("user", msg);
  input.value = "";

  const sendBtn = document.getElementById("btn-send-chat");
  sendBtn.disabled = true;

  try {
    if (mode === "explain") {
      await sendChatStream(msg);
    } else {
      await sendChatNormal(msg, mode);
    }
    await loadSessions();
  } catch (e) {
    appendMsg("assistant", `请求失败: ${e.message}`);
    toast("问答失败", true);
  } finally {
    sendBtn.disabled = false;
  }
}

async function sendChatNormal(msg, mode) {
  const resp = await api("/chat/ask", {
    method: "POST",
    body: JSON.stringify({ session_id: state.chatSessionId, message: msg, mode }),
  });
  state.chatSessionId = resp.session_id;
  appendMsg("assistant", resp.message.content || "");

  const cc = (resp.config_commands || []).join("\n");
  const vc = (resp.verify_commands || []).join("\n");
  document.getElementById("chat-config-output").textContent = cc || "-";
  document.getElementById("chat-verify-output").textContent = vc || "-";
}

async function sendChatStream(msg) {
  const { thinkEl, contentEl, wrapper } = createStreamingMsg();
  const box = document.getElementById("chat-window");
  let accumulated = "";

  const resp = await fetch("/api/chat/ask_stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: state.chatSessionId, message: msg, mode: "explain" }),
  });

  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop();

    let eventType = "";
    for (const line of lines) {
      if (line.startsWith("event: ")) {
        eventType = line.slice(7).trim();
      } else if (line.startsWith("data: ")) {
        const raw = line.slice(6);
        let data;
        try { data = JSON.parse(raw); } catch { data = raw; }

        if (eventType === "session" && data && data.session_id) {
          state.chatSessionId = data.session_id;
        } else if (eventType === "think") {
          const step = document.createElement("div");
          step.textContent = typeof data === "string" ? data : JSON.stringify(data);
          thinkEl.appendChild(step);
          box.scrollTop = box.scrollHeight;
        } else if (eventType === "delta") {
          const text = typeof data === "string" ? data : "";
          accumulated += text;
          contentEl.textContent = accumulated;
          box.scrollTop = box.scrollHeight;
        } else if (eventType === "done") {
          thinkEl.style.display = "none";
        } else if (eventType === "error") {
          contentEl.textContent = typeof data === "string" ? data : "处理失败";
          contentEl.style.color = "#dc2626";
        }
        eventType = "";
      }
    }
  }
}

async function initTestingView() {
  document.getElementById("btn-refresh-jobs").addEventListener("click", loadJobs);
  document.getElementById("btn-save-job").addEventListener("click", saveJob);
  document.getElementById("btn-delete-job").addEventListener("click", deleteJob);
  document.getElementById("btn-run-selected").addEventListener("click", runSelectedJobs);
  document.getElementById("btn-run-all").addEventListener("click", () => runJobs([]));
  document.getElementById("btn-cancel-run").addEventListener("click", cancelRun);
  await loadJobs();
}

async function loadJobs() {
  const jobs = await api("/testing/jobs");
  const list = document.getElementById("job-list");
  list.innerHTML = "";

  jobs.forEach((j) => {
    const row = document.createElement("div");
    row.className = "list-item";
    row.innerHTML = `
      <label><input type="checkbox" data-job-check="${escapeHtml(j.name)}"> ${escapeHtml(j.name)}</label>
      <div class="hint">${fmtSize(j.size || 0)}</div>
    `;
    row.addEventListener("click", async (e) => {
      if (e.target && e.target.matches("input")) return;
      const detail = await api(`/testing/jobs/${encodeURIComponent(j.name)}`);
      document.getElementById("job-name").value = detail.name;
      document.getElementById("job-content").value = detail.content;
    });
    list.appendChild(row);
  });
}

async function saveJob() {
  const nameInput = document.getElementById("job-name");
  const contentInput = document.getElementById("job-content");
  let name = nameInput.value.trim();
  const content = contentInput.value;
  if (!name) {
    toast("请输入任务文件名", true);
    return;
  }
  if (!name.endsWith(".txt")) name += ".txt";

  const payload = { name, content };
  try {
    await api("/testing/jobs", { method: "POST", body: JSON.stringify(payload) });
    toast(`已创建 ${name}`);
  } catch {
    await api(`/testing/jobs/${encodeURIComponent(name)}`, { method: "PUT", body: JSON.stringify(payload) });
    toast(`已更新 ${name}`);
  }
  await loadJobs();
}

async function deleteJob() {
  const name = document.getElementById("job-name").value.trim();
  if (!name) {
    toast("请先选择任务", true);
    return;
  }
  await api(`/testing/jobs/${encodeURIComponent(name)}`, { method: "DELETE" });
  toast(`已删除 ${name}`);
  document.getElementById("job-name").value = "";
  document.getElementById("job-content").value = "";
  await loadJobs();
}

function selectedJobs() {
  return Array.from(document.querySelectorAll("input[data-job-check]:checked")).map((x) => x.dataset.jobCheck);
}

async function runSelectedJobs() {
  const jobs = selectedJobs();
  if (!jobs.length) {
    toast("请先勾选任务", true);
    return;
  }
  await runJobs(jobs);
}

async function runJobs(jobFiles) {
  // 防止重复启动
  const btn = document.querySelector('[onclick*="runSelectedJobs"], [onclick*="runAllJobs"]');
  if (btn) btn.disabled = true;
  try {
    const resp = await api("/testing/run", {
      method: "POST",
      body: JSON.stringify({ job_files: jobFiles }),
    });
    state.currentRunId = resp.run_id;
    document.getElementById("current-run").textContent = `当前 Run: ${resp.run_id}`;
    document.getElementById("run-live-log").textContent = "启动成功，等待日志...\n";
    openRunWebSocket(resp.run_id);
    toast(`测试已启动: ${resp.run_id}`);
  } catch (e) {
    toast(e.message || "启动失败", true);
    if (btn) btn.disabled = false;
  }
}

function openRunWebSocket(runId) {
  if (state.ws) {
    state.ws.close();
    state.ws = null;
  }

  const scheme = location.protocol === "https:" ? "wss" : "ws";
  let retries = 0;
  const maxRetries = 5;

  function connect() {
    const ws = new WebSocket(`${scheme}://${location.host}/api/testing/ws/${runId}`);
    state.ws = ws;

    ws.onmessage = (evt) => {
      const log = document.getElementById("run-live-log");
      log.textContent += evt.data;
      log.scrollTop = log.scrollHeight;
    };

    ws.onerror = () => {
      toast("日志连接异常", true);
    };

    ws.onclose = (evt) => {
      state.ws = null;
      // 重新启用按钮
      const btn = document.querySelector('[onclick*="runSelectedJobs"], [onclick*="runAllJobs"]');
      if (btn) btn.disabled = false;
      // 非正常关闭且未超重试次数时自动重连
      if (evt.code !== 1000 && retries < maxRetries) {
        retries++;
        const delay = Math.min(1000 * Math.pow(2, retries), 10000);
        setTimeout(connect, delay);
      } else {
        loadRunsSummaryAndList().catch(() => null);
      }
    };
  }

  connect();
}

async function cancelRun() {
  if (!state.currentRunId) {
    toast("当前没有运行中的任务", true);
    return;
  }
  await api(`/testing/cancel/${encodeURIComponent(state.currentRunId)}`, { method: "POST" });
  toast(`已发送取消请求: ${state.currentRunId}`);
}

async function initReportsView() {
  document.getElementById("btn-refresh-runs").addEventListener("click", loadRunsSummaryAndList);
  await loadRunsSummaryAndList();
}

async function loadRunsSummaryAndList() {
  const summary = await api("/reports/summary");
  document.getElementById("sum-total").textContent = summary.total || 0;
  document.getElementById("sum-running").textContent = summary.by_status?.running || 0;
  document.getElementById("sum-success").textContent = summary.by_status?.success || 0;
  document.getElementById("sum-failed").textContent = summary.by_status?.failed || 0;
  document.getElementById("verdict-summary").textContent = JSON.stringify(summary.by_verdict || {}, null, 2);

  const runs = await api("/reports/runs?limit=100");
  const tbody = document.querySelector("#runs-table tbody");
  tbody.innerHTML = "";
  runs.forEach((r) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(r.id)}</td>
      <td>${escapeHtml(r.job_file)}</td>
      <td>${escapeHtml(r.status)}</td>
      <td>${escapeHtml(r.verdict || "-")}</td>
      <td>${escapeHtml(r.created_at || "")}</td>
    `;
    tr.addEventListener("click", async () => {
      const detail = await api(`/reports/runs/${encodeURIComponent(r.id)}`);
      document.getElementById("report-md").textContent = detail.report_md || "-";
      document.getElementById("report-json").textContent = JSON.stringify(detail.result_json || {}, null, 2);
    });
    tbody.appendChild(tr);
  });
}

async function initSystemView() {
  document.querySelectorAll("[data-probe]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const target = btn.dataset.probe;
      const data = await api(`/system/probe/${encodeURIComponent(target)}`);
      document.getElementById("probe-output").textContent = JSON.stringify(data, null, 2);
    });
  });

  document.getElementById("btn-save-env").addEventListener("click", saveEnv);
  document.getElementById("btn-refresh-logs").addEventListener("click", loadLogs);
  document.getElementById("btn-open-log").addEventListener("click", openSelectedLog);

  // Gateway control
  document.getElementById("btn-gateway-start").addEventListener("click", gatewayStart);
  document.getElementById("btn-gateway-stop").addEventListener("click", gatewayStop);
  document.getElementById("btn-gateway-refresh").addEventListener("click", gatewayRefreshStatus);
  document.getElementById("btn-gateway-set-mode").addEventListener("click", gatewaySetMode);
  document.getElementById("btn-gateway-logs").addEventListener("click", gatewayRefreshLogs);

  await refreshSystemStatus();
  await loadEnv();
  await loadLogs();
  await gatewayRefreshStatus();
}

async function refreshSystemStatus() {
  const s = await api("/system/status");
  const host = document.getElementById("system-status-list");
  host.innerHTML = "";

  const rows = [
    ["LLM Gateway", s.llm_gateway],
    ["NSAE Device", s.nsae_device],
    ["Test VM", s.test_vm],
    ["RAG Ready", s.rag_ready],
    ["GraphRAG Ready", s.graphrag_ready],
    ["KB Exists", s.kb_exists],
  ];

  rows.forEach(([name, ok]) => {
    const div = document.createElement("div");
    div.className = `status-card ${ok ? "ok" : "bad"}`;
    div.innerHTML = `<strong>${name}</strong><div class="hint">${ok ? "OK" : "DOWN"}</div>`;
    host.appendChild(div);
  });
}

async function loadEnv() {
  const env = await api("/system/env");
  document.getElementById("env-gateway-url").value = env.llm_gateway_url || "";
  document.getElementById("env-chat-model").value = env.llm_chat_model || "";
  document.getElementById("env-device-ip").value = env.lb_device_ip || "";
  document.getElementById("env-device-user").value = env.lb_username || "";
  document.getElementById("env-vm-ip").value = env.vm_mgmt_ip || "";
  document.getElementById("env-vm-user").value = env.vm_username || "";
}

async function saveEnv() {
  const payload = {
    llm_gateway_url: document.getElementById("env-gateway-url").value.trim(),
    llm_chat_model: document.getElementById("env-chat-model").value.trim(),
    lb_device_ip: document.getElementById("env-device-ip").value.trim(),
    lb_username: document.getElementById("env-device-user").value.trim(),
    vm_mgmt_ip: document.getElementById("env-vm-ip").value.trim(),
    vm_username: document.getElementById("env-vm-user").value.trim(),
  };
  await api("/system/env", { method: "PUT", body: JSON.stringify(payload) });
  toast("环境配置已更新");
  await refreshSystemStatus();
  await refreshQuickStatus();
}

async function loadLogs() {
  const logs = await api("/system/logs");
  const select = document.getElementById("log-select");
  select.innerHTML = "";
  logs.forEach((l) => {
    const opt = document.createElement("option");
    opt.value = l.name;
    opt.textContent = `${l.name} (${fmtSize(l.size)})`;
    select.appendChild(opt);
  });
}

async function openSelectedLog() {
  const name = document.getElementById("log-select").value;
  if (!name) {
    toast("无可用日志", true);
    return;
  }
  const data = await api(`/system/logs/${encodeURIComponent(name)}?tail=300`);
  document.getElementById("log-viewer").textContent = (data.lines || []).join("\n");
}

async function gatewayRefreshStatus() {
  try {
    const s = await api("/system/gateway/status");
    const hint = document.getElementById("gateway-pid-hint");
    hint.textContent = s.running_locally
      ? `运行中 PID=${s.pid}`
      : s.connected
      ? "外部运行"
      : "未运行";

    if (s.mode) {
      const sel = document.getElementById("gateway-mode-select");
      if (sel.value !== s.mode) sel.value = s.mode;
    }

    const out = {
      connected: s.connected,
      running_locally: s.running_locally,
      pid: s.pid,
      mode: s.mode,
      health: s.health,
      models: s.models,
      metrics: s.metrics,
    };
    if (s.error) out.error = s.error;
    document.getElementById("gateway-status-output").textContent = JSON.stringify(out, null, 2);

    // 同步顶栏状态
    setQuickPill("pill-gateway", s.connected, "Gateway");
  } catch (e) {
    document.getElementById("gateway-status-output").textContent = `请求失败: ${e.message}`;
  }
}

async function gatewayStart() {
  try {
    const r = await api("/system/gateway/start", { method: "POST" });
    toast(r.message);
    setTimeout(gatewayRefreshStatus, 2000);
  } catch (e) {
    toast(`启动失败: ${e.message}`, true);
  }
}

async function gatewayStop() {
  try {
    const r = await api("/system/gateway/stop", { method: "POST" });
    toast(r.message);
    await gatewayRefreshStatus();
    await refreshQuickStatus();
  } catch (e) {
    toast(`停止失败: ${e.message}`, true);
  }
}

async function gatewaySetMode() {
  const mode = document.getElementById("gateway-mode-select").value;
  try {
    const r = await api("/system/gateway/mode", {
      method: "POST",
      body: JSON.stringify({ mode }),
    });
    toast(`模式已切换为: ${r.mode}`);
  } catch (e) {
    toast(`切换失败: ${e.message}`, true);
  }
}

async function gatewayRefreshLogs() {
  try {
    const r = await api("/system/gateway/logs?tail=150");
    document.getElementById("gateway-log-viewer").textContent =
      (r.lines || []).join("\n") || "(暂无日志)";
  } catch (e) {
    document.getElementById("gateway-log-viewer").textContent = `请求失败: ${e.message}`;
  }
}

async function bootstrap() {
  setupTabs();

  await Promise.all([
    initKnowledgeView(),
    initChatView(),
    initTestingView(),
    initReportsView(),
    initSystemView(),
    refreshQuickStatus(),
  ]);

  setInterval(() => {
    refreshQuickStatus().catch(() => null);
  }, 12000);
}

bootstrap().catch((e) => {
  toast(`初始化失败: ${e.message}`, true);
});
