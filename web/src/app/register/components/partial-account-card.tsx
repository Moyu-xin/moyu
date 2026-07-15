"use client";

import { useEffect, useMemo, useRef, useState, type ChangeEvent } from "react";
import {
  Activity,
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  Clipboard,
  Clock3,
  Copy,
  Download,
  KeyRound,
  LoaderCircle,
  RefreshCw,
  Save,
  Send,
  TimerReset,
  Trash2,
  Upload,
  Zap,
  X,
} from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  acquireAccountOAuthAutomatically,
  checkPartialAccountHealth,
  checkPartialAccountQuota,
  clearPartialAccounts,
  completeAccountOAuth,
  deletePartialAccount,
  deletePartialAccounts,
  exportPartialAccountsToSub2ApiData,
  fetchPartialAccountHealthState,
  fetchPartialAccountPool,
  importPartialAccountToSub2Api,
  importPartialAccounts,
  importPartialAccountsToCpa,
  importPartialAccountsToSub2Api,
  prunePartialAccounts,
  refreshPartialAccountToken,
  startAccountOAuth,
  startPartialAccountHealthSchedule,
  startPartialAccountHealth,
  stopPartialAccountHealth,
  stopPartialAccountHealthSchedule,
  updatePartialAccount,
  updatePartialAccountHealthSettings,
  type AccountOAuthSession,
  type PartialAccountHealthState,
  type PartialAccountItem,
  type PartialAccountPool,
} from "@/lib/api";
import { AccountOAuthDialog } from "./account-oauth-dialog";

type SortKey = "email" | "status" | "health" | "plan" | "created_at";
type SortDirection = "asc" | "desc";
type ActionScope = "selected" | "filtered" | "all";
type UiLog = { time: string; text: string; level: "info" | "success" | "warning" | "error" };
type EditDraft = {
  email: string;
  password: string;
  access_token: string;
  refresh_token: string;
  status: string;
  stage: string;
  plan_type: string;
  account_type: string;
  mailbox_group: string;
  provider_label: string;
  register_mode: string;
  reason: string;
  note: string;
};

const EMPTY_EDIT: EditDraft = {
  email: "",
  password: "",
  access_token: "",
  refresh_token: "",
  status: "",
  stage: "",
  plan_type: "",
  account_type: "",
  mailbox_group: "",
  provider_label: "",
  register_mode: "",
  reason: "",
  note: "",
};

function formatTime(value?: string | number) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function formatDuration(value?: number) {
  const seconds = Math.max(0, Number(value || 0));
  if (!seconds) return "不足 1 分钟";
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days) return `${days} 天 ${hours} 小时`;
  if (hours) return `${hours} 小时 ${minutes} 分钟`;
  return `${minutes} 分钟`;
}

function selector(item?: PartialAccountItem) {
  return String(item?.id || item?.email || "");
}

function statusText(value?: string) {
  const status = String(value || "partial").toLowerCase();
  const labels: Record<string, string> = {
    add_phone: "待手机号",
    oauth_failed: "OAuth 失败",
    oauth_missing: "缺 OAuth 回调",
    token_exchange_failed: "Token 交换失败",
    profile_created: "资料已提交",
    email_verified: "邮箱已验证",
    password_set: "密码已设置",
    flow_blocked: "流程受阻",
    failed_after_partial: "半路失败",
    partial: "半成品",
  };
  return labels[status] || status;
}

function statusVariant(value?: string): "success" | "secondary" | "danger" | "outline" | "warning" | "info" {
  const status = String(value || "").toLowerCase();
  if (["profile_created", "email_verified", "password_set"].includes(status)) return "info";
  if (["add_phone", "oauth_failed", "oauth_missing", "token_exchange_failed", "flow_blocked"].includes(status)) return "warning";
  if (status === "failed_after_partial") return "secondary";
  return "outline";
}

function healthText(value?: string) {
  const status = String(value || "unknown").toLowerCase();
  if (["ok", "alive", "healthy"].includes(status)) return "存活";
  if (["banned", "dead", "disabled"].includes(status)) return "已封禁";
  if (["token_invalid", "token_expired", "auth_invalid", "invalid"].includes(status)) return "Token 失效";
  if (status === "refresh_token_missing") return "缺 RT";
  if (["refresh_token_invalid", "refresh_token_expired"].includes(status)) return "RT 失效";
  if (status === "refresh_token_refresh_failed") return "RT 刷新失败";
  if (["network_error", "proxy_error", "timeout", "failed"].includes(status)) return "检测失败";
  if (["cloudflare_blocked", "region_blocked", "rate_limited", "blocked"].includes(status)) return "请求受阻";
  return "未检测";
}

function healthVariant(value?: string): "success" | "secondary" | "danger" | "outline" | "warning" | "info" {
  const status = String(value || "unknown").toLowerCase();
  if (["ok", "alive", "healthy"].includes(status)) return "success";
  if (["banned", "dead", "disabled", "token_invalid", "token_expired", "auth_invalid", "invalid", "refresh_token_invalid", "refresh_token_expired"].includes(status)) return "danger";
  if (["network_error", "proxy_error", "timeout", "failed", "refresh_token_missing", "refresh_token_refresh_failed"].includes(status)) return "warning";
  if (["cloudflare_blocked", "region_blocked", "rate_limited", "blocked"].includes(status)) return "info";
  return "outline";
}

function healthBucket(value?: string) {
  const status = String(value || "unknown").toLowerCase();
  if (["ok", "alive", "healthy"].includes(status)) return "alive";
  if (["banned", "dead", "disabled", "token_invalid", "token_expired", "auth_invalid", "invalid", "refresh_token_expired", "refresh_token_invalid"].includes(status)) return "dead";
  return "unknown";
}

function typeText(item?: PartialAccountItem) {
  const value = String(item?.plan_type || item?.account_type || "unknown").toLowerCase();
  if (["plus", "chatgpt_plus", "paid", "premium"].includes(value)) return "Plus";
  if (value === "pro") return "Pro";
  if (["team", "business", "enterprise"].includes(value)) return "团队";
  if (value === "k12") return "K12";
  if (value === "free") return "Free";
  return value === "unknown" ? "未知" : value;
}

function quotaText(item?: PartialAccountItem) {
  if (!item) return "未知";
  if (String(item.quota_status || "").toLowerCase() === "failed") return "同步失败";
  if (item.quota_used !== undefined && item.quota_total !== undefined) return `${item.quota_used}/${item.quota_total}`;
  if (item.quota_remaining !== undefined) return `剩余 ${item.quota_remaining}`;
  return item.quota_status && item.quota_status !== "unknown" ? item.quota_status : "未知";
}

function quotaPercent(item?: PartialAccountItem) {
  const total = Number(item?.quota_total);
  const used = item?.quota_used !== undefined
    ? Number(item.quota_used)
    : item?.quota_remaining !== undefined
      ? total - Number(item.quota_remaining)
      : Number.NaN;
  if (!Number.isFinite(total) || total <= 0 || !Number.isFinite(used)) return null;
  return Math.max(0, Math.min(100, (used / total) * 100));
}

function oauthText(item?: PartialAccountItem) {
  const status = String(item?.oauth_token_status || "missing");
  if (status === "valid_refresh") return "OAuth 可用";
  if (status === "valid_access_only") return "OAuth AT 可用";
  if (status === "invalid_refresh") return "OAuth RT 失效";
  if (status === "incompatible_refresh") return "非 Sub2API RT";
  if (status === "web_session_only") return "仅网页会话";
  if (status === "mailbox_refresh_token") return "邮箱 RT";
  if (status === "unverified_refresh") return "RT 未验证";
  return "缺 OAuth";
}

