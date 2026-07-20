const $ = (sel) => document.querySelector(sel);
const state = {
  currentJobId: localStorage.getItem("paypal-web-current-job") || "",
  pollTimer: null,
  lastLogCount: 0,
  logsSignature: "",
  currentLogLines: [],
};

function fmtTime(ts) {
  if (!ts) return "-";
  return new Date(ts * 1000).toLocaleString();
}

function fmtDuration(seconds) {
  seconds = Math.max(0, Math.floor(seconds || 0));
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  if (m < 60) return `${m}m ${s}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m ${s}s`;
}

function pretty(obj) {
  if (!obj) return "{}";
  return JSON.stringify(obj, null, 2);
}

function esc(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.remove("hidden");
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.add("hidden"), 2600);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function setServer(ok) {
  const el = $("#serverStatus");
  el.textContent = ok ? "已连接" : "连接失败";
  el.classList.toggle("ok", ok);
  el.classList.toggle("bad", !ok);
}

async function health() {
  try {
    await api("/api/health");
    setServer(true);
  } catch (err) {
    setServer(false);
  }
}

async function refreshJobs() {
  try {
    const data = await api("/api/jobs");
    renderJobs(data.jobs || []);
  } catch (err) {
    toast(err.message);
  }
}

function renderJobs(jobs) {
  const box = $("#jobsList");
  if (!jobs.length) {
    box.className = "jobs-list empty";
    box.textContent = "暂无任务";
    return;
  }
  box.className = "jobs-list";
  box.innerHTML = jobs.map(job => `
    <div class="job-item ${job.id === state.currentJobId ? "active" : ""}" data-job-id="${esc(job.id)}">
      <div class="job-top">
        <span class="job-id">#${esc(job.id)}</span>
        <span class="badge ${esc(job.status)}">${esc(job.status)}</span>
      </div>
      <div class="job-sub">${esc(job.stage || "")}</div>
      <div class="job-sub">${esc(job.ba_token || "")} · ${esc(fmtTime(job.created_at))} · ${esc(job.proxy_enabled ? (job.proxy_label || "代理开") : "代理关")} · SMS:${esc(job.sms_provider || "manual")} · FP:${esc(job.fingerprint_source || "-")} · DD:${esc(job.datadome_mode || "-")} · MTR:${esc(job.mtr_runtime || "-")}${job.record_traffic ? " · 发包记录开" : ""}</div>
    </div>`).join("");
  box.querySelectorAll(".job-item").forEach(item => {
    item.addEventListener("click", () => selectJob(item.dataset.jobId));
  });
}

function syncProxyFields() {
  const enabled = $("#proxyEnabled").checked;
  const custom = $("#proxyMode").value === "custom";
  $("#proxyMode").disabled = !enabled;
  $("#proxyUrlWrap").classList.toggle("hidden", !(enabled && custom));
  $("#proxyUrl").disabled = !(enabled && custom);
  $("#proxyUrl").required = enabled && custom;
}

function syncTrafficFields() {
  const enabled = $("#recordTraffic").checked || Boolean($("#compareRoxyCapture")?.value.trim());
  $("#trafficDirWrap").classList.toggle("hidden", !enabled);
  $("#compareRoxyWrap").classList.toggle("hidden", !enabled);
  $("#trafficDir").disabled = !enabled;
  $("#compareRoxyCapture").disabled = !enabled;
}

function syncSmsFields() {
  const enabled = $("#smsbowerEnabled").checked;
  const phone = $("#phone");
  phone.required = !enabled;
  phone.placeholder = enabled ? "SMSBower 自动获取，可留空" : "+5591980133818";
}

function selectJob(jobId) {
  state.currentJobId = jobId || "";
  state.lastLogCount = 0;
  state.logsSignature = "";
  state.currentLogLines = [];
  if (jobId) localStorage.setItem("paypal-web-current-job", jobId);
  else localStorage.removeItem("paypal-web-current-job");
  refreshJobs();
  pollCurrent(true);
}

function formatLogLine(line) {
  const t = new Date((Number(line?.time) || 0) * 1000).toLocaleTimeString();
  const level = String(line?.level || "").padEnd(7);
  return `[${t}] ${level} ${line?.message ?? ""}`;
}

