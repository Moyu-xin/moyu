"use client";

import { useEffect, useMemo, useRef, useState, type ChangeEvent } from "react";
import { Activity, ArrowDown, ArrowUp, ArrowUpDown, Clock3, Copy, Download, KeyRound, LoaderCircle, RefreshCw, Send, ShieldAlert, Trash2, Upload, X, Zap } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  checkAccountHealth,
  acquireAccountOAuthAutomatically,
  checkAccountQuota,
  clearAccountPool,
  completeAccountOAuth,
  deleteAccount,
  exportAccountsToSub2ApiData,
  fetchAccountHealth,
  fetchAccountPool,
  importAccounts,
  importAccountsToCpa,
  importAccountsToSub2Api,
  pruneBannedAccounts,
  refreshAccountTypes,
  startAccountOAuth,
  startAccountHealth,
  startAccountHealthSchedule,
  stopAccountHealth,
  stopAccountHealthSchedule,
  updateAccountHealthSettings,
  updateAccount as updateAccountApi,
  refreshAccountToken,
  type AccountInventoryState,
  type AccountItem,
  type AccountOAuthSession,
  type AccountPool,
} from "@/lib/api";
import { AccountOAuthDialog } from "./account-oauth-dialog";

function formatTime(v?: string | number) {
  if (!v) return "-";
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? String(v) : d.toLocaleString();
}

function n(v: unknown) {
  const x = Number(v || 0);
  return Number.isFinite(x) ? x : 0;
}

function importFailureText(error: string | undefined, response: unknown, statusCode: number | undefined) {
  if (error) return error;
  if (response !== undefined && response !== null && response !== "") {
    if (typeof response === "string") return response;
    try {
      return JSON.stringify(response);
    } catch {
      return String(response);
    }
  }
  return `HTTP ${statusCode || 0}`;
}

function quotaText(i?: AccountItem) {
  if (!i) return "未知";
  const status = String(i.quota_status || "").toLowerCase();
  if (status === "failed") return i.last_quota_error ? `失败：${healthText(i.quota_error_status || "quota_failed")}` : "同步失败";
  const used = i.quota_used;
  const total = i.quota_total;
  const remaining = i.quota_remaining;
  if (used !== undefined && total !== undefined) return `${used}/${total}`;
  if (remaining !== undefined) return `剩余 ${remaining}`;
  return i.quota_status && i.quota_status !== "unknown" ? i.quota_status : "未知";
}

function quotaPercent(i?: AccountItem) {
  if (!i) return null;
  const total = Number(i.quota_total);
  const used = i.quota_used !== undefined ? Number(i.quota_used) : i.quota_remaining !== undefined ? total - Number(i.quota_remaining) : NaN;
  if (!Number.isFinite(total) || total <= 0 || !Number.isFinite(used)) return null;
  return Math.max(0, Math.min(100, (used / total) * 100));
}

function healthText(v?: string) {
  const s = String(v || "unknown").toLowerCase();
  if (["ok", "alive", "healthy"].includes(s)) return "存活";
  if (s === "missing_token") return "缺 Token";
  if (s === "missing_sub2api_id") return "缺 sub2api ID";
  if (s === "token_expired") return "Token 过期";
  if (s === "token_invalid") return "Token 无效";
  if (s === "auth_invalid") return "鉴权失败";
  if (s === "refresh_token_missing") return "缺 RT";
  if (s === "refresh_token_expired") return "RT 过期";
  if (s === "refresh_token_invalid") return "RT 失效";
  if (s === "refresh_token_refresh_failed") return "RT 刷新失败";
  if (s === "cloudflare_blocked") return "Cloudflare";
  if (s === "region_blocked") return "地区受限";
  if (s === "rate_limited") return "限流";
  if (s === "proxy_error") return "代理错误";
  if (s === "timeout") return "超时";
  if (s === "network_error") return "网络错误";
  if (s === "upstream_error") return "上游错误";
  if (s === "quota_failed") return "额度失败";
  if (s === "quota_unknown") return "额度未知";
  if (["banned", "invalid", "dead", "disabled"].includes(s)) return "不可用";
  if (s === "blocked") return "受阻";
  if (s === "failed") return "失败";
  return "未知";
}

function healthVariant(v?: string): "success" | "secondary" | "danger" | "outline" | "warning" | "info" {
  const s = String(v || "unknown").toLowerCase();
  if (["ok", "alive", "healthy"].includes(s)) return "success";
  if (["banned", "invalid", "dead", "disabled", "token_invalid", "auth_invalid", "refresh_token_invalid", "refresh_token_expired"].includes(s)) return "danger";
  if (["missing_token", "missing_sub2api_id", "token_expired", "refresh_token_missing", "refresh_token_refresh_failed", "cloudflare_blocked", "region_blocked", "rate_limited", "quota_failed", "quota_unknown"].includes(s)) return "warning";
  if (["proxy_error", "timeout", "network_error", "upstream_error", "blocked"].includes(s)) return "info";
  if (["failed"].includes(s)) return "secondary";
  return "outline";
}

const failedHealthStatuses = new Set([
  "failed",
  "dead",
  "banned",
  "invalid",
  "disabled",
  "missing_token",
  "missing_sub2api_id",
  "token_expired",
  "token_invalid",
  "auth_invalid",
  "refresh_token_missing",
  "refresh_token_expired",
  "refresh_token_invalid",
  "refresh_token_refresh_failed",
  "cloudflare_blocked",
  "region_blocked",
  "rate_limited",
  "proxy_error",
  "timeout",
  "network_error",
  "upstream_error",
  "blocked",
  "quota_failed",
  "quota_unknown",
]);

function accountRtStatus(i?: AccountItem) {
  const status = String(i?.oauth_refresh_status || "").toLowerCase();
  if (status && status !== "ok") return status;
  if (i?.token_refresh_required) return "refresh_token_invalid";
  return "";
}

function rtWarningText(i?: AccountItem) {
  const status = accountRtStatus(i);
  if (!status) return "";
  const error = String(i?.oauth_refresh_error || "").trim();
  return `${healthText(status)}${error ? `：${error.slice(0, 80)}` : "，需重新登录/重新导入有效 OAuth"}`;
}

function healthFilterMatches(filter: string, health: string, rtStatus = "") {
  if (filter === "all") return true;
  if (filter === "failed_group") return failedHealthStatuses.has(health);
  if (filter === "token_group") return ["missing_token", "token_expired", "token_invalid", "auth_invalid"].includes(health) || Boolean(rtStatus);
  if (filter === "rt_group") return Boolean(rtStatus);
  if (filter === "network_group") return ["proxy_error", "timeout", "network_error", "upstream_error"].includes(health);
  if (filter === "blocked_group") return ["cloudflare_blocked", "region_blocked", "rate_limited", "blocked"].includes(health);
  return health === filter || rtStatus === filter;
}

function typeText(i?: AccountItem) {
  const accountType = String(i?.account_type || "").toLowerCase();
  const t = String(i?.plan_type || i?.account_type || "free").toLowerCase();
  if (["plus", "chatgpt_plus", "paid", "premium"].includes(t)) return "Plus";
  if (t === "pro") return "Pro";
  if (["team", "business", "enterprise"].includes(t)) return "团队";
  if (i?.k12_mode || accountType === "k12" || t === "k12") return "K12";
  if (t === "free") return "Free";
  return t || "未知";
}

function saveJsonFile(name: string, data: unknown) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

function accountSelector(account?: AccountItem) {
  return String(account?.inventory_id || account?.inventory_key || account?.email || "");
}

type AccountSortKey = "email" | "plan" | "created_at" | "expires_at" | "sub2api";
type SortDirection = "asc" | "desc";

function accountSortValue(account: AccountItem, key: AccountSortKey): string | number {
  if (key === "email") return String(account.email || "").toLowerCase();
  if (key === "plan") return typeText(account).toLowerCase();
  if (key === "expires_at") return tokenExpiresAtMs(account) || 0;
  if (key === "sub2api") return account.sub2api_account_id ? 1 : 0;
  return new Date(account.created_at || 0).getTime() || 0;
}

function mailboxGroupLabel(account?: Pick<AccountItem, "mailbox_group">) {
  return String(account?.mailbox_group || "默认");
}

function shortToken(value?: string, preview?: string) {
  const text = String(value || preview || "").trim();
  if (!text) return "-";
  if (text.length <= 24) return text;
  return `${text.slice(0, 12)}…${text.slice(-8)}`;
}

function tokenStateText(i?: AccountItem) {
  const hasAccess = Boolean(i?.access_token || i?.has_access_token);
  const hasRefresh = Boolean(i?.refresh_token || i?.has_refresh_token);
  if (hasAccess && hasRefresh) return "AT + RT";
  if (hasAccess) return "仅 AT";
  if (hasRefresh) return "仅 RT";
  return "缺 Token";
}