function shortToken(value?: string, preview?: string) {
  const text = String(value || preview || "").trim();
  if (!text) return "-";
  if (text.length <= 24) return text;
  return `${text.slice(0, 12)}…${text.slice(-8)}`;
}

function tokenExpiryText(item?: PartialAccountItem) {
  const raw = item?.token_expires_at;
  if (!raw) return "未知";
  const numeric = Number(raw);
  const expiresAt = Number.isFinite(numeric) ? (numeric > 1e12 ? numeric : numeric * 1000) : Date.parse(String(raw));
  if (!Number.isFinite(expiresAt)) return "未知";
  const minutes = Math.floor((expiresAt - Date.now()) / 60000);
  if (minutes <= 0) return "已过期";
  if (minutes < 60) return `${minutes} 分钟`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)} 小时`;
  return `${Math.floor(minutes / 1440)} 天`;
}

function sortValue(item: PartialAccountItem, key: SortKey): string | number {
  if (key === "email") return item.email.toLowerCase();
  if (key === "status") return String(item.status || "").toLowerCase();
  if (key === "health") return String(item.health_status || "unknown").toLowerCase();
  if (key === "plan") return typeText(item).toLowerCase();
  return new Date(item.created_at || item.registered_at || 0).getTime() || 0;
}

function downloadFile(filename: string, content: string, type = "text/plain;charset=utf-8") {
  const blob = new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

function jsonFileName(item: PartialAccountItem) {
  return `${String(item.email || "partial-account").replace(/[^a-zA-Z0-9._-]+/g, "_")}.json`;
}

export function PartialAccountCard() {
  const [pool, setPool] = useState<PartialAccountPool | null>(null);
  const [healthState, setHealthState] = useState<PartialAccountHealthState | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [scheduleBusy, setScheduleBusy] = useState(false);
  const [scheduleInterval, setScheduleInterval] = useState(60);
  const [scheduleConcurrency, setScheduleConcurrency] = useState(8);
  const [scheduleRefreshQuota, setScheduleRefreshQuota] = useState(false);
  const [healthMethod, setHealthMethod] = useState("auto");
  const scheduleDirty = useRef(false);

  const [importText, setImportText] = useState("");
  const [autoCheckOnImport, setAutoCheckOnImport] = useState(false);
  const [dropBannedOnImport, setDropBannedOnImport] = useState(false);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [healthFilter, setHealthFilter] = useState("all");
  const [typeFilter, setTypeFilter] = useState("all");
  const [modeFilter, setModeFilter] = useState("all");
  const [providerFilter, setProviderFilter] = useState("all");
  const [groupFilter, setGroupFilter] = useState("all");
  const [readinessFilter, setReadinessFilter] = useState("all");
  const [sortKey, setSortKey] = useState<SortKey>("created_at");
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [actionScope, setActionScope] = useState<ActionScope>("selected");
  const [detailId, setDetailId] = useState("");
  const [editDraft, setEditDraft] = useState<EditDraft>(EMPTY_EDIT);
  const [oauthSession, setOauthSession] = useState<AccountOAuthSession | null>(null);
  const [logs, setLogs] = useState<UiLog[]>([]);

  const appendLog = (text: string, level: UiLog["level"] = "info") => {
    setLogs((current) => [{ time: new Date().toISOString(), text, level }, ...current].slice(0, 80));
  };

  const applyPool = (nextPool: PartialAccountPool) => {
    setPool(nextPool);
    const valid = new Set((nextPool.items || []).map(selector).filter(Boolean));
    setSelectedIds((current) => current.filter((id) => valid.has(id)));
  };

  const syncScheduleSettings = (state: PartialAccountHealthState) => {
    setScheduleInterval(state.settings.interval_minutes || 60);
    setScheduleConcurrency(state.settings.concurrency || 8);
    setScheduleRefreshQuota(Boolean(state.settings.refresh_quota));
    setHealthMethod(state.settings.method || "auto");
  };

  const load = async (silent = false, syncDraft = true) => {
    if (!silent) setLoading(true);
    try {
      const [poolData, healthData] = await Promise.all([
        fetchPartialAccountPool(),
        fetchPartialAccountHealthState(),
      ]);
      applyPool(poolData.partial_account_pool);
      setHealthState(healthData.partial_health);
      if (syncDraft || !scheduleDirty.current) syncScheduleSettings(healthData.partial_health);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载半成品库存失败");
    } finally {
      if (!silent) setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(true, false), 10000);
    return () => window.clearInterval(timer);
  }, []);

  const items = pool?.items || [];
  const selectedSet = useMemo(() => new Set(selectedIds), [selectedIds]);
  const detail = useMemo(() => items.find((item) => selector(item) === detailId), [items, detailId]);
  const statusOptions = useMemo(() => Array.from(new Set(items.map((item) => String(item.status || "partial")).filter(Boolean))).sort(), [items]);
  const modeOptions = useMemo(() => Array.from(new Set(items.map((item) => String(item.register_mode || "").trim()).filter(Boolean))).sort(), [items]);
  const providerOptions = useMemo(() => Array.from(new Set(items.map((item) => String(item.provider_label || item.provider || item.source || "").trim()).filter(Boolean))).sort(), [items]);
  const groupOptions = useMemo(() => Array.from(new Set(items.map((item) => String(item.mailbox_group || "").trim()).filter(Boolean))).sort(), [items]);
  const typeOptions = useMemo(() => Array.from(new Set(items.map((item) => typeText(item)))).filter((value) => value !== "未知").sort(), [items]);

  const filtered = useMemo(() => {
    const search = query.trim().toLowerCase();
    const result = items.filter((item) => {
      const status = String(item.status || "partial").toLowerCase();
      const mode = String(item.register_mode || "");
      const provider = String(item.provider_label || item.provider || item.source || "");
      const group = String(item.mailbox_group || "");
      const hasToken = Boolean(item.access_token || item.has_access_token);
      const readinessMatch = readinessFilter === "all"
        || (readinessFilter === "oauth_capable" && Boolean(item.oauth_capable))
        || (readinessFilter === "needs_oauth" && Boolean(item.needs_oauth))
        || (readinessFilter === "with_token" && hasToken)
        || (readinessFilter === "fixed_proxy" && Boolean(item.registration_proxy?.node_name || item.registration_proxy_node_id));
      const haystack = [
        item.email,
        item.inbox_address,
        item.status,
        item.stage,
        item.reason,
        item.note,
        item.health_status,
        item.health_error,
        item.provider_label,
        item.register_mode,
        item.mailbox_group,
        item.registration_proxy?.node_name,
        item.registration_proxy_label,
      ].join("\n").toLowerCase();
      return (statusFilter === "all" || status === statusFilter)
        && (healthFilter === "all" || healthBucket(item.health_status) === healthFilter)
        && (typeFilter === "all" || typeText(item) === typeFilter)
        && (modeFilter === "all" || mode === modeFilter)
        && (providerFilter === "all" || provider === providerFilter)
        && (groupFilter === "all" || group === groupFilter)
        && readinessMatch
        && (!search || haystack.includes(search));
    });
    result.sort((left, right) => {
      const a = sortValue(left, sortKey);
      const b = sortValue(right, sortKey);
      const compared = typeof a === "number" && typeof b === "number"
        ? a - b
        : String(a).localeCompare(String(b), "zh-CN");
      return sortDirection === "asc" ? compared : -compared;
    });
    return result;
  }, [items, query, statusFilter, healthFilter, typeFilter, modeFilter, providerFilter, groupFilter, readinessFilter, sortKey, sortDirection]);

  useEffect(() => {
    setPage(1);
  }, [query, statusFilter, healthFilter, typeFilter, modeFilter, providerFilter, groupFilter, readinessFilter, sortKey, sortDirection, pageSize]);

  useEffect(() => {
    if (!detailId) return;
    const selected = items.find((item) => selector(item) === detailId);
    if (!selected) {
      setDetailId("");
      return;
    }
    setEditDraft({
      email: selected.email || "",
      password: selected.password || "",
      access_token: selected.access_token || "",
      refresh_token: selected.refresh_token || "",
      status: selected.status || "",
      stage: selected.stage || "",
      plan_type: selected.plan_type || "",
      account_type: selected.account_type || "",
      mailbox_group: selected.mailbox_group || "",
      provider_label: selected.provider_label || "",
      register_mode: selected.register_mode || "",
      reason: selected.reason || "",
      note: selected.note || "",
    });
  }, [detailId, items]);

  const effectivePageSize = pageSize <= 0 ? Math.max(filtered.length, 1) : pageSize;
  const totalPages = Math.max(1, Math.ceil(filtered.length / effectivePageSize));
  const safePage = Math.min(page, totalPages);
  const pageItems = filtered.slice((safePage - 1) * effectivePageSize, safePage * effectivePageSize);

  const scopeItems = actionScope === "selected"
    ? items.filter((item) => selectedSet.has(selector(item)))
    : actionScope === "filtered"
      ? filtered
      : items;
  const scopeSelectors = Array.from(new Set(scopeItems.map(selector).filter(Boolean)));
  const scopeOAuthSelectors = Array.from(new Set(scopeItems.filter((item) => item.oauth_capable).map(selector).filter(Boolean)));
  const scopeTokenSelectors = Array.from(new Set(scopeItems.filter((item) => item.access_token || item.has_access_token).map(selector).filter(Boolean)));
  const allPageSelected = pageItems.length > 0 && pageItems.every((item) => selectedSet.has(selector(item)));

  const copy = async (text: string, message = "已复制") => {
    await navigator.clipboard.writeText(text);
    toast.success(message);
  };

  const toggleRowSelection = (id: string) => {
    setSelectedIds((current) => current.includes(id) ? current.filter((value) => value !== id) : [...current, id]);
  };

  const toggleCurrentPage = () => {
    const pageIds = pageItems.map(selector).filter(Boolean);
    setSelectedIds((current) => allPageSelected
      ? current.filter((id) => !pageIds.includes(id))
      : Array.from(new Set([...current, ...pageIds])));
  };

  const toggleSort = (key: SortKey) => {
    if (sortKey === key) setSortDirection((current) => current === "asc" ? "desc" : "asc");
    else {
      setSortKey(key);
      setSortDirection(key === "email" ? "asc" : "desc");
    }
  };

  const sortIcon = (key: SortKey) => sortKey !== key
    ? <ArrowUpDown className="size-3.5 text-slate-400" />
    : sortDirection === "asc"
      ? <ArrowUp className="size-3.5" />
      : <ArrowDown className="size-3.5" />;

  const exportJson = (targetItems = filtered, name = "半成品账号") => {
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
    downloadFile(`${name}-${stamp}.json`, JSON.stringify(targetItems, null, 2), "application/json;charset=utf-8");
  };

  const exportCredentials = (targetItems = filtered) => {
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
    const lines = targetItems.map((item) => `${item.email}----${item.password || ""}`).join("\n");
    downloadFile(`半成品账号密码-${stamp}.txt`, lines);
  };

  const doImportContent = async (text: string, label = "导入") => {
    if (!text.trim()) return;
    setBusy("import");
    try {
      const data = await importPartialAccounts(text, {
        auto_health_check: autoCheckOnImport,
        drop_banned: dropBannedOnImport,
      });
      applyPool(data.partial_account_pool);
      setImportText("");
      const summary = `${label}完成：新增 ${data.imported}，更新 ${data.updated}，跳过 ${data.skipped}`;
      appendLog(summary, data.skipped ? "warning" : "success");
      toast.success(summary);
    } catch (error) {
      const message = error instanceof Error ? error.message : "导入失败";
      appendLog(message, "error");
      toast.error(message);
    } finally {
      setBusy("");
    }
  };

  const doImportFiles = async (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files || []);
    event.target.value = "";
    if (!files.length) return;
    try {
      const contents = await Promise.all(files.map((file) => file.text()));
      await doImportContent(contents.join("\n"), `${files.length} 个文件导入`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "读取文件失败");
    }
  };

  const checkHealth = async (targets: string[], refreshQuota: boolean, key: string, mode = "") => {
    if (!targets.length) {
      toast.warning("当前操作范围没有账号");
      return;
    }
    setBusy(key);
    try {
      const data = await checkPartialAccountHealth(targets, refreshQuota, Math.min(12, Math.max(1, targets.length)), mode, healthMethod);
      applyPool(data.partial_health.partial_account_pool);
      const stats = data.partial_health.stats;
      const label = mode === "refresh_type" ? "类型刷新" : refreshQuota ? "测活和额度同步" : "测活";
      const summary = `${label}完成：存活 ${stats.alive}，失效 ${stats.dead}，失败 ${stats.failed}`;
      appendLog(summary, stats.failed ? "warning" : "success");
      toast.success(summary);
    } catch (error) {
      const message = error instanceof Error ? error.message : "半成品测活失败";
      appendLog(message, "error");
      toast.error(message);
    } finally {
      setBusy("");
    }
  };

  const syncQuota = async (targets: string[]) => {
    if (!targets.length) return toast.warning("当前操作范围没有账号");
    setBusy("quota-only");
    try {
      const data = await checkPartialAccountQuota(targets, Math.min(12, Math.max(1, targets.length)));
      applyPool(data.partial_health.partial_account_pool);
      const stats = data.partial_health.stats;
      const quotaSuccess = Number(stats.quota_ok || 0);
      const quotaFailed = Number(stats.quota_failed ?? Math.max(0, stats.total - quotaSuccess));
      const summary = `额度同步完成：成功 ${quotaSuccess}，失败 ${quotaFailed}`;
      appendLog(summary, quotaFailed ? "warning" : "success");
      toast[quotaFailed ? "warning" : "success"](summary);
    } catch (error) {
      const message = error instanceof Error ? error.message : "额度同步失败";
      appendLog(message, "error");
      toast.error(message);
    } finally {
      setBusy("");
    }
  };

  const startBackgroundHealth = async () => {
    if (!scopeSelectors.length) return toast.warning("当前操作范围没有账号");
    setBusy("background-start");
    try {
      const data = await startPartialAccountHealth({
        selectors: scopeSelectors,
        method: healthMethod,
        refresh_quota: true,
        concurrency: Math.min(12, Math.max(1, scopeSelectors.length)),
      });
      setHealthState(data.partial_health);
      if (data.partial_health.busy) toast.warning(data.partial_health.message || "已有半成品测活任务正在运行");
      else toast.success("后台测活已启动");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "启动后台测活失败");
    } finally {
      setBusy("");
    }
  };

  const stopBackgroundHealth = async () => {
    const data = await stopPartialAccountHealth();
    setHealthState(data.partial_health);
    toast.success("已请求停止后台测活");
  };

  const remove = async (item: PartialAccountItem) => {
    if (!window.confirm(`确定删除 ${item.email} 吗？`)) return;
    setBusy(`delete:${selector(item)}`);
    try {
      const data = await deletePartialAccount(selector(item));
      applyPool(data.partial_account_pool);
      setSelectedIds((current) => current.filter((id) => id !== selector(item)));
      setDetailId("");
      appendLog(`已删除 ${item.email}`, "success");
      toast.success("已删除半成品账号");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "删除失败");
    } finally {
      setBusy("");
    }
  };

  const removeScope = async () => {
    if (!scopeSelectors.length) return toast.warning("当前操作范围没有账号");
    if (!window.confirm(`确定删除当前范围内的 ${scopeSelectors.length} 个半成品账号吗？`)) return;
    setBusy("delete-scope");
    try {
      const data = await deletePartialAccounts(scopeSelectors);
      applyPool(data.partial_account_pool);
      setSelectedIds((current) => current.filter((id) => !scopeSelectors.includes(id)));
      appendLog(`批量删除 ${data.partial_account_pool.removed || 0} 个账号`, "success");
      toast.success(`已删除 ${data.partial_account_pool.removed || 0} 个账号`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "批量删除失败");
    } finally {
      setBusy("");
    }
  };

  const prune = async () => {
    if (!window.confirm("确定清理所有已确认封禁或 Token 失效的半成品账号吗？")) return;
    setBusy("prune");
    try {
      const data = await prunePartialAccounts();
      applyPool(data.partial_account_pool);
      appendLog(`清理失效账号 ${data.partial_account_pool.removed || 0} 个`, "success");
      toast.success(`已清理 ${data.partial_account_pool.removed || 0} 个失效账号`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "清理失败");
    } finally {
      setBusy("");
    }
  };

  const clear = async () => {
    if (!window.confirm("确定清空半成品库存吗？")) return;
    setBusy("clear");
    try {
      const data = await clearPartialAccounts();
      applyPool(data.partial_account_pool);
      setSelectedIds([]);
      setDetailId("");
      appendLog("半成品库存已清空", "success");
      toast.success("半成品库存已清空");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "清空失败");
    } finally {
      setBusy("");
    }
  };

  const doExportSub2Api = async (targets: string[]) => {
    if (!targets.length) return toast.warning("当前操作范围没有可导出的 Token 账号");
    setBusy("export-sub2api");
    try {
      const data = await exportPartialAccountsToSub2ApiData(targets);
      const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
      downloadFile(`partial-sub2api-${stamp}.json`, JSON.stringify(data.sub2api.data, null, 2), "application/json;charset=utf-8");
      appendLog(`已导出 ${data.sub2api.count} 个 Sub2API 账号`, "success");
      toast.success(`已导出 ${data.sub2api.count} 个账号`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "导出 Sub2API JSON 失败");
    } finally {
      setBusy("");
    }
  };

  const doImportSub2Api = async (targets: string[]) => {
    if (!targets.length) return toast.warning("当前操作范围没有 OAuth 可用账号");
    setBusy("import-sub2api");
    try {
      const data = await importPartialAccountsToSub2Api(targets);
      applyPool(data.promotion.partial_account_pool);
      const summary = `Sub2API 导入：成功 ${data.promotion.success}，失败 ${data.promotion.fail}`;
      appendLog(summary, data.promotion.fail ? "warning" : "success");
      toast[data.promotion.fail ? "warning" : "success"](summary);
    } catch (error) {
      const message = error instanceof Error ? error.message : "导入 Sub2API 失败";
      appendLog(message, "error");
      toast.error(message);
    } finally {
      setBusy("");
    }
  };

  const doImportCpa = async (targets: string[]) => {
    if (!targets.length) return toast.warning("当前操作范围没有 Access Token 账号");
    setBusy("import-cpa");
    try {
      const data = await importPartialAccountsToCpa(targets);
      applyPool(data.partial_account_pool);
      const summary = `CPA 导入：成功 ${data.cpa.success}，失败 ${data.cpa.fail}`;
      appendLog(summary, data.cpa.fail ? "warning" : "success");
      toast[data.cpa.fail ? "warning" : "success"](summary);
    } catch (error) {
      const message = error instanceof Error ? error.message : "导入 CPA 失败";
      appendLog(message, "error");
      toast.error(message);
    } finally {
      setBusy("");
    }
  };

  const refreshToken = async (item: PartialAccountItem) => {
    setBusy(`refresh-token:${selector(item)}`);
    try {
      const data = await refreshPartialAccountToken(selector(item));
      applyPool(data.partial_refresh.partial_account_pool);
      appendLog(`${item.email} Token 已刷新`, "success");
      toast.success("Token 已刷新并同步额度");
    } catch (error) {
      const message = error instanceof Error ? error.message : "刷新 Token 失败";
      appendLog(`${item.email}: ${message}`, "error");
      toast.error(message);
    } finally {
      setBusy("");
    }
  };

  const startOAuth = async (item: PartialAccountItem) => {
    const target = selector(item);
    if (item.registration_proxy?.node_name || item.registration_proxy_node_id) {
      if (!window.confirm(`将使用 ${item.registration_proxy?.node_name || item.registration_proxy_label || "注册节点"} 立即执行自动 OAuth，并尝试导入 Sub2API。是否继续？`)) return;
      setBusy(`oauth-start:${target}`);
      try {
        const data = await acquireAccountOAuthAutomatically("partial", target);
        applyPool(data.oauth.partial_account_pool);
        if (data.oauth.warehouse.ok) toast.success("OAuth 已通过注册节点获取，账号已晋升并通过 Sub2API 测活");
        else toast.warning("OAuth 已获取，但 Sub2API 导入或测活未通过");
        appendLog(`${item.email} 自动 OAuth 完成`, data.oauth.warehouse.ok ? "success" : "warning");
      } catch (error) {
        const message = error instanceof Error ? error.message : "同节点 OAuth 获取失败";
        appendLog(`${item.email}: ${message}`, "error");
        toast.error(message);
      } finally {
        setBusy("");
      }
      return;
    }
    const popup = window.open("about:blank", "_blank");
    setBusy(`oauth-start:${target}`);
    try {
      const data = await startAccountOAuth("partial", target);
      setOauthSession(data.oauth);
      if (popup) {
        popup.opener = null;
        popup.location.href = data.oauth.auth_url;
      } else {
        toast.warning("浏览器拦截了授权窗口，请在授权弹窗中打开授权页");
      }
    } catch (error) {
      popup?.close();
      toast.error(error instanceof Error ? error.message : "生成 OAuth 授权链接失败");
    } finally {
      setBusy("");
    }
  };

  const completeOAuth = async (callback: string) => {
    if (!oauthSession) return;
    setBusy("oauth-complete");
    try {
      const data = await completeAccountOAuth(oauthSession.job_id, callback);
      applyPool(data.oauth.partial_account_pool);
      setOauthSession(null);
      if (data.oauth.warehouse.ok) toast.success("OAuth 已获取，账号已晋升并通过 Sub2API 测活");
      else toast.warning("OAuth 已获取并晋升为成品账号，但 Sub2API 导入或测活未通过");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "OAuth 换码失败");
    } finally {
      setBusy("");
    }
  };

  const importWarehouse = async (item: PartialAccountItem) => {
    const target = selector(item);
    setBusy(`warehouse:${target}`);
    try {
      const data = await importPartialAccountToSub2Api(target);
      applyPool(data.promotion.partial_account_pool);
      if (data.promotion.warehouse.ok) toast.success("已导入 Sub2API 并晋升为成品账号");
      else toast.warning("已晋升为成品账号，但 Sub2API 测活未通过");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "导入 Sub2API 失败");
    } finally {
      setBusy("");
    }
  };

  const saveDetail = async () => {
    if (!detail) return;
    if (!editDraft.email.trim() || !editDraft.email.includes("@")) return toast.error("邮箱格式不正确");
    setBusy("save-detail");
    try {
      const data = await updatePartialAccount(selector(detail), editDraft);
      applyPool(data.partial_account_pool);
      setDetailId(selector(data.partial_account));
      appendLog(`${data.partial_account.email} 详情已更新`, "success");
      toast.success("账号详情已保存");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存失败");
    } finally {
      setBusy("");
    }
  };

  const schedulePayload = () => ({
    interval_minutes: Math.max(1, scheduleInterval || 60),
    concurrency: Math.max(1, Math.min(32, scheduleConcurrency || 8)),
    refresh_quota: scheduleRefreshQuota,
    method: healthMethod,
  });

  const saveSchedule = async () => {
    setScheduleBusy(true);
    try {
      const data = await updatePartialAccountHealthSettings(schedulePayload());
      setHealthState(data.partial_health);
      syncScheduleSettings(data.partial_health);
      scheduleDirty.current = false;
      toast.success("定时测活配置已保存");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存定时测活配置失败");
    } finally {
      setScheduleBusy(false);
    }
  };

  const toggleSchedule = async (enabled: boolean) => {
    setScheduleBusy(true);
    try {
      const data = enabled
        ? await startPartialAccountHealthSchedule(schedulePayload())
        : await stopPartialAccountHealthSchedule();
      setHealthState(data.partial_health);
      syncScheduleSettings(data.partial_health);
      scheduleDirty.current = false;
      toast.success(enabled ? "定时测活已开启" : "定时测活已停止");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "切换定时测活失败");
    } finally {
      setScheduleBusy(false);
    }
  };

  const stats = pool?.stats || {};
  const scheduledStats = healthState?.last_scheduled_stats || {};
  const saving = Boolean(busy);

  return (
    <>
      <div className="space-y-4">
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
          {[
            ["总数", stats.total || 0],
            ["当前存活", stats.health_alive || 0],
            ["已经失效", stats.health_dead || 0],
            ["尚未检测", stats.health_unknown || 0],
            ["OAuth 可导入", stats.oauth_capable || 0],
          ].map(([label, value]) => (
            <div key={String(label)} className="border border-slate-200 bg-white p-4 shadow-sm">
              <div className="text-xs text-slate-500">{label}</div>
              <div className="mt-1 text-2xl font-semibold text-slate-950">{value}</div>
            </div>
          ))}
        </div>

        <div className="border border-slate-200 bg-white p-4 shadow-sm">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-3">
              <TimerReset className="size-5 text-slate-600" />
              <div>
                <div className="font-semibold text-slate-950">半成品定时测活</div>
                <div className="text-xs text-slate-500">
                  {healthState?.schedule_running ? "调度器运行中" : "调度器未运行"}
                  {healthState?.last_scheduled_run_at ? ` · 上次 ${formatTime(healthState.last_scheduled_run_at)}` : " · 尚未执行"}
                </div>
              </div>
            </div>
            <label className="flex items-center gap-2 text-sm font-medium text-slate-700">
              <Checkbox checked={Boolean(healthState?.settings.scheduled_enabled)} onCheckedChange={(checked) => void toggleSchedule(Boolean(checked))} disabled={scheduleBusy} />
              启用定时测活
            </label>
          </div>
          <div className="mt-4 grid gap-3 md:grid-cols-[160px_140px_minmax(180px,1fr)_auto] md:items-end">
            <label className="space-y-1.5 text-xs font-medium text-slate-600">
              检测间隔（分钟）
              <Input type="number" min={1} value={String(scheduleInterval)} onChange={(event) => { scheduleDirty.current = true; setScheduleInterval(Math.max(1, Number(event.target.value) || 1)); }} />
            </label>
            <label className="space-y-1.5 text-xs font-medium text-slate-600">
              并发数
              <Input type="number" min={1} max={32} value={String(scheduleConcurrency)} onChange={(event) => { scheduleDirty.current = true; setScheduleConcurrency(Math.max(1, Math.min(32, Number(event.target.value) || 1))); }} />
            </label>
            <label className="flex h-10 items-center gap-2 text-sm text-slate-700">
              <Checkbox checked={scheduleRefreshQuota} onCheckedChange={(checked) => { scheduleDirty.current = true; setScheduleRefreshQuota(Boolean(checked)); }} />
              测活时同步额度
            </label>
            <Button variant="outline" onClick={() => void saveSchedule()} disabled={scheduleBusy}>
              {scheduleBusy ? <LoaderCircle className="size-4 animate-spin" /> : <Save className="size-4" />}保存配置
            </Button>
          </div>
          <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-xs text-slate-500">
            <span>下次执行：{formatTime(healthState?.next_scheduled_run_at)}</span>
            <span>上次结果：存活 {scheduledStats.alive || 0} / 失效 {scheduledStats.dead || 0} / 失败 {scheduledStats.failed || 0}</span>
            {healthState?.last_error ? <span className="text-red-600">调度错误：{healthState.last_error}</span> : null}
          </div>
        </div>

        <Card className="border-slate-200">
          <CardHeader className="gap-3">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <CardTitle>半成品库存</CardTitle>
                <CardDescription>网页注册已完成、尚待 OAuth 或仓库入库的账号。</CardDescription>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button variant="outline" onClick={() => void load()} disabled={saving}><RefreshCw className={`size-4 ${loading ? "animate-spin" : ""}`} />刷新</Button>
                <Button variant="outline" onClick={() => exportJson()} disabled={!filtered.length}><Download className="size-4" />导出筛选 JSON</Button>
                <Button variant="outline" onClick={() => exportCredentials()} disabled={!filtered.length}><Clipboard className="size-4" />导出账号密码</Button>
                <Button variant="outline" className="border-amber-200 text-amber-700" onClick={() => void prune()} disabled={saving || !stats.health_dead}><Trash2 className="size-4" />清理失效</Button>
                <Button variant="outline" className="border-rose-200 text-rose-700" onClick={() => void clear()} disabled={saving || !items.length}><Trash2 className="size-4" />清空</Button>
              </div>
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid gap-3 border-y border-slate-200 bg-slate-50/70 py-4 lg:grid-cols-[minmax(260px,1fr)_auto]">
              <Textarea value={importText} onChange={(event) => setImportText(event.target.value)} placeholder="email----password----refresh_token----access_token，或粘贴 JSON" className="min-h-24 bg-white font-mono text-xs" />
              <div className="flex min-w-52 flex-col gap-2">
                <label className="flex items-center gap-2 text-sm text-slate-700"><Checkbox checked={autoCheckOnImport} onCheckedChange={(checked) => setAutoCheckOnImport(Boolean(checked))} />导入后测活和额度</label>
                <label className="flex items-center gap-2 text-sm text-slate-700"><Checkbox checked={dropBannedOnImport} onCheckedChange={(checked) => setDropBannedOnImport(Boolean(checked))} disabled={!autoCheckOnImport} />移除导入后确认失效</label>
                <div className="mt-auto flex gap-2">
                  <Button onClick={() => void doImportContent(importText)} disabled={saving || !importText.trim()}>{busy === "import" ? <LoaderCircle className="size-4 animate-spin" /> : <Upload className="size-4" />}导入文本</Button>
                  <label className="inline-flex h-9 cursor-pointer items-center gap-2 border border-slate-200 bg-white px-3 text-sm font-medium text-slate-700 hover:bg-slate-50">
                    <Upload className="size-4" />导入文件
                    <input type="file" accept=".json,.txt,.csv" multiple className="hidden" onChange={(event) => void doImportFiles(event)} disabled={saving} />
                  </label>
                </div>
              </div>
            </div>

            <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-4">
              <Input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索邮箱、分组、错误、节点" className="xl:col-span-2" />
              <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)} className="h-9 border border-slate-200 bg-white px-3 text-sm"><option value="all">全部状态</option>{statusOptions.map((value) => <option key={value} value={value}>{statusText(value)}</option>)}</select>
              <select value={healthFilter} onChange={(event) => setHealthFilter(event.target.value)} className="h-9 border border-slate-200 bg-white px-3 text-sm"><option value="all">全部测活</option><option value="alive">存活</option><option value="dead">失效</option><option value="unknown">未确认</option></select>
              <select value={typeFilter} onChange={(event) => setTypeFilter(event.target.value)} className="h-9 border border-slate-200 bg-white px-3 text-sm"><option value="all">全部类型</option>{typeOptions.map((value) => <option key={value} value={value}>{value}</option>)}</select>
              <select value={modeFilter} onChange={(event) => setModeFilter(event.target.value)} className="h-9 border border-slate-200 bg-white px-3 text-sm"><option value="all">全部注册模式</option>{modeOptions.map((value) => <option key={value} value={value}>{value}</option>)}</select>
              <select value={providerFilter} onChange={(event) => setProviderFilter(event.target.value)} className="h-9 border border-slate-200 bg-white px-3 text-sm"><option value="all">全部邮箱来源</option>{providerOptions.map((value) => <option key={value} value={value}>{value}</option>)}</select>
              <select value={groupFilter} onChange={(event) => setGroupFilter(event.target.value)} className="h-9 border border-slate-200 bg-white px-3 text-sm"><option value="all">全部邮箱分组</option>{groupOptions.map((value) => <option key={value} value={value}>{value}</option>)}</select>
              <select value={readinessFilter} onChange={(event) => setReadinessFilter(event.target.value)} className="h-9 border border-slate-200 bg-white px-3 text-sm"><option value="all">全部就绪状态</option><option value="oauth_capable">OAuth 可导入</option><option value="needs_oauth">待 OAuth</option><option value="with_token">有 Access Token</option><option value="fixed_proxy">已记录固定节点</option></select>
            </div>

            <div className="flex flex-wrap items-center gap-2 border-y border-slate-200 py-3">
              <Button variant="outline" className="h-8" onClick={toggleCurrentPage}>{allPageSelected ? "取消当前页" : "选择当前页"}</Button>
              <Button variant="outline" className="h-8" onClick={() => setSelectedIds(Array.from(new Set([...selectedIds, ...filtered.map(selector)])))} disabled={!filtered.length}>选择筛选结果</Button>
              <Button variant="outline" className="h-8" onClick={() => setSelectedIds([])} disabled={!selectedIds.length}><X className="size-3.5" />清除选择</Button>
              <Badge variant="secondary">已选 {selectedIds.length}</Badge>
              <span className="ml-auto text-xs font-medium text-slate-500">操作范围</span>
              <select value={actionScope} onChange={(event) => setActionScope(event.target.value as ActionScope)} className="h-8 border border-slate-200 bg-white px-2 text-xs"><option value="selected">已选账号</option><option value="filtered">筛选结果</option><option value="all">全部账号</option></select>
              <select value={healthMethod} onChange={(event) => { scheduleDirty.current = true; setHealthMethod(event.target.value); }} className="h-8 border border-slate-200 bg-white px-2 text-xs"><option value="auto">自动测活</option><option value="sub2api">Sub2API</option><option value="local">本地 Token</option></select>
              <Badge variant="outline">{scopeSelectors.length} 个</Badge>
            </div>

            <div className="flex flex-wrap gap-2">
              <Button variant="outline" onClick={() => void checkHealth(scopeSelectors, false, "health-scope")} disabled={saving || !scopeSelectors.length}><Activity className="size-4" />测活</Button>
              <Button variant="outline" className="border-emerald-200 text-emerald-700" onClick={() => void checkHealth(scopeSelectors, true, "quota-scope")} disabled={saving || !scopeSelectors.length}><Activity className="size-4" />测活和额度</Button>
              <Button variant="outline" className="border-emerald-200 text-emerald-700" onClick={() => void syncQuota(scopeSelectors)} disabled={saving || !scopeSelectors.length}>{busy === "quota-only" ? <LoaderCircle className="size-4 animate-spin" /> : null}同步额度</Button>
              <Button variant="outline" onClick={() => void startBackgroundHealth()} disabled={saving || Boolean(healthState?.task?.enabled) || !scopeSelectors.length}>{busy === "background-start" ? <LoaderCircle className="size-4 animate-spin" /> : <Zap className="size-4" />}后台测活</Button>
              <Button variant="outline" onClick={() => void stopBackgroundHealth()} disabled={!healthState?.task?.enabled}>停止</Button>
              <Button variant="outline" className="border-indigo-200 text-indigo-700" onClick={() => void checkHealth(scopeSelectors, false, "type-scope", "refresh_type")} disabled={saving || !scopeSelectors.length}><RefreshCw className="size-4" />刷新类型</Button>
              <Button variant="outline" className="border-sky-200 text-sky-700" onClick={() => void doImportSub2Api(scopeOAuthSelectors)} disabled={saving || !scopeOAuthSelectors.length}><Send className="size-4" />导入 Sub2API ({scopeOAuthSelectors.length})</Button>
              <Button variant="outline" className="border-amber-200 text-amber-700" onClick={() => void doImportCpa(scopeTokenSelectors)} disabled={saving || !scopeTokenSelectors.length}><Send className="size-4" />导入 CPA ({scopeTokenSelectors.length})</Button>
              <Button variant="outline" onClick={() => void doExportSub2Api(scopeTokenSelectors)} disabled={saving || !scopeTokenSelectors.length}><Download className="size-4" />导出 Sub2API JSON</Button>
              <Button variant="outline" className="border-rose-200 text-rose-700" onClick={() => void removeScope()} disabled={saving || !scopeSelectors.length}><Trash2 className="size-4" />删除范围</Button>
            </div>

            <div className="overflow-hidden border border-slate-200">
              <div className="overflow-x-auto">
                <table className="w-full min-w-[1320px] border-collapse text-left">
                  <thead className="bg-slate-50 text-xs font-semibold text-slate-600">
                    <tr>
                      <th className="w-12 border-r border-b border-slate-200 px-3 py-2"><input type="checkbox" aria-label="选择当前页" checked={allPageSelected} onChange={toggleCurrentPage} /></th>
                      {[["账号", "email"], ["状态", "status"], ["类型与额度", "plan"], ["Token 与 OAuth", "health"], ["时间", "created_at"]].map(([label, key]) => (
                        <th key={key} className="border-r border-b border-slate-200 px-3 py-2"><button type="button" className="inline-flex items-center gap-1" onClick={() => toggleSort(key as SortKey)}>{label}{sortIcon(key as SortKey)}</button></th>
                      ))}
                      <th className="border-r border-b border-slate-200 px-3 py-2">固定节点</th>
                      <th className="border-b border-slate-200 px-3 py-2">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {loading && !pool ? (
                      <tr><td colSpan={8} className="p-10 text-center text-sm text-slate-500"><LoaderCircle className="mx-auto mb-2 size-5 animate-spin" />正在加载</td></tr>
                    ) : !pageItems.length ? (
                      <tr><td colSpan={8} className="p-10 text-center text-sm text-slate-500">暂无账号</td></tr>
                    ) : pageItems.map((item) => {
                      const id = selector(item);
                      const selected = selectedSet.has(id);
                      const percent = quotaPercent(item);
                      const isBad = healthBucket(item.health_status) === "dead";
                      const nodeLabel = item.registration_proxy?.node_name || item.registration_proxy_label || item.registration_proxy_node_id || "未记录";
                      const hasRegistrationNode = Boolean(item.registration_proxy?.node_name || item.registration_proxy_node_id);
                      return (
                        <tr key={id} className={`${selected ? "bg-emerald-50" : isBad ? "bg-rose-50/70" : "bg-white"} align-top transition hover:bg-slate-50`} onClick={() => setDetailId(id)}>
                          <td className="border-r border-b border-slate-100 px-3 py-3" onClick={(event) => event.stopPropagation()}><input type="checkbox" aria-label={`选择 ${item.email}`} checked={selected} onChange={() => toggleRowSelection(id)} /></td>
                          <td className="min-w-72 border-r border-b border-slate-100 px-3 py-3">
                            <div className="truncate font-mono text-xs font-semibold text-slate-950" title={item.email}>{item.email}</div>
                            <div className="mt-1 flex flex-wrap gap-1.5 text-[11px] text-slate-500">
                              <Badge variant="secondary">{item.mailbox_group || "默认"}</Badge>
                              <span>{item.provider_label || item.provider || item.source || "-"}</span>
                              <span>{item.register_mode || "-"}</span>
                            </div>
                            {item.reason ? <div className="mt-1 max-w-80 truncate text-[11px] text-amber-700" title={item.reason}>{item.reason}</div> : null}
                          </td>
                          <td className="min-w-40 border-r border-b border-slate-100 px-3 py-3">
                            <div className="flex flex-wrap gap-1.5"><Badge variant={statusVariant(item.status)}>{statusText(item.status)}</Badge><Badge variant={healthVariant(item.health_status)}>{healthText(item.health_status)}</Badge></div>
                            <div className="mt-2 text-[11px] text-slate-500">阶段：{item.stage || "-"}</div>
                            <div className="mt-1 font-mono text-[11px] text-slate-400">HTTP {item.health_code || "-"}</div>
                          </td>
                          <td className="min-w-40 border-r border-b border-slate-100 px-3 py-3">
                            <Badge variant="outline">{typeText(item)}</Badge>
                            <div className="mt-2 text-xs font-semibold text-slate-600">{quotaText(item)}</div>
                            <div className="mt-1 h-1.5 overflow-hidden bg-slate-100"><span className={`${percent === null ? "bg-slate-300" : percent > 85 ? "bg-rose-500" : percent > 60 ? "bg-amber-500" : "bg-emerald-500"} block h-full`} style={{ width: `${percent || 0}%` }} /></div>
                          </td>
                          <td className="min-w-52 border-r border-b border-slate-100 px-3 py-3">
                            <Badge variant={item.oauth_capable ? "success" : "warning"}>{oauthText(item)}</Badge>
                            <div className="mt-2 font-mono text-[11px] text-slate-600" title={item.access_token || item.access_token_preview || ""}>AT: {shortToken(item.access_token, item.access_token_preview)}</div>
                            <div className="mt-1 font-mono text-[11px] text-slate-500" title={item.refresh_token || item.refresh_token_preview || ""}>RT: {shortToken(item.refresh_token, item.refresh_token_preview)}</div>
                            <div className="mt-1 text-[11px] text-slate-400">有效期：{tokenExpiryText(item)}</div>
                          </td>
                          <td className="min-w-44 border-r border-b border-slate-100 px-3 py-3 text-xs text-slate-600">
                            <div>注册 {formatTime(item.registered_at || item.created_at)}</div>
                            <div className="mt-1">更新 {formatTime(item.updated_at)}</div>
                            <div className="mt-1">存活 {formatDuration(item.lifespan_seconds)}</div>
                          </td>
                          <td className="min-w-48 border-r border-b border-slate-100 px-3 py-3 text-xs">
                            <Badge variant={nodeLabel === "未记录" ? "warning" : "success"}>{nodeLabel === "未记录" ? "未记录" : "固定"}</Badge>
                            <div className="mt-2 max-w-52 truncate font-mono text-[11px] text-slate-600" title={nodeLabel}>{nodeLabel}</div>
                            <div className="mt-1 text-[11px] text-slate-400">最近测活：{item.health_proxy_label || "-"}</div>
                          </td>
                          <td className="min-w-80 border-b border-slate-100 px-3 py-3" onClick={(event) => event.stopPropagation()}>
                            <div className="flex flex-wrap gap-1.5">
                              <Button variant="outline" className="h-7 px-2 text-xs" onClick={() => setDetailId(id)}>查看</Button>
                              <Button variant="outline" className="h-7 px-2 text-xs" onClick={() => void copy(JSON.stringify(item, null, 2), "账号 JSON 已复制")}><Copy className="size-3" />JSON</Button>
                              <Button variant="outline" className="h-7 px-2 text-xs" onClick={() => void copy(item.access_token || "", "Access Token 已复制")} disabled={!item.access_token}>复制 AT</Button>
                              <Button variant="outline" className="h-7 px-2 text-xs" onClick={() => void copy(item.refresh_token || "", "Refresh Token 已复制")} disabled={!item.refresh_token}>复制 RT</Button>
                              <Button variant="outline" className="h-7 px-2 text-xs" onClick={() => downloadFile(jsonFileName(item), JSON.stringify(item, null, 2), "application/json;charset=utf-8")}><Download className="size-3" />下载</Button>
                              <Button variant="outline" className="h-7 px-2 text-xs" onClick={() => void checkHealth([id], true, `health:${id}`)} disabled={saving}><Activity className="size-3" />测活</Button>
                              <Button variant="outline" className="h-7 border-blue-200 px-2 text-xs text-blue-700" onClick={() => void refreshToken(item)} disabled={saving || !(item.refresh_token || item.has_refresh_token)}><RefreshCw className="size-3" />Token</Button>
                              <Button className="h-7 bg-emerald-600 px-2 text-xs text-white hover:bg-emerald-700" onClick={() => void startOAuth(item)} disabled={saving} title={hasRegistrationNode ? "使用注册时记录的节点立即执行自动 OAuth" : "打开人工 OAuth 授权页"}>{busy === `oauth-start:${id}` ? <LoaderCircle className="size-3 animate-spin" /> : <KeyRound className="size-3" />}{hasRegistrationNode ? "同节点 OAuth" : "打开授权"}</Button>
                              <Button variant="outline" className="h-7 border-sky-200 px-2 text-xs text-sky-700" onClick={() => void importWarehouse(item)} disabled={saving || !item.oauth_capable}><Send className="size-3" />Sub2API</Button>
                              <Button variant="outline" className="h-7 border-amber-200 px-2 text-xs text-amber-700" onClick={() => void doImportCpa([id])} disabled={saving || !(item.access_token || item.has_access_token)}><Send className="size-3" />CPA</Button>
                              <Button variant="outline" className="h-7 border-rose-200 px-2 text-xs text-rose-700" onClick={() => void remove(item)} disabled={saving}><Trash2 className="size-3" />删除</Button>
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>

            <div className="flex flex-wrap items-center justify-between gap-3 text-xs text-slate-500">
              <span>共 {filtered.length} 个，{safePage} / {totalPages} 页</span>
              <div className="flex items-center gap-2">
                <select value={String(pageSize)} onChange={(event) => setPageSize(Number(event.target.value))} className="h-8 border border-slate-200 bg-white px-2"><option value="20">20 / 页</option><option value="50">50 / 页</option><option value="100">100 / 页</option><option value="0">全部</option></select>
                <Button variant="outline" className="h-8" onClick={() => setPage((value) => Math.max(1, value - 1))} disabled={safePage <= 1}>上一页</Button>
                <Button variant="outline" className="h-8" onClick={() => setPage((value) => Math.min(totalPages, value + 1))} disabled={safePage >= totalPages}>下一页</Button>
              </div>
            </div>

            <div className="border-t border-slate-200 pt-4">
              <div className="mb-2 flex items-center justify-between"><div className="flex items-center gap-2 text-sm font-semibold text-slate-800"><Clock3 className="size-4" />执行记录</div><Button variant="outline" className="h-7 px-2 text-xs" onClick={() => setLogs([])} disabled={!logs.length}>清空记录</Button></div>
              <div className="max-h-44 overflow-auto border border-slate-200 bg-slate-950 p-3 font-mono text-xs text-slate-200">
                {!logs.length ? <div className="text-slate-500">暂无执行记录</div> : logs.map((log, index) => <div key={`${log.time}-${index}`} className={log.level === "error" ? "text-rose-300" : log.level === "warning" ? "text-amber-300" : log.level === "success" ? "text-emerald-300" : "text-slate-300"}><span className="text-slate-500">[{formatTime(log.time)}]</span> {log.text}</div>)}
              </div>
            </div>
          </CardContent>
        </Card>
      </div>

      {detail ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/45 p-4 backdrop-blur-xs">
          <div className="max-h-[90vh] w-full max-w-4xl overflow-hidden border border-slate-200 bg-white shadow-2xl">
            <div className="flex items-center justify-between border-b border-slate-200 bg-slate-50 px-5 py-4">
              <div><div className="text-sm font-semibold text-slate-950">半成品账号详情</div><div className="mt-1 font-mono text-xs text-slate-500">{detail.email}</div></div>
              <Button variant="outline" onClick={() => setDetailId("")}><X className="size-4" />关闭</Button>
            </div>
            <div className="max-h-[calc(90vh-76px)] space-y-5 overflow-auto p-5">
              <div className="flex flex-wrap gap-2">
                <Button variant="outline" onClick={() => void checkHealth([selector(detail)], true, "detail-health")} disabled={saving}><Activity className="size-4" />测活和额度</Button>
                <Button variant="outline" className="border-indigo-200 text-indigo-700" onClick={() => void checkHealth([selector(detail)], false, "detail-type", "refresh_type")} disabled={saving}><RefreshCw className="size-4" />刷新类型</Button>
                <Button variant="outline" className="border-blue-200 text-blue-700" onClick={() => void refreshToken(detail)} disabled={saving || !(detail.refresh_token || detail.has_refresh_token)}><RefreshCw className="size-4" />刷新 Token</Button>
                <Button onClick={() => void startOAuth(detail)} disabled={saving}><KeyRound className="size-4" />获取 OAuth 并导入</Button>
                <Button variant="outline" className="border-sky-200 text-sky-700" onClick={() => void importWarehouse(detail)} disabled={saving || !detail.oauth_capable}><Send className="size-4" />导入 Sub2API</Button>
                <Button variant="outline" className="border-amber-200 text-amber-700" onClick={() => void doImportCpa([selector(detail)])} disabled={saving || !(detail.access_token || detail.has_access_token)}><Send className="size-4" />导入 CPA</Button>
                <Button variant="outline" onClick={() => void doExportSub2Api([selector(detail)])} disabled={saving || !(detail.access_token || detail.has_access_token)}><Download className="size-4" />导出 JSON</Button>
                <Button variant="outline" className="border-rose-200 text-rose-700" onClick={() => void remove(detail)} disabled={saving}><Trash2 className="size-4" />删除</Button>
              </div>

              <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                {[
                  ["邮箱", "email"], ["密码", "password"], ["状态", "status"], ["阶段", "stage"],
                  ["套餐类型", "plan_type"], ["账号类型", "account_type"], ["邮箱分组", "mailbox_group"],
                  ["邮箱来源", "provider_label"], ["注册模式", "register_mode"],
                ].map(([label, key]) => (
                  <label key={key} className="space-y-1.5 text-xs font-semibold text-slate-600">{label}<Input value={editDraft[key as keyof EditDraft]} onChange={(event) => setEditDraft((current) => ({ ...current, [key]: event.target.value }))} className="font-mono text-xs" /></label>
                ))}
              </div>
              <label className="block space-y-1.5 text-xs font-semibold text-slate-600">Access Token<Textarea value={editDraft.access_token} onChange={(event) => setEditDraft((current) => ({ ...current, access_token: event.target.value }))} className="min-h-24 font-mono text-xs" /></label>
              <label className="block space-y-1.5 text-xs font-semibold text-slate-600">Refresh Token<Textarea value={editDraft.refresh_token} onChange={(event) => setEditDraft((current) => ({ ...current, refresh_token: event.target.value }))} className="min-h-20 font-mono text-xs" /></label>
              <div className="grid gap-3 md:grid-cols-2">
                <label className="space-y-1.5 text-xs font-semibold text-slate-600">原因<Textarea value={editDraft.reason} onChange={(event) => setEditDraft((current) => ({ ...current, reason: event.target.value }))} /></label>
                <label className="space-y-1.5 text-xs font-semibold text-slate-600">备注<Textarea value={editDraft.note} onChange={(event) => setEditDraft((current) => ({ ...current, note: event.target.value }))} /></label>
              </div>
              <div className="flex justify-end"><Button onClick={() => void saveDetail()} disabled={saving}>{busy === "save-detail" ? <LoaderCircle className="size-4 animate-spin" /> : <Save className="size-4" />}保存修改</Button></div>

              <div className="grid gap-3 text-xs md:grid-cols-2">
                {[
                  ["测活", `${healthText(detail.health_status)} / HTTP ${detail.health_code || "-"}`],
                  ["测活错误", detail.health_error || "-"],
                  ["上次尝试", `${healthText(detail.last_health_attempt_status)} / ${formatTime(detail.last_health_attempt_at)}`],
                  ["额度", `${quotaText(detail)} / ${formatTime(detail.quota_checked_at)}`],
                  ["OAuth", `${oauthText(detail)} / ${detail.oauth_client_kind || "-"}`],
                  ["Token 到期", tokenExpiryText(detail)],
                  ["注册节点", detail.registration_proxy?.node_name || detail.registration_proxy_label || detail.registration_proxy_node_id || "未记录"],
                  ["测活节点", detail.health_proxy_label || detail.health_proxy_node_id || "-"],
                  ["Sub2API", `${detail.sub2api_import_status || "未导入"} / ${detail.sub2api_account_id || "-"}`],
                  ["CPA", `${detail.cpa_import_status || "未导入"} / ${detail.cpa_auth_file || "-"}`],
                  ["注册时间", formatTime(detail.registered_at || detail.created_at)],
                  ["存活时长", formatDuration(detail.lifespan_seconds)],
                ].map(([label, value]) => (
                  <div key={label} className="space-y-1"><div className="font-semibold text-slate-500">{label}</div><div className="relative break-all border border-slate-200 bg-slate-50 p-2 pr-8 font-mono text-slate-800">{value}<button type="button" className="absolute right-1.5 top-1.5 bg-white p-1 text-slate-400 shadow-sm hover:text-slate-700" onClick={() => void copy(String(value), `${label} 已复制`)} title={`复制 ${label}`}><Copy className="size-3.5" /></button></div></div>
                ))}
              </div>
            </div>
          </div>
        </div>
      ) : null}

      <AccountOAuthDialog session={oauthSession} busy={busy === "oauth-complete"} onClose={() => setOauthSession(null)} onComplete={completeOAuth} />
    </>
  );
}