function renderLogs(logs) {
  const rows = (logs || []).map(formatLogLine);
  const box = $("#logsBox");
  const signature = `${rows.length}:${rows[rows.length - 1] || ""}`;
  if (signature === state.logsSignature) return;
  state.logsSignature = signature;
  state.currentLogLines = rows;
  box.innerHTML = (logs || []).map((line, index) => {
    const level = String(line?.level || "INFO").replace(/[^A-Z]/gi, "").toUpperCase() || "INFO";
    return `
      <div class="log-line log-${esc(level)}" data-log-index="${index}">
        <span class="log-message">${esc(rows[index] || "")}</span>
        <button class="log-copy" type="button" title="复制这一行">复制</button>
      </div>`;
  }).join("");
  $("#copyLogs").disabled = rows.length === 0;
  if ($("#autoScroll").checked) box.scrollTop = box.scrollHeight;
}

function renderCurrent(job) {
  $("#currentEmpty").classList.add("hidden");
  $("#currentBody").classList.remove("hidden");
  const trafficMeta = job.record_traffic
    ? ` · 发包记录：${job.traffic_dir || "准备中"}${job.traffic_report_json ? " · 已生成差异报告" : ""}`
    : "";
  const runtimeMeta = ` · SMS:${job.sms_provider || "manual"} · FP:${job.fingerprint_source || "-"} · DD:${job.datadome_mode || "-"} · MTR:${job.mtr_runtime || "-"}`;
  $("#currentMeta").textContent = `#${job.id} · 创建于 ${fmtTime(job.created_at)} · ${job.proxy_label || "代理关闭"}${runtimeMeta}${trafficMeta}`;
  $("#jobStatus").textContent = job.status;
  $("#jobStage").textContent = job.stage || "";
  $("#jobDuration").textContent = fmtDuration(job.duration);
  $("#generatedBox").textContent = pretty(job.generated);
  $("#resultBox").textContent = pretty(job.result || (job.error ? { error: job.error, traceback: job.traceback } : {}));
  if (job.record_traffic) {
    const current = job.result || (job.error ? { error: job.error, traceback: job.traceback } : {});
    $("#resultBox").textContent = pretty({
      ...current,
      traffic: {
        dir: job.traffic_dir,
        diff_report_json: job.traffic_report_json,
        diff_report_md: job.traffic_report_md,
      },
    });
  }
  $("#copyResult").disabled = !(job.result || job.error);

  const otpPanel = $("#otpPanel");
  otpPanel.classList.toggle("hidden", !job.awaiting_otp);
  $("#otpPrompt").textContent = job.awaiting_prompt || "请输入短信验证码或新手机号。";
  if (job.awaiting_otp) $("#otpValue").focus();

  renderLogs(job.logs || []);
}

async function pollCurrent(force = false) {
  if (!state.currentJobId) {
    $("#currentEmpty").classList.remove("hidden");
    $("#currentBody").classList.add("hidden");
    $("#currentMeta").textContent = "未选择任务";
    $("#copyResult").disabled = true;
    $("#copyLogs").disabled = true;
    $("#logsBox").innerHTML = "";
    state.logsSignature = "";
    state.currentLogLines = [];
    return;
  }
  try {
    const job = await api(`/api/jobs/${state.currentJobId}`);
    renderCurrent(job);
    if (force || ["completed", "failed", "awaiting_otp"].includes(job.status)) refreshJobs();
  } catch (err) {
    toast(err.message);
    state.currentJobId = "";
    localStorage.removeItem("paypal-web-current-job");
  }
}