function oauthStateText(i?: AccountItem) {
  const status = String(i?.oauth_token_status || "missing");
  if (status === "valid_refresh") return "OAuth 可用";
  if (status === "valid_access_only") return "OAuth AT 可用";
  if (status === "invalid_refresh") return "OAuth RT 失效";
  if (status === "incompatible_refresh") return "非 sub2api OAuth RT";
  if (status === "web_session_only") return "仅网页会话";
  if (status === "mailbox_refresh_token") return "邮箱 RT";
  if (status === "unverified_refresh") return "RT 未验证";
  return "缺 OAuth";
}

function readinessMatches(filter: string, account: AccountItem) {
  if (filter === "all") return true;
  if (filter === "finished") return Boolean(account.finished_account);
  if (filter === "needs_oauth") return Boolean(account.needs_oauth);
  if (filter === "sub2api_healthy") return Boolean(account.sub2api_healthy);
  if (filter === "oauth_sub2api") return account.oauth_client_kind === "sub2api" && Boolean(account.oauth_capable);
  if (filter === "oauth_inventory") return account.oauth_client_kind === "inventory";
  if (filter === "warehouse_failed") return account.warehouse_status === "failed";
  return account.oauth_token_status === filter || account.warehouse_status === filter;
}

function productStateText(account: AccountItem) {
  if (account.finished_account) return "成品";
  if (account.needs_oauth) return "待 OAuth";
  return "不可用";
}

function productStateVariant(account: AccountItem): "success" | "danger" | "warning" {
  if (account.finished_account) return "success";
  if (account.needs_oauth) return "warning";
  return "danger";
}

function tokenExpiresAtMs(i?: AccountItem) {
  const raw = i?.token_expires_at;
  if (raw !== undefined && raw !== null && raw !== "") {
    if (typeof raw === "number" && Number.isFinite(raw)) return raw > 1e12 ? raw : raw * 1000;
    const text = String(raw).trim();
    const numeric = Number(text);
    if (text && Number.isFinite(numeric)) return numeric > 1e12 ? numeric : numeric * 1000;
    const parsed = Date.parse(text);
    if (Number.isFinite(parsed)) return parsed;
  }
  const expiresIn = Number(i?.token_expires_in);
  if (Number.isFinite(expiresIn) && expiresIn > 0) return Date.now() + expiresIn * 1000;
  return null;
}

function tokenExpiryText(i?: AccountItem) {
  const expiresAt = tokenExpiresAtMs(i);
  if (!expiresAt) return "未知";
  const seconds = Math.floor((expiresAt - Date.now()) / 1000);
  if (seconds <= 0) return "已过期";
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.floor(hours / 24);
  return `${days}d`;
}

function tokenExpiryVariant(i?: AccountItem): "success" | "secondary" | "danger" | "outline" | "warning" | "info" {
  const expiresAt = tokenExpiresAtMs(i);
  if (!expiresAt) return "outline";
  const seconds = Math.floor((expiresAt - Date.now()) / 1000);
  if (seconds <= 0) return "danger";
  if (seconds < 3600) return "warning";
  return "success";
}

function statusText(value?: string) {
  const status = String(value || "active").toLowerCase();
  if (status === "active") return "正常";
  if (status === "disabled") return "禁用";
  if (status === "banned") return "封禁";
  if (status === "invalid") return "无效";
  return status;
}

function jsonFileName(account: AccountItem) {
  const email = String(account.email || "account").replace(/[^a-zA-Z0-9._-]+/g, "_");
  return `account-${email}-${account.inventory_id || Date.now()}.json`;
}

export function AccountPoolCard({ active = true }: { active?: boolean }) {
  const loadedRef = useRef(false);
  const [pool, setPool] = useState<AccountPool | null>(null);
  const [inventory, setInventory] = useState<AccountInventoryState | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyAction, setBusyAction] = useState("");
  const saving = Boolean(busyAction);
  const importing = busyAction === "import" || busyAction === "file-import";
  const [selectedAccountId, setSelectedAccountId] = useState("");
  const [importText, setImportText] = useState("");
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [healthFilter, setHealthFilter] = useState("all");
  const [typeFilter, setTypeFilter] = useState("all");
  const [groupFilter, setGroupFilter] = useState("all");
  const [readinessFilter, setReadinessFilter] = useState("all");
  const [sortKey, setSortKey] = useState<AccountSortKey>("created_at");
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [selectedAccountIds, setSelectedAccountIds] = useState<string[]>([]);
  const [autoCheckOnImport, setAutoCheckOnImport] = useState(false);
  const [dropBannedOnImport, setDropBannedOnImport] = useState(false);
  const [healthMethod, setHealthMethod] = useState("auto");
  const [healthConcurrency, setHealthConcurrency] = useState(10);
  const [oauthSession, setOauthSession] = useState<AccountOAuthSession | null>(null);

  const selected = useMemo<AccountItem | undefined>(() => pool?.accounts.find((i) => accountSelector(i) === selectedAccountId), [pool, selectedAccountId]);
  const stats = pool?.stats || {};
  const logs = useMemo(() => [...(inventory?.logs || [])].reverse(), [inventory]);
  const selectedSet = useMemo(() => new Set(selectedAccountIds), [selectedAccountIds]);
  const selectedSelectors = useMemo(() => Array.from(new Set(selectedAccountIds.filter(Boolean))), [selectedAccountIds]);
  const groupOptions = useMemo(() => Array.from(new Set(["默认", ...(pool?.accounts || []).map(mailboxGroupLabel)]))
    .filter(Boolean)
    .sort((a, b) => a.localeCompare(b, "zh-Hans-CN")), [pool]);

  const appendUiLog = (text: string, level = "info") => {
    const entry = { time: new Date().toISOString(), text, level };
    setInventory((prev) => prev ? { ...prev, logs: [...(prev.logs || []), entry] } : prev);
  };

  const copyText = async (value: string, label: string) => {
    const text = String(value || "");
    if (!text.trim()) return toast.error(`${label} 为空`);
    try {
      await navigator.clipboard.writeText(text);
      toast.success(`${label} 已复制`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : `${label} 复制失败`);
    }
  };

  const copyAccountJson = async (account: AccountItem) => {
    await copyText(JSON.stringify(account, null, 2), "完整 JSON");
  };

  const downloadAccountJson = (account: AccountItem) => {
    saveJsonFile(jsonFileName(account), account);
    toast.success("完整 JSON 已下载");
  };

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const accounts = (pool?.accounts || []).filter((i) => {
      const health = String(i.health_status || i.sub2api_health_status || "unknown").toLowerCase();
      const rtStatus = accountRtStatus(i);
      const plan = String(i.plan_type || "").toLowerCase();
      const accountType = String(i.account_type || "").toLowerCase();
      const type = ["plus", "pro", "team", "business", "enterprise"].includes(plan)
        ? plan
        : String(i.k12_mode || accountType === "k12" ? "k12" : (plan || accountType || "free")).toLowerCase();
      const status = String(i.status || "active").toLowerCase();
      const group = mailboxGroupLabel(i);
      return (statusFilter === "all" || status === statusFilter)
        && healthFilterMatches(healthFilter, health, rtStatus)
        && readinessMatches(readinessFilter, i)
        && (typeFilter === "all" || type === typeFilter || (typeFilter === "team" && ["team", "business", "enterprise"].includes(type)))
        && (groupFilter === "all" || group === groupFilter)
        && (!q || `${i.email} ${group} ${i.note || ""} ${i.plan_type || ""} ${i.account_type || ""} ${i.health_status || ""} ${i.health_error || ""} ${i.oauth_status || ""} ${i.oauth_refresh_status || ""} ${i.oauth_refresh_error || ""} ${i.oauth_token_status || ""} ${i.refresh_token_kind || ""} ${i.refresh_token_label || ""} ${i.warehouse_status || ""} ${i.quota_error_status || ""} ${i.last_quota_error || ""} ${i.sub2api_account_id || ""} ${i.chatgpt_account_id || ""} ${i.workspace_id || ""} ${i.inventory_id || ""}`.toLowerCase().includes(q));
    });
    accounts.sort((a, b) => {
      const left = accountSortValue(a, sortKey);
      const right = accountSortValue(b, sortKey);
      const compared = typeof left === "number" && typeof right === "number"
        ? left - right
        : String(left).localeCompare(String(right), "zh-Hans-CN", { numeric: true, sensitivity: "base" });
      return sortDirection === "asc" ? compared : -compared;
    });
    return accounts;
  }, [pool, query, statusFilter, healthFilter, readinessFilter, typeFilter, groupFilter, sortKey, sortDirection]);
  const filteredSelectors = useMemo(() => Array.from(new Set(filtered.map(accountSelector).filter(Boolean))), [filtered]);
  const filteredOAuthSelectors = useMemo(() => Array.from(new Set(filtered.filter((account) => account.oauth_capable).map(accountSelector).filter(Boolean))), [filtered]);
  const selectedOAuthSelectors = useMemo(() => {
    const selectedIds = new Set(selectedSelectors);
    return (pool?.accounts || []).filter((account) => account.oauth_capable && selectedIds.has(accountSelector(account))).map(accountSelector).filter(Boolean);
  }, [pool, selectedSelectors]);

  const effectivePageSize = pageSize <= 0 ? Math.max(filtered.length, 1) : pageSize;
  const totalPages = Math.max(1, Math.ceil(filtered.length / effectivePageSize));
  const safePage = Math.min(page, totalPages);
  const pageItems = filtered.slice((safePage - 1) * effectivePageSize, safePage * effectivePageSize);

  useEffect(() => { if (page > totalPages) setPage(totalPages); }, [page, totalPages]);
  useEffect(() => {
    if (!pool?.accounts.length) {
      setSelectedAccountIds([]);
      return;
    }
    const valid = new Set(pool.accounts.map(accountSelector).filter(Boolean));
    setSelectedAccountIds((items) => items.filter((item) => valid.has(item)));
  }, [pool]);

  const toggleRowSelection = (selector: string) => {
    if (!selector) return;
    setSelectedAccountIds((items) => items.includes(selector) ? items.filter((item) => item !== selector) : [...items, selector]);
  };

  const selectCurrentPage = () => {
    const pageSelectors = pageItems.map(accountSelector).filter(Boolean);
    setSelectedAccountIds((items) => Array.from(new Set([...items, ...pageSelectors])));
  };

  const toggleSort = (key: AccountSortKey) => {
    if (sortKey === key) {
      setSortDirection((direction) => direction === "asc" ? "desc" : "asc");
    } else {
      setSortKey(key);
      setSortDirection(key === "email" || key === "plan" ? "asc" : "desc");
    }
    setPage(1);
  };

  const sortIcon = (key: AccountSortKey) => sortKey !== key
    ? <ArrowUpDown className="size-3.5 text-slate-400" />
    : sortDirection === "asc"
      ? <ArrowUp className="size-3.5 text-emerald-600" />
      : <ArrowDown className="size-3.5 text-emerald-600" />;

  const applyInventory = (nextInventory?: AccountInventoryState | null) => {
    if (!nextInventory) return;
    setInventory(nextInventory);
    setAutoCheckOnImport(Boolean(nextInventory.settings.import_auto_health_check));
    setDropBannedOnImport(Boolean(nextInventory.settings.import_drop_banned));
    setHealthMethod(nextInventory.settings.method || "auto");
    setHealthConcurrency(Math.max(1, Math.min(64, Number(nextInventory.settings.concurrency || 10))));
  };

  const load = async (silent = false, includeAccounts = true, preview = false) => {
    if (!silent) setLoading(true);
    try {
      if (includeAccounts) {
        const [poolData, invData] = await Promise.all([fetchAccountPool({ force: silent, preview }), fetchAccountHealth().catch(() => null)]);
        setPool(poolData.account_pool);
        setSelectedAccountId((current) => current && !poolData.account_pool.accounts.some((item) => accountSelector(item) === current) ? "" : current);
        applyInventory(invData?.inventory);
        if (preview && poolData.account_pool.page_info?.partial) {
          window.setTimeout(() => {
            void fetchAccountPool({ force: true }).then((fullData) => {
              setPool(fullData.account_pool);
              setSelectedAccountId((current) => current && !fullData.account_pool.accounts.some((item) => accountSelector(item) === current) ? "" : current);
            }).catch(() => undefined);
          }, 600);
        }
      } else {
        const invData = await fetchAccountHealth();
        applyInventory(invData.inventory);
      }
    } catch (e) {
      if (includeAccounts || !silent) toast.error(e instanceof Error ? e.message : "账号库存读取失败");
    } finally {
      if (!silent) setLoading(false);
    }
  };

  useEffect(() => {
    if (!active) return;
    const firstLoad = !loadedRef.current;
    if (firstLoad) void load(false, true, true).finally(() => { loadedRef.current = true; });
    else setLoading(false);
    const timer = window.setInterval(() => void load(true, false), 6000);
    return () => window.clearInterval(timer);
  }, [active]);

  const doImportContent = async (text: string, label = "导入", source: "text" | "file" = "text") => {
    if (!text.trim()) return toast.error("请选择文件或粘贴账号内容");
    setBusyAction(source === "file" ? "file-import" : "import");
    appendUiLog(`${label}开始，导入后自动测活=${autoCheckOnImport ? "是" : "否"}`);
    try {
      await updateAccountHealthSettings({ import_auto_health_check: autoCheckOnImport, import_drop_banned: dropBannedOnImport, method: healthMethod, concurrency: healthConcurrency });
      const data = await importAccounts(text, { auto_health_check: autoCheckOnImport, drop_banned: dropBannedOnImport, health_method: healthMethod });
      setPool(data.account_pool);
      if (data.inventory) setInventory(data.inventory);
      toast.success(`${label}完成：新增 ${data.account_pool.imported || 0}，更新 ${data.account_pool.updated || 0}`);
      await load(true);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : `${label}失败`);
    } finally {
      setBusyAction("");
    }
  };

  const doImport = async () => {
    await doImportContent(importText, "导入");
    setImportText("");
  };

  const doImportFiles = async (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files || []);
    event.target.value = "";
    if (!files.length) return;
    try {
      const chunks = await Promise.all(files.map(async (file) => `\n${await file.text()}\n`));
      await doImportContent(chunks.join("\n"), `文件批量导入 ${files.length} 个文件`, "file");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "文件读取失败");
    }
  };

  const doCheck = async (emails: string[] = [], quota = true) => {
    setBusyAction(quota ? "check" : "health");
    appendUiLog(`${quota ? "测活+额度" : "账号测活"}已提交，目标=${emails.length ? emails.length : "全部"}`);
    try {
      const data = await checkAccountHealth({ emails, method: healthMethod, refresh_quota: quota, concurrency: healthConcurrency });
      setPool(data.account_pool);
      if (data.inventory) setInventory(data.inventory);
      if (data.inventory_health?.busy) {
        toast.warning(String(data.inventory_health.message || "已有账号库存任务正在运行"));
        return;
      }
      toast.success(quota ? "测活+额度完成" : "测活完成");
      await load(true);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "测活失败");
    } finally {
      setBusyAction("");
    }
  };

  const doQuota = async (emails: string[] = []) => {
    setBusyAction("quota");
    appendUiLog(`同步额度已提交，目标=${emails.length ? emails.length : "全部"}`);
    try {
      const data = await checkAccountQuota({ emails, method: "local", concurrency: healthConcurrency });
      setPool(data.account_pool);
      if (data.inventory) setInventory(data.inventory);
      if (data.inventory_health?.busy) {
        toast.warning(String(data.inventory_health.message || "已有账号库存任务正在运行"));
        return;
      }
      const fail = Number(data.inventory_health?.fail || 0);
      const success = Number(data.inventory_health?.success || 0);
      if (fail > 0) toast.warning(`额度同步完成：成功 ${success}，失败 ${fail}`);
      else toast.success(`额度同步完成：成功 ${success}`);
      await load(true);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "额度同步失败");
    } finally {
      setBusyAction("");
    }
  };

  const doRefreshTypes = async (emails: string[] = []) => {
    setBusyAction("type-refresh");
    appendUiLog(`刷新账号类型已提交，目标=${emails.length ? emails.length : "全部"}；会强制刷新 OAuth/读取 ChatGPT 账号元信息并同步 Free/Plus/Pro/K12。`);
    try {
      const data = await refreshAccountTypes({ emails, method: "local", mode: "refresh_type", refresh_quota: false, concurrency: healthConcurrency });
      setPool(data.account_pool);
      if (data.inventory) setInventory(data.inventory);
      if (data.inventory_health?.busy) {
        toast.warning(String(data.inventory_health.message || "已有账号库存任务正在运行"));
        return;
      }
      const fail = Number(data.inventory_health?.fail || 0);
      const success = Number(data.inventory_health?.success || 0);
      if (fail > 0) toast.warning(`类型刷新完成：成功 ${success}，失败 ${fail}`);
      else toast.success(`类型刷新完成：成功 ${success}`);
      await load(true);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "类型刷新失败");
    } finally {
      setBusyAction("");
    }
  };

  const doStartBackground = async () => {
    setBusyAction("background");
    appendUiLog("后台测活启动请求已提交");
    try {
      await updateAccountHealthSettings({ method: healthMethod, concurrency: healthConcurrency });
      const data = await startAccountHealth({ method: healthMethod, refresh_quota: true, concurrency: healthConcurrency });
      setInventory(data.inventory);
      if (data.inventory?.busy) {
        toast.warning(String(data.inventory.message || "已有账号库存任务正在运行"));
        return;
      }
      toast.success("后台测活已启动");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "启动失败");
    } finally {
      setBusyAction("");
    }
  };

  const doStopBackground = async () => {
    const data = await stopAccountHealth();
    setInventory(data.inventory);
    toast.success("已停止");
  };

  const toggleSchedule = async () => {
    setBusyAction("schedule");
    appendUiLog(inventory?.settings.scheduled_enabled ? "关闭定时测活请求已提交" : "开启定时测活请求已提交");
    try {
      const data = inventory?.settings.scheduled_enabled
        ? await stopAccountHealthSchedule()
        : await startAccountHealthSchedule({ method: healthMethod, refresh_quota_on_health: true, concurrency: healthConcurrency });
      setInventory(data.inventory);
      toast.success(data.inventory.settings.scheduled_enabled ? "定时测活已开启" : "定时测活已关闭");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "定时设置失败");
    } finally {
      setBusyAction("");
    }
  };

  const doPrune = async () => {
    setBusyAction("prune");
    appendUiLog("剔除不可用账号请求已提交");
    try {
      const data = await pruneBannedAccounts();
      setPool(data.account_pool);
      toast.success(`已剔除 ${data.account_pool.removed || 0} 个不可用账号`);
      await load(true);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "剔除失败");
    } finally {
      setBusyAction("");
    }
  };

  const doSub2ApiImport = async (emails: string[]) => {
    setBusyAction("sub2api-import");
    appendUiLog(`sub2api 导入已提交，目标=${emails.length ? emails.length : "全部"}`);
    try {
      const data = await importAccountsToSub2Api(emails);
      setPool(data.account_pool);
      const result = data.sub2api;
      const failed = (result.results || []).filter((item) => !item.ok);
      failed.slice(0, 20).forEach((item) => appendUiLog(`sub2api 导入失败：${item.email || item.name || "未知账号"}，${importFailureText(item.error, item.response, item.status_code)}`));
      if (result.ok) {
        toast.success(`sub2api 导入完成：成功 ${result.success}/${result.total}`);
      } else if (result.partial) {
        toast.warning(`sub2api 部分成功：成功 ${result.success}，失败 ${result.fail}`);
      } else {
        toast.error(`sub2api 全部失败：${result.fail}/${result.total}`);
      }
      await load(true);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "sub2api 导入失败");
    } finally {
      setBusyAction("");
    }
  };

  const doCpaImport = async (emails: string[]) => {
    setBusyAction("cpa-import");
    appendUiLog(`CPA 导入已提交，目标=${emails.length ? emails.length : "全部"}`);
    try {
      const data = await importAccountsToCpa(emails);
      setPool(data.account_pool);
      const result = data.cpa;
      const failed = (result.results || []).filter((item) => !item.ok);
      failed.slice(0, 20).forEach((item) => appendUiLog(`CPA 导入失败：${item.email || item.name || item.filename || "未知账号"}，${importFailureText(item.error, item.response, item.status_code)}`));
      if (result.ok) {
        toast.success(`CPA 导入完成：成功 ${result.success}/${result.total}`);
      } else if (result.partial) {
        toast.warning(`CPA 部分成功：成功 ${result.success}，失败 ${result.fail}`);
      } else {
        toast.error(`CPA 全部失败：${result.fail}/${result.total}`);
      }
      await load(true);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "CPA 导入失败");
    } finally {
      setBusyAction("");
    }
  };

  const doExportSub2Api = async (emails: string[]) => {
    setBusyAction("sub2api-export");
    try {
      const data = await exportAccountsToSub2ApiData(emails);
      saveJsonFile(`sub2api-accounts-${Date.now()}.json`, (data.sub2api as any).data);
      toast.success("已导出 sub2api JSON");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "导出失败");
    } finally {
      setBusyAction("");
    }
  };

  const doRefreshToken = async (selector: string) => {
    if (!selector) return;
    setBusyAction("refresh-token");
    appendUiLog(`刷新 Token 已提交：${selector}`);
    try {
      const data = await refreshAccountToken(selector);
      setPool(data.account_pool);
      toast.success("Token 已刷新，并已重新同步账号类型");
      await load(true);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "RT 刷新失败：需要重新登录或重新导入有效 OAuth");
    } finally {
      setBusyAction("");
    }
  };

  const startOAuth = async (account: AccountItem) => {
    const selector = accountSelector(account);
    if (!selector) return;
    if (account.registration_proxy?.node_name || account.registration_proxy_node_id) {
      if (!window.confirm(`将使用 ${account.registration_proxy?.node_name || account.registration_proxy_label || "注册节点"} 立即执行自动 OAuth，并尝试导入 sub2api。是否继续？`)) return;
      setBusyAction("oauth-start");
      appendUiLog(`通过注册节点获取 OpenAI OAuth：${account.email}`);
      try {
        const data = await acquireAccountOAuthAutomatically("registered", selector);
        setPool(data.oauth.account_pool);
        if (data.oauth.warehouse.ok) toast.success("OAuth 已通过注册节点获取，sub2api 已更新并通过测活");
        else toast.warning("OAuth 已通过注册节点获取，但 sub2api 导入或测活未通过");
        await load(true);
      } catch (error) {
        toast.error(error instanceof Error ? error.message : "同节点 OAuth 获取失败");
      } finally {
        setBusyAction("");
      }
      return;
    }
    const popup = window.open("about:blank", "_blank");
    setBusyAction("oauth-start");
    appendUiLog(`生成 OpenAI OAuth 授权链接：${account.email}`);
    try {
      const data = await startAccountOAuth("registered", selector);
      setOauthSession(data.oauth);
      if (popup) {
        popup.opener = null;
        popup.location.href = data.oauth.auth_url;
      } else {
        toast.warning("浏览器拦截了新窗口，请在授权弹窗中点击“打开授权页”");
      }
    } catch (error) {
      popup?.close();
      toast.error(error instanceof Error ? error.message : "生成 OAuth 授权链接失败");
    } finally {
      setBusyAction("");
    }
  };

  const completeOAuth = async (callback: string) => {
    if (!oauthSession) return;
    setBusyAction("oauth-complete");
    appendUiLog(`提交 OAuth 回调并导入 sub2api：${oauthSession.email}`);
    try {
      const data = await completeAccountOAuth(oauthSession.job_id, callback);
      setPool(data.oauth.account_pool);
      setOauthSession(null);
      if (data.oauth.warehouse.ok) {
        toast.success("OAuth 已获取，sub2api 已更新并通过测活");
      } else {
        toast.warning("OAuth 已获取，账号已成为成品账号；sub2api 导入或测活未通过");
      }
      await load(true);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "OAuth 换码失败");
    } finally {
      setBusyAction("");
    }
  };

  const doDelete = async (selector: string) => {
    setBusyAction("delete");
    try {
      const data = await deleteAccount(selector);
      setPool(data.account_pool);
      setSelectedAccountId("");
    } finally {
      setBusyAction("");
    }
  };

  const doClear = async () => {
    setBusyAction("clear");
    try {
      const data = await clearAccountPool();
      setPool(data.account_pool);
      setSelectedAccountId("");
    } finally {
      setBusyAction("");
    }
  };

  const statCards = [
    ["总库存", n(stats.total)],
    ["成品账号", n(stats.finished)],
    ["OAuth Token 可用", n(stats.oauth_capable)],
    ["待获取 OAuth", n(stats.needs_oauth)],
    ["sub2api 可用", n(stats.sub2api_healthy)],
    ["不可用", n(stats.unavailable)],
  ];

  return (
    <>
      <Card className="overflow-hidden">
        <CardHeader className="gap-3 border-b border-slate-200 p-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <CardTitle className="text-lg text-slate-950">成品账号库存</CardTitle>
              <CardDescription className="text-slate-500">成品账号需具备可用 OAuth Token，或已在 sub2api 中通过测活；未达标记录保留为待认证状态。</CardDescription>
            </div>
            <Button variant="outline" className="h-9 rounded-xl cursor-pointer hover:bg-slate-50" onClick={() => void load()} disabled={loading || saving}>
              <RefreshCw className="size-4" />刷新
            </Button>
          </div>
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-6">
            {statCards.map(([label, value]) => (
              <div key={label} className="rounded-2xl border border-slate-200 bg-slate-50 px-3 py-2">
                <div className="text-xs text-slate-500">{label}</div>
                <div className="mt-1 text-xl font-semibold text-slate-950">{String(value)}</div>
              </div>
            ))}
          </div>
        </CardHeader>

        <CardContent className="space-y-4 p-4">
          <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_420px]">
            <div className="space-y-3">
              <div className="space-y-2 rounded-xl border border-slate-200 bg-slate-50 p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="text-sm font-semibold text-slate-800">导入账号 / sub2api JSON</div>
                  <div className="flex flex-wrap gap-3 text-xs text-slate-600">
                    <label className="inline-flex items-center gap-1 cursor-pointer"><input type="checkbox" checked={autoCheckOnImport} onChange={(e) => setAutoCheckOnImport(e.target.checked)} /> 导入后自动测活</label>
                    <label className="inline-flex items-center gap-1 cursor-pointer"><input type="checkbox" checked={dropBannedOnImport} onChange={(e) => setDropBannedOnImport(e.target.checked)} /> 剔除不可用</label>
                  </div>
                </div>
                <Textarea value={importText} onChange={(e) => setImportText(e.target.value)} placeholder="支持 sub2api-data JSON / sub2api 后台账号 JSON / email----password----refresh_token----access_token----id_token" className="min-h-28 rounded-xl font-mono text-xs" disabled={saving} />
                <div className="flex flex-wrap items-center gap-2">
                  <select value={healthMethod} onChange={(e) => setHealthMethod(e.target.value)} className="h-9 rounded-xl border border-slate-200 bg-white px-3 text-sm">
                    <option value="auto">自动测活</option>
                    <option value="sub2api">sub2api 测活</option>
                    <option value="local">本地 Token 测活/额度</option>
                  </select>
                  <label className="inline-flex h-9 items-center gap-2 rounded-xl border border-slate-200 bg-white px-3 text-sm text-slate-700">
                    <span>并发</span>
                    <input
                      type="number"
                      min={1}
                      max={64}
                      value={healthConcurrency}
                      onChange={(e) => setHealthConcurrency(Math.max(1, Math.min(64, Number(e.target.value) || 1)))}
                      className="h-7 w-16 rounded-lg border border-slate-200 px-2 text-sm outline-none"
                      disabled={saving}
                    />
                  </label>
                  <Button className="h-9 rounded-xl bg-emerald-600 text-white cursor-pointer" onClick={() => void doImport()} disabled={saving || !importText.trim()}>
                    {importing ? <LoaderCircle className="size-4 animate-spin" /> : null}导入文本
                  </Button>
                  <label className={`inline-flex h-9 items-center gap-2 rounded-xl border border-emerald-200 bg-white px-3 text-sm font-semibold text-emerald-700 transition hover:bg-emerald-50 ${saving ? "cursor-not-allowed opacity-60" : "cursor-pointer"}`}>
                    {busyAction === "file-import" ? <LoaderCircle className="size-4 animate-spin" /> : <Upload className="size-4" />}批量导入文件
                    <input type="file" multiple accept=".json,.txt,.csv,.log" className="hidden" onChange={(event) => void doImportFiles(event)} disabled={saving} />
                  </label>
                  <Button variant="outline" className="h-9 rounded-xl border-rose-200 bg-rose-50 text-rose-700 cursor-pointer" onClick={() => void doClear()} disabled={saving || !pool?.accounts.length}>清空库存</Button>
                </div>
              </div>

              <div className="flex flex-wrap gap-2 rounded-xl border border-emerald-200 bg-emerald-50 p-3">
                {selectedSelectors.length ? (
                  <Button className="h-9 rounded-xl bg-emerald-700 text-white" onClick={() => void doCheck(selectedSelectors, true)} disabled={saving}><Activity className="size-4" />测选中+额度（{selectedSelectors.length}）</Button>
                ) : null}
                <Button className="h-9 rounded-xl bg-emerald-600 text-white" onClick={() => void doCheck(filteredSelectors, true)} disabled={saving || !filteredSelectors.length}>{busyAction === "check" ? <LoaderCircle className="size-4 animate-spin" /> : <Activity className="size-4" />}测当前筛选+额度（{filteredSelectors.length}）</Button>
                <Button variant="outline" className="h-9 rounded-xl bg-white cursor-pointer hover:bg-slate-50" onClick={() => void doCheck([], true)} disabled={saving || !pool?.accounts.length}>全部测活+额度</Button>
                <Button variant="outline" className="h-9 rounded-xl bg-white cursor-pointer hover:bg-slate-50" onClick={() => void doStartBackground()} disabled={saving || inventory?.enabled || !pool?.accounts.length}>{busyAction === "background" ? <LoaderCircle className="size-4 animate-spin" /> : <Zap className="size-4" />}后台测活</Button>
                <Button variant="outline" className="h-9 rounded-xl bg-white cursor-pointer hover:bg-slate-50" onClick={() => void doStopBackground()} disabled={!inventory?.enabled}>停止</Button>
                <Button variant="outline" className="h-9 rounded-xl bg-white cursor-pointer hover:bg-slate-50" onClick={() => void toggleSchedule()} disabled={saving}><Clock3 className="size-4" />{inventory?.settings.scheduled_enabled ? "关闭定时" : "开启定时"}</Button>
                {selectedSelectors.length ? (
                  <Button variant="outline" className="h-9 rounded-xl bg-white cursor-pointer hover:bg-slate-50" onClick={() => void doQuota(selectedSelectors)} disabled={saving}>同步选中额度（{selectedSelectors.length}）</Button>
                ) : null}
                <Button variant="outline" className="h-9 rounded-xl bg-white cursor-pointer hover:bg-slate-50" onClick={() => void doQuota(filteredSelectors)} disabled={saving || !filteredSelectors.length}>{busyAction === "quota" ? <LoaderCircle className="size-4 animate-spin" /> : null}同步筛选额度（{filteredSelectors.length}）</Button>
                <Button variant="outline" className="h-9 rounded-xl bg-white cursor-pointer hover:bg-slate-50" onClick={() => void doQuota([])} disabled={saving || !pool?.accounts.length}>同步全部</Button>
                {selectedSelectors.length ? (
                  <Button variant="outline" className="h-9 rounded-xl border-indigo-200 bg-white text-indigo-700 cursor-pointer hover:bg-indigo-50" onClick={() => void doRefreshTypes(selectedSelectors)} disabled={saving}>{busyAction === "type-refresh" ? <LoaderCircle className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}刷新选中类型（{selectedSelectors.length}）</Button>
                ) : null}
                <Button variant="outline" className="h-9 rounded-xl border-indigo-200 bg-white text-indigo-700 cursor-pointer hover:bg-indigo-50" onClick={() => void doRefreshTypes(filteredSelectors)} disabled={saving || !filteredSelectors.length}>{busyAction === "type-refresh" ? <LoaderCircle className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}刷新筛选类型（{filteredSelectors.length}）</Button>
                <Button variant="outline" className="h-9 rounded-xl border-indigo-200 bg-white text-indigo-700 cursor-pointer hover:bg-indigo-50" onClick={() => void doRefreshTypes([])} disabled={saving || !pool?.accounts.length}>一键刷新全部类型</Button>
                <Button variant="outline" className="h-9 rounded-xl border-rose-200 bg-white text-rose-700 cursor-pointer hover:bg-rose-50/50" onClick={() => void doPrune()} disabled={saving || !pool?.accounts.length}><ShieldAlert className="size-4" />剔除不可用</Button>
              </div>

              <div className="flex flex-wrap gap-2 rounded-xl border border-sky-200 bg-sky-50 p-3">
                {selectedOAuthSelectors.length ? (
                  <Button className="h-9 rounded-xl bg-sky-700 text-white cursor-pointer hover:bg-sky-800" onClick={() => void doSub2ApiImport(selectedOAuthSelectors)} disabled={saving}><Send className="size-4" />导入选中 OAuth（{selectedOAuthSelectors.length}）</Button>
                ) : null}
                <Button className="h-9 rounded-xl bg-sky-600 text-white cursor-pointer hover:bg-sky-700" onClick={() => void doSub2ApiImport(filteredOAuthSelectors)} disabled={saving || !filteredOAuthSelectors.length}><Send className="size-4" />导入筛选 OAuth（{filteredOAuthSelectors.length}）</Button>
                <Button variant="outline" className="h-9 rounded-xl bg-white cursor-pointer hover:bg-slate-50" onClick={() => void doSub2ApiImport([])} disabled={saving || !n(stats.oauth_capable)}>导入全部 OAuth</Button>
                {selectedOAuthSelectors.length ? (
                  <Button variant="outline" className="h-9 rounded-xl bg-white cursor-pointer hover:bg-slate-50" onClick={() => void doExportSub2Api(selectedOAuthSelectors)} disabled={saving}>导出选中 OAuth JSON</Button>
                ) : null}
                <Button variant="outline" className="h-9 rounded-xl bg-white cursor-pointer hover:bg-slate-50" onClick={() => void doSub2ApiImport(selected ? [accountSelector(selected)] : [])} disabled={saving || !selected?.oauth_capable}>导入当前 OAuth 账号</Button>
                <Button variant="outline" className="h-9 rounded-xl bg-white cursor-pointer hover:bg-slate-50" onClick={() => void doExportSub2Api([])} disabled={saving || !n(stats.oauth_capable)}><Download className="size-4" />导出全部 OAuth JSON</Button>
                <Button variant="outline" className="h-9 rounded-xl bg-white cursor-pointer hover:bg-slate-50" onClick={() => void doExportSub2Api(selected ? [accountSelector(selected)] : [])} disabled={saving || !selected?.oauth_capable}>导出当前 OAuth 账号</Button>
              </div>

              <div className="flex flex-wrap gap-2 rounded-xl border border-amber-200 bg-amber-50 p-3">
                {selectedSelectors.length ? (
                  <Button className="h-9 rounded-xl bg-amber-700 text-white cursor-pointer hover:bg-amber-800" onClick={() => void doCpaImport(selectedSelectors)} disabled={saving}><Send className="size-4" />导入选中 CPA（{selectedSelectors.length}）</Button>
                ) : null}
                <Button className="h-9 rounded-xl bg-amber-600 text-white cursor-pointer hover:bg-amber-700" onClick={() => void doCpaImport(filteredSelectors)} disabled={saving || !filteredSelectors.length}><Send className="size-4" />导入当前筛选 CPA（{filteredSelectors.length}）</Button>
                <Button variant="outline" className="h-9 rounded-xl border-amber-200 bg-white text-amber-800 cursor-pointer hover:bg-amber-50" onClick={() => void doCpaImport([])} disabled={saving || !pool?.accounts.length}>导入全部 CPA</Button>
                <Button variant="outline" className="h-9 rounded-xl border-amber-200 bg-white text-amber-800 cursor-pointer hover:bg-amber-50" onClick={() => void doCpaImport(selected ? [accountSelector(selected)] : [])} disabled={saving || !selected}>导入当前详情账号</Button>
              </div>
            </div>

            <div className="rounded-xl border border-slate-200 bg-slate-950 p-3 text-white">
              <div className="mb-2 flex items-center justify-between">
                <div>
                  <div className="text-sm font-semibold">测活 / 额度执行日志</div>
                  <div className="text-xs text-slate-400 font-medium">测活、额度同步、后台/定时任务都会写入这里</div>
                </div>
                <Badge variant={inventory?.enabled ? "success" : "secondary"} className="rounded-md">{inventory?.enabled ? "运行中" : "空闲"}</Badge>
              </div>
              <div className="max-h-80 space-y-1 overflow-auto rounded-lg border border-white/10 bg-black/20 p-2 font-mono text-xs">
                {!logs.length ? <div className="py-8 text-center text-slate-400">暂无执行日志</div> : logs.map((log, index) => (
                  <div key={`${log.time}-${index}`} className={log.level === "red" ? "text-rose-300" : log.level === "green" ? "text-emerald-300" : log.level === "yellow" ? "text-amber-300" : "text-slate-200"}>
                    <span className="text-slate-500">[{formatTime(log.time)}]</span> {log.text}
                  </div>
                ))}
              </div>
            </div>
          </div>

          <div className="space-y-2 rounded-xl border border-slate-200 bg-white p-3">
            <div className="grid gap-2 lg:grid-cols-4 xl:grid-cols-[minmax(240px,1fr)_130px_135px_150px_120px_120px_100px]">
              <Input value={query} onChange={(e) => { setQuery(e.target.value); setPage(1); }} placeholder="搜索邮箱/RT 来源/OAuth/测活/sub2api ID" className="h-9 rounded-xl" />
              <select value={statusFilter} onChange={(e) => { setStatusFilter(e.target.value); setPage(1); }} className="h-9 rounded-xl border border-slate-200 bg-white px-3 text-sm">
                <option value="all">全部状态</option><option value="active">正常</option><option value="disabled">禁用</option><option value="banned">封禁</option><option value="invalid">无效</option>
              </select>
              <select value={healthFilter} onChange={(e) => { setHealthFilter(e.target.value); setPage(1); }} className="h-9 rounded-xl border border-slate-200 bg-white px-3 text-sm">
                <option value="all">全部测活</option>
                <option value="ok">存活</option>
                <option value="failed_group">全部异常</option>
                <option value="token_group">Token 异常</option>
                <option value="rt_group">RT 失效/需重登</option>
                <option value="network_group">网络/代理</option>
                <option value="blocked_group">拦截/限流</option>
                <option value="missing_token">缺 Token</option>
                <option value="token_expired">Token 过期</option>
                <option value="token_invalid">Token 无效</option>
                <option value="refresh_token_missing">缺 RT</option>
                <option value="refresh_token_invalid">RT 失效</option>
                <option value="refresh_token_expired">RT 过期</option>
                <option value="refresh_token_refresh_failed">RT 刷新失败</option>
                <option value="cloudflare_blocked">Cloudflare</option>
                <option value="region_blocked">地区受限</option>
                <option value="rate_limited">限流</option>
                <option value="proxy_error">代理错误</option>
                <option value="timeout">超时</option>
                <option value="network_error">网络错误</option>
                <option value="banned">封禁</option>
                <option value="unknown">未知</option>
              </select>
              <select value={typeFilter} onChange={(e) => { setTypeFilter(e.target.value); setPage(1); }} className="h-9 rounded-xl border border-slate-200 bg-white px-3 text-sm">
                <option value="all">全部类型</option><option value="free">Free</option><option value="plus">Plus</option><option value="pro">Pro</option><option value="k12">K12</option><option value="team">团队</option>
              </select>
              <select value={readinessFilter} onChange={(e) => { setReadinessFilter(e.target.value); setPage(1); }} className="h-9 rounded-xl border border-slate-200 bg-white px-3 text-sm">
                <option value="all">全部成品状态</option>
                <option value="finished">仅成品账号</option>
                <option value="needs_oauth">待获取 OAuth</option>
                <option value="sub2api_healthy">sub2api 可用</option>
                <option value="oauth_sub2api">sub2api OAuth RT</option>
                <option value="oauth_inventory">本地库存 RT（不兼容）</option>
                <option value="incompatible_refresh">非 sub2api OAuth RT</option>
                <option value="invalid_refresh">OAuth RT 失效</option>
                <option value="web_session_only">仅网页会话</option>
                <option value="warehouse_failed">仓库导入失败</option>
              </select>
              <select value={groupFilter} onChange={(e) => { setGroupFilter(e.target.value); setPage(1); }} className="h-9 rounded-xl border border-slate-200 bg-white px-3 text-sm">
                <option value="all">全部分组</option>{groupOptions.map((group) => <option key={group} value={group}>{group}</option>)}
              </select>
              <select value={pageSize} onChange={(e) => { setPageSize(Number(e.target.value)); setPage(1); }} className="h-9 rounded-xl border border-slate-200 bg-white px-3 text-sm">
                <option value={10}>10/页</option><option value={20}>20/页</option><option value={50}>50/页</option><option value={200}>200/页</option><option value={500}>500/页</option><option value={1000}>1000/页</option><option value={0}>全部</option>
              </select>
            </div>
            <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-600">
              <span>当前筛选 {filtered.length} 个，本页 {pageItems.length} 个，已选 {selectedSelectors.length} 个</span>
              <div className="flex flex-wrap gap-2">
                <Button variant="outline" className="h-8 rounded-lg bg-white px-2.5 text-xs cursor-pointer" onClick={selectCurrentPage} disabled={saving || !pageItems.length}>选中本页</Button>
                <Button variant="outline" className="h-8 rounded-lg bg-white px-2.5 text-xs cursor-pointer" onClick={() => setSelectedAccountIds(filteredSelectors)} disabled={saving || !filteredSelectors.length}>选中筛选</Button>
                <Button variant="outline" className="h-8 rounded-lg bg-white px-2.5 text-xs cursor-pointer" onClick={() => setSelectedAccountIds([])} disabled={saving || !selectedSelectors.length}>清空选择</Button>
              </div>
            </div>

            <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-[0_12px_34px_-30px_rgba(15,23,42,0.45)]">
              <div className="max-h-[700px] overflow-auto">
                <table className="w-full min-w-[1760px] table-fixed border-separate border-spacing-0 text-left text-sm">
                  <colgroup>
                    <col style={{ width: "52px" }} />
                    <col style={{ width: "430px" }} />
                    <col style={{ width: "130px" }} />
                    <col style={{ width: "260px" }} />
                    <col style={{ width: "175px" }} />
                    <col style={{ width: "130px" }} />
                    <col style={{ width: "145px" }} />
                    <col style={{ width: "438px" }} />
                  </colgroup>
                  <thead className="sticky top-0 z-10 bg-slate-50 text-xs font-semibold uppercase tracking-wide text-slate-500">
                    <tr>
                      {[
                        { label: "选" },
                        { label: "账号", sort: "email" as AccountSortKey },
                        { label: "套餐", sort: "plan" as AccountSortKey },
                        { label: "Token" },
                        { label: "创建时间", sort: "created_at" as AccountSortKey },
                        { label: "完整 JSON" },
                        { label: "有效期", sort: "expires_at" as AccountSortKey },
                        { label: "操作" },
                      ].map((column) => (
                        <th key={column.label} className="border-r border-b border-slate-200 px-3 py-3 last:border-r-0">
                          {column.sort ? (
                            <button type="button" className="inline-flex items-center gap-1.5 hover:text-slate-900" onClick={() => toggleSort(column.sort!)}>
                              {column.label}{sortIcon(column.sort)}
                            </button>
                          ) : column.label}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="bg-white">
                    {loading ? (
                      <tr>
                        <td colSpan={8} className="p-4">
                          <div className="space-y-2">
                            {[0, 1, 2, 3].map((item) => (
                              <div key={item} className="grid grid-cols-[52px_430px_130px_260px_175px_130px_145px_438px] overflow-hidden rounded-xl border border-slate-100 bg-white">
                                {[0, 1, 2, 3, 4, 5, 6, 7].map((cell) => (
                                  <div key={cell} className="border-r border-slate-100 px-3 py-3 last:border-r-0">
                                    <div className={`h-4 animate-pulse rounded-full ${cell === 1 ? "bg-slate-200" : "bg-slate-100"}`} />
                                  </div>
                                ))}
                              </div>
                            ))}
                          </div>
                        </td>
                      </tr>
                    ) : !pageItems.length ? (
                      <tr><td colSpan={8} className="p-10 text-center text-sm text-slate-500">暂无账号</td></tr>
                    ) : pageItems.map((i) => {
                      const percent = quotaPercent(i);
                      const selector = accountSelector(i);
                      const isSelected = selectedSet.has(selector);
                      const rtWarning = rtWarningText(i);
                      const expiresAt = tokenExpiresAtMs(i);
                      const healthStatus = i.health_status || i.sub2api_health_status;
                      const healthKey = String(healthStatus || "unknown").toLowerCase();
                      const accountStatus = String(i.status || "active").toLowerCase();
                      const isBad = failedHealthStatuses.has(healthKey) || ["banned", "dead", "invalid", "disabled"].includes(accountStatus) || Boolean(rtWarning);
                      const rowTone = isSelected ? "bg-emerald-50/90" : isBad ? "bg-rose-50/75" : "bg-white";
                      const rowHover = isBad ? "hover:bg-rose-100/70" : "hover:bg-emerald-50/70";
                      const plan = typeText(i);
                      const planClass = plan === "Plus"
                        ? "border-indigo-200 bg-indigo-50 text-indigo-700"
                        : plan === "Pro"
                          ? "border-purple-200 bg-purple-50 text-purple-700"
                          : plan === "Free"
                            ? "border-slate-200 bg-slate-50 text-slate-600"
                            : "border-emerald-200 bg-emerald-50 text-emerald-700";
                      return (
                        <tr
                          key={selector}
                          role="button"
                          tabIndex={0}
                          onClick={() => setSelectedAccountId(selector)}
                          onKeyDown={(event) => {
                            if (event.key === "Enter" || event.key === " ") {
                              event.preventDefault();
                              setSelectedAccountId(selector);
                            }
                          }}
                          className={`align-top transition ${rowTone} ${rowHover} ${isSelected ? "ring-1 ring-inset ring-emerald-200" : ""}`}
                        >
                          <td className={`border-r border-b border-slate-100 px-3 py-3 ${isBad ? "border-l-4 border-l-rose-500" : isSelected ? "border-l-4 border-l-emerald-500" : "border-l-4 border-l-transparent"}`} onClick={(event) => event.stopPropagation()}>
                            <input
                              type="checkbox"
                              aria-label={`选择 ${i.email}`}
                              checked={isSelected}
                              onChange={() => toggleRowSelection(selector)}
                              className="size-4 rounded border-slate-300 text-emerald-600"
                            />
                          </td>
                          <td className="min-w-0 border-r border-b border-slate-100 px-3 py-3">
                            <div className="space-y-1.5">
                              <div className="truncate font-mono text-xs font-semibold text-slate-950" title={i.email}>{i.email}</div>
                              <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-slate-500">
                                <Badge variant="secondary" className="rounded-md">{mailboxGroupLabel(i)}</Badge>
                                <Badge variant={healthVariant(healthStatus)} className="rounded-md" title={i.health_error || i.last_quota_error || ""}>{healthText(healthStatus)}</Badge>
                                <span className="rounded-md bg-slate-100 px-1.5 py-0.5 font-mono">{statusText(i.status)}</span>
                                <Badge variant={productStateVariant(i)} className="rounded-md">{productStateText(i)}</Badge>
                                {i.chatgpt_account_id ? <span className="font-mono">空间:{String(i.chatgpt_account_id).slice(0, 10)}…</span> : null}
                                {i.sub2api_account_id ? <span className="font-mono">sub2api:{i.sub2api_account_id}</span> : null}
                                {i.cpa_auth_file ? <span className="font-mono text-amber-700">CPA:{String(i.cpa_auth_file).slice(0, 18)}{String(i.cpa_auth_file).length > 18 ? "…" : ""}</span> : null}
                              </div>
                              {rtWarning ? <div className="truncate text-xs font-semibold text-amber-600" title={rtWarning}>{rtWarning}</div> : null}
                            </div>
                          </td>
                          <td className="border-r border-b border-slate-100 px-3 py-3">
                            <div className="space-y-2">
                              <Badge variant="outline" className={`rounded-md ${planClass}`}>{plan}</Badge>
                              <div className="text-[11px] font-semibold text-slate-600" title={i.last_quota_error || ""}>{quotaText(i)}</div>
                              <div className="h-1.5 overflow-hidden rounded-full bg-slate-100">
                                <span className={`block h-full rounded-full ${percent === null ? "bg-slate-300" : percent > 85 ? "bg-rose-500" : percent > 60 ? "bg-amber-500" : "bg-emerald-500"}`} style={{ width: `${percent ?? 0}%` }} />
                              </div>
                            </div>
                          </td>
                          <td className="border-r border-b border-slate-100 px-3 py-3">
                            <div className="space-y-1.5 text-xs">
                              <Badge variant={i.access_token || i.has_access_token ? "success" : "warning"} className="rounded-md">{tokenStateText(i)}</Badge>
                              <div className={`text-[11px] font-semibold ${i.oauth_capable ? "text-emerald-700" : "text-amber-700"}`}>{oauthStateText(i)}</div>
                              <div className="truncate text-[11px] text-slate-500" title={i.refresh_token_label || ""}>{i.refresh_token_label || "RT 来源未知"}</div>
                              <div className="truncate font-mono text-[11px] text-slate-600" title={i.access_token || i.access_token_preview || ""}>AT: {shortToken(i.access_token, i.access_token_preview)}</div>
                              <div className="truncate font-mono text-[11px] text-slate-500" title={i.refresh_token || i.refresh_token_preview || ""}>RT: {shortToken(i.refresh_token, i.refresh_token_preview)}</div>
                            </div>
                          </td>
                          <td className="border-r border-b border-slate-100 px-3 py-3 text-xs text-slate-600">
                            <div>{formatTime(i.created_at)}</div>
                            <div className="mt-1 text-[11px] text-slate-400">更新 {formatTime(i.updated_at)}</div>
                            <div className="mt-1 font-mono text-[11px] text-slate-400">HTTP {i.health_code || i.sub2api_health_code || "-"}</div>
                          </td>
                          <td className="border-r border-b border-slate-100 px-3 py-3" onClick={(event) => event.stopPropagation()}>
                            <div className="flex flex-wrap gap-1.5">
                              <Button variant="outline" className="h-7 rounded-lg bg-white px-2 text-xs" onClick={() => void copyAccountJson(i)} disabled={saving}><Copy className="size-3" />复制</Button>
                              <Button variant="outline" className="h-7 rounded-lg bg-white px-2 text-xs" onClick={() => downloadAccountJson(i)} disabled={saving}><Download className="size-3" />下载</Button>
                            </div>
                          </td>
                          <td className="border-r border-b border-slate-100 px-3 py-3 text-xs text-slate-600">
                            <Badge variant={tokenExpiryVariant(i)} className="rounded-md">{tokenExpiryText(i)}</Badge>
                            <div className="mt-1 font-mono text-[11px] text-slate-400" title={expiresAt ? formatTime(expiresAt) : ""}>{expiresAt ? formatTime(expiresAt) : "未知"}</div>
                          </td>
                          <td className="border-b border-slate-100 px-3 py-3" onClick={(event) => event.stopPropagation()}>
                            <div className="flex flex-wrap gap-1.5">
                              <Button variant="outline" className="h-7 rounded-lg bg-white px-2 text-xs" onClick={() => setSelectedAccountId(selector)} disabled={saving}>查看</Button>
                              <Button variant="outline" className="h-7 rounded-lg bg-white px-2 text-xs" onClick={() => void copyText(i.access_token || "", "Access Token")} disabled={saving || !i.access_token}>复制 AT</Button>
                              <Button variant="outline" className="h-7 rounded-lg border-blue-200 bg-white px-2 text-xs text-blue-700 hover:bg-blue-50" onClick={() => void doRefreshToken(selector)} disabled={saving || !(i.refresh_token || i.has_refresh_token)}>{busyAction === "refresh-token" ? <LoaderCircle className="size-3 animate-spin" /> : null}刷新 token</Button>
                              <Button className="h-7 bg-emerald-600 px-2 text-xs text-white hover:bg-emerald-700" onClick={() => void startOAuth(i)} disabled={saving} title={i.registration_proxy?.node_name || i.registration_proxy_node_id ? "使用注册时记录的节点立即执行自动 OAuth" : "打开人工 OAuth 授权页"}>{busyAction === "oauth-start" ? <LoaderCircle className="size-3 animate-spin" /> : <KeyRound className="size-3" />}{i.registration_proxy?.node_name || i.registration_proxy_node_id ? "同节点 OAuth" : "打开授权"}</Button>
                              <Button variant="outline" className="h-7 rounded-lg bg-white px-2 text-xs" onClick={() => void doCheck([selector], false)} disabled={saving}>检测存活</Button>
                              <Button variant="outline" className="h-7 rounded-lg border-indigo-200 bg-white px-2 text-xs text-indigo-700 hover:bg-indigo-50" onClick={() => void doRefreshTypes([selector])} disabled={saving}>{busyAction === "type-refresh" ? <LoaderCircle className="size-3 animate-spin" /> : null}刷新类型</Button>
                              <Button variant="outline" className="h-7 rounded-lg border-sky-200 bg-white px-2 text-xs text-sky-700 hover:bg-sky-50" onClick={() => void doSub2ApiImport([selector])} disabled={saving || !i.oauth_capable} title={i.oauth_capable ? "导入 sub2api" : "需要先获取 sub2api OAuth"}><Send className="size-3" />导入 sub2api</Button>
                              <Button variant="outline" className="h-7 rounded-lg border-amber-200 bg-white px-2 text-xs text-amber-700 hover:bg-amber-50" onClick={() => void doCpaImport([selector])} disabled={saving}><Send className="size-3" />导入 CPA</Button>
                              <Button variant="outline" className="h-7 rounded-lg border-rose-200 bg-rose-50 px-2 text-xs text-rose-700 hover:bg-rose-100" onClick={() => void doDelete(selector)} disabled={saving}><Trash2 className="size-3" />删除</Button>
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>

            <div className="flex items-center justify-between text-xs text-slate-500 pt-1">
              <span>共 {filtered.length} 个，{safePage} / {totalPages} 页；单击账号查看详情</span>
              <div className="flex gap-2">
                <Button variant="outline" className="h-8 rounded-lg cursor-pointer" onClick={() => setPage((v) => Math.max(1, v - 1))} disabled={page <= 1}>上一页</Button>
                <Button variant="outline" className="h-8 rounded-lg cursor-pointer" onClick={() => setPage((v) => Math.min(totalPages, v + 1))} disabled={page >= totalPages}>下一页</Button>
              </div>
            </div>
          </div>
        </CardContent>
      </Card>

      {selected ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/45 p-4 backdrop-blur-xs">
          <div className="max-h-[88vh] w-full max-w-3xl overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-2xl">
            <div className="flex items-center justify-between border-b border-slate-200 bg-slate-50 px-5 py-4">
              <div>
                <div className="text-sm font-semibold text-slate-950">账号详情</div>
                <div className="mt-1 truncate font-mono text-xs text-slate-500">{selected.email}</div>
              </div>
              <Button variant="outline" className="h-9 rounded-xl cursor-pointer" onClick={() => setSelectedAccountId("")}><X className="size-4" />关闭</Button>
            </div>
            <div className="max-h-[calc(88vh-76px)] space-y-4 overflow-auto p-5">
              <div className="flex flex-wrap gap-2">
                <Button variant="outline" className="h-8 rounded-lg px-2.5 text-xs cursor-pointer hover:bg-slate-50" onClick={() => void doCheck([accountSelector(selected)], true)} disabled={saving}>测活+额度</Button>
                <Button variant="outline" className="h-8 rounded-lg px-2.5 text-xs cursor-pointer hover:bg-slate-50" onClick={() => void doQuota([accountSelector(selected)])} disabled={saving}>仅额度</Button>
                <Button variant="outline" className="h-8 rounded-lg px-2.5 text-xs cursor-pointer hover:bg-indigo-50 text-indigo-700 border-indigo-200" onClick={() => void doRefreshTypes([accountSelector(selected)])} disabled={saving}>{busyAction === "type-refresh" ? <LoaderCircle className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}刷新类型</Button>
                <Button variant="outline" className="h-8 rounded-lg px-2.5 text-xs cursor-pointer hover:bg-emerald-50 text-emerald-700 border-emerald-200" onClick={async () => {
                  try {
                    await updateAccountApi(accountSelector(selected), { plan_type: "plus", account_type: "plus" });
                    toast.success("已标记为 Plus");
                    await load(true);
                  } catch { toast.error("更新失败"); }
                }} disabled={saving}>标记为 Plus</Button>
                <Button variant="outline" className="h-8 rounded-lg px-2.5 text-xs cursor-pointer hover:bg-blue-50 text-blue-700 border-blue-200" onClick={() => void doRefreshToken(accountSelector(selected))} disabled={saving || !(selected.refresh_token || selected.has_refresh_token)}>RT刷新Token</Button>
                <Button className="h-8 bg-emerald-600 px-2.5 text-xs text-white hover:bg-emerald-700" onClick={() => void startOAuth(selected)} disabled={saving}><KeyRound className="size-3.5" />获取 OAuth 并导入</Button>
                <Button variant="outline" className="h-8 rounded-lg px-2.5 text-xs cursor-pointer hover:bg-slate-50" onClick={() => void doSub2ApiImport([accountSelector(selected)])} disabled={saving || !selected.oauth_capable}>导入 sub2api</Button>
                <Button variant="outline" className="h-8 rounded-lg border-amber-200 px-2.5 text-xs text-amber-700 cursor-pointer hover:bg-amber-50" onClick={() => void doCpaImport([accountSelector(selected)])} disabled={saving}>导入 CPA</Button>
                <Button variant="outline" className="h-8 rounded-lg px-2.5 text-xs cursor-pointer hover:bg-slate-50" onClick={() => void doExportSub2Api([accountSelector(selected)])} disabled={saving}>导出 JSON</Button>
                <Button variant="outline" className="h-8 rounded-lg border-rose-200 bg-rose-50 px-2.5 text-xs text-rose-700 cursor-pointer hover:bg-rose-100/50" onClick={() => void doDelete(accountSelector(selected))} disabled={saving}><Trash2 className="size-3.5" />删除</Button>
              </div>
              <div className="grid gap-3 text-xs md:grid-cols-2">
                {[
                  ["邮箱分组", mailboxGroupLabel(selected)],
                  ["账号类型", `${typeText(selected)} / ${selected.plan_type || "-"}`],
                  ["测活", `${healthText(selected.health_status || selected.sub2api_health_status)} ${selected.health_code || selected.sub2api_health_code || ""} (${selected.health_status || selected.sub2api_health_status || "unknown"})`],
                  ["额度", quotaText(selected)],
                  ["额度检查", formatTime(selected.quota_checked_at)],
                  ["额度错误", selected.last_quota_error || "-"],
                  ["RT 状态", selected.oauth_refresh_status ? `${healthText(selected.oauth_refresh_status)} (${selected.oauth_status || "-"})` : (selected.has_refresh_token ? "未检查" : "缺 RT")],
                  ["成品状态", selected.finished_account ? `成品账号（${selected.finished_reason || "oauth_token"}）` : selected.needs_oauth ? "待认证账号" : "不可用账号"],
                  ["OAuth 能力", oauthStateText(selected)],
                  ["RT 来源", selected.refresh_token_label || selected.refresh_token_kind || "未知"],
                  ["OAuth Client", selected.oauth_client_id || selected.client_id || "-"],
                  ["仓库状态", selected.warehouse_status || "not_imported"],
                  ["RT 错误", selected.oauth_refresh_error || "-"],
                  ["Access Token", selected.access_token || selected.access_token_preview || "-"],
                  ["Refresh Token", selected.refresh_token || selected.refresh_token_preview || "-"],
                  ["ChatGPT Account", selected.chatgpt_account_id || "-"],
                  ["User ID", selected.chatgpt_user_id || "-"],
                  ["库存实例ID", selected.inventory_id || "-"],
                  ["K12 Team", selected.k12_team_id || "-"],
                  ["Workspace", selected.workspace_id || "-"],
                  ["sub2api ID", selected.sub2api_account_id || "-"],
                  ["sub2api", selected.sub2api_import_status || "-"],
                  ["CPA 文件", selected.cpa_auth_file || "-"],
                  ["CPA 状态", selected.cpa_import_status || "-"],
                  ["Source", selected.source_type || "web"],
                  ["错误", selected.health_error || selected.k12_last_error || "-"],
                  ["创建时间", formatTime(selected.created_at)],
                ].map(([label, value]) => (
                  <div key={label} className="space-y-1">
                    <div className="font-semibold text-slate-500">{label}</div>
                    <div className="relative">
                      <div className="max-h-40 whitespace-pre-wrap break-all rounded-lg border border-slate-200 bg-slate-50/80 p-2 pr-8 font-mono text-slate-800">{String(value)}</div>
                      {value && String(value) !== "-" && (
                        <button
                          type="button"
                          className="absolute top-1.5 right-1.5 rounded bg-white p-1 text-slate-400 shadow-sm hover:bg-slate-100 hover:text-slate-600"
                          onClick={() => {
                            void navigator.clipboard.writeText(String(value));
                            toast.success(`${label} 已复制`);
                          }}
                          title={`复制 ${label}`}
                        >
                          <Copy className="size-3.5" />
                        </button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      ) : null}
      <AccountOAuthDialog
        session={oauthSession}
        busy={busyAction === "oauth-complete"}
        onClose={() => setOauthSession(null)}
        onComplete={completeOAuth}
      />
    </>
  );
}