async function startJob(evt) {
  evt.preventDefault();
  const btn = $("#startBtn");
  const proxyEnabled = $("#proxyEnabled").checked;
  const proxyMode = $("#proxyMode").value || "environment";
  const proxyUrl = proxyEnabled && proxyMode === "custom" ? $("#proxyUrl").value.trim() : "";
  const recordTraffic = $("#recordTraffic").checked || Boolean($("#compareRoxyCapture").value.trim());
  if (proxyEnabled && proxyMode === "custom" && !proxyUrl) {
    toast("请填写链式代理 URL");
    $("#proxyUrl").focus();
    return;
  }
  btn.disabled = true;
  btn.textContent = "启动中…";
  try {
    const data = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({
        ba_token: $("#baToken").value,
        phone: $("#phone").value,
        sms_provider: $("#smsbowerEnabled").checked ? "smsbower" : "manual",
        max_card_attempts: Number($("#maxCardAttempts").value || 5),
        max_flow_attempts: Number($("#maxFlowAttempts").value || 1),
        max_authorize_attempts: Number($("#maxAuthorizeAttempts").value || 3),
        card_retry_delay_seconds: Number($("#cardRetryDelay").value || 6),
        card_retry_jitter_seconds: Number($("#cardRetryJitter").value || 2),
        debug: $("#debug").checked,
        proxy_enabled: proxyEnabled,
        proxy_mode: proxyMode,
        proxy_url: proxyUrl,
        fingerprint_source: $("#fingerprintSource").value || "headless",
        datadome_mode: $("#datadomeMode").value || "headless",
        mtr_runtime: $("#mtrRuntime").value || "headless",
        record_traffic: recordTraffic,
        traffic_dir: recordTraffic ? $("#trafficDir").value.trim() : "",
        compare_roxy_capture: recordTraffic ? $("#compareRoxyCapture").value.trim() : "",
      }),
    });
    toast("任务已启动");
    selectJob(data.job.id);
  } catch (err) {
    toast(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "开始执行";
  }
}

async function submitOtp(evt) {
  evt.preventDefault();
  if (!state.currentJobId) return;
  const value = $("#otpValue").value.trim();
  if (!value) return toast("请输入验证码或手机号");
  try {
    await api(`/api/jobs/${state.currentJobId}/otp`, {
      method: "POST",
      body: JSON.stringify({ value }),
    });
    $("#otpValue").value = "";
    toast("已提交");
    pollCurrent(true);
  } catch (err) {
    toast(err.message);
  }
}

async function copyResult() {
  if (!state.currentJobId) return;
  try {
    const job = await api(`/api/jobs/${state.currentJobId}`);
    await navigator.clipboard.writeText(pretty(job.result || { error: job.error, traceback: job.traceback }));
    toast("结果已复制");
  } catch (err) {
    toast(err.message);
  }
}

async function copyLogs() {
  const text = state.currentLogLines.join("\n");
  if (!text) return toast("暂无日志可复制");
  try {
    await navigator.clipboard.writeText(text);
    toast("日志已复制");
  } catch (err) {
    toast(err.message);
  }
}

async function copyLogLine(evt) {
  const button = evt.target.closest(".log-copy");
  if (!button) return;
  const line = button.closest(".log-line");
  const message = line?.querySelector(".log-message")?.textContent || "";
  if (!message) return;
  try {
    await navigator.clipboard.writeText(message);
    toast("已复制该行日志");
  } catch (err) {
    toast(err.message);
  }
}

function bind() {
  $("#runForm").addEventListener("submit", startJob);
  $("#otpForm").addEventListener("submit", submitOtp);
  $("#refreshJobs").addEventListener("click", refreshJobs);
  $("#copyResult").addEventListener("click", copyResult);
  $("#copyLogs").addEventListener("click", copyLogs);
  $("#logsBox").addEventListener("click", copyLogLine);
  $("#clearCurrent").addEventListener("click", () => selectJob(""));
  $("#proxyEnabled").addEventListener("change", syncProxyFields);
  $("#proxyMode").addEventListener("change", syncProxyFields);
  $("#recordTraffic").addEventListener("change", syncTrafficFields);
  $("#compareRoxyCapture").addEventListener("input", syncTrafficFields);
  $("#smsbowerEnabled").addEventListener("change", syncSmsFields);
  syncProxyFields();
  syncTrafficFields();
  syncSmsFields();
}

bind();
health();
refreshJobs().then(() => pollCurrent(true));
setInterval(health, 8000);
setInterval(refreshJobs, 5000);
state.pollTimer = setInterval(() => pollCurrent(false), 1000);
