"use client";

import { useEffect, useMemo, useState } from "react";
import { Activity, Ban, Database, Gauge, Images, LoaderCircle, Play, Power, RefreshCw, Search, ServerCog, Trash2, Warehouse } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import {
  actOnCpaAccounts,
  actOnGrok2ApiAccounts,
  actOnSub2ApiAccounts,
  fetchCpaAccounts,
  fetchCpaConfig,
  fetchGrok2ApiAccounts,
  fetchGrok2ApiConfig,
  fetchSub2ApiAccounts,
  fetchSub2ApiAccountsUsage,
  fetchSub2ApiConfig,
  fetchSub2ApiGroups,
  refreshGrok2ApiAccounts,
  testCpaAccounts,
  testSub2ApiAccounts,
  updateCpaConfig,
  updateSub2ApiConfig,
  type CpaConfig,
  type Grok2ApiConfig,
  type Sub2ApiConfig,
} from "@/lib/api";

type WarehouseKind = "sub2api" | "cpa" | "grok2api";
type Row = Record<string, unknown>;

function formatTime(value: unknown) {
  if (!value) return "-";
  const date = new Date(String(value));
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function idOf(kind: WarehouseKind, item: Row) {
  return kind === "cpa" ? String(item.name || item.id || "") : String(item.id || item.account_id || "");
}

function emailOf(item: Row) {
  const credentials = record(item.credentials);
  return String(item.email || credentials.email || item.name || item.id || "-").replace(/\.json$/i, "");
}

function statusOf(kind: WarehouseKind, item: Row) {
  if (kind === "cpa") return String(item.warehouse_status || (item.disabled ? "disabled" : "active"));
  if (kind === "grok2api") {
    if (item.enabled === false) return "disabled";
    const authStatus = String(item.authStatus || "active").toLowerCase();
    return ["active", "available", "ready"].includes(authStatus) ? "active" : authStatus;
  }
  const status = String(item.status || (item.disabled ? "inactive" : "active")).toLowerCase();
  return status === "inactive" ? "disabled" : status;
}

function planOf(kind: WarehouseKind, item: Row) {
  const credentials = record(item.credentials);
  const idToken = record(item.id_token);
  const health = record(item.warehouse_health);
  const usage = record(health.usage);
  if (kind === "cpa") return String(idToken.plan_type || usage.plan_type || item.account_type || item.provider || "Codex");
  if (kind === "grok2api") return String(item.webTier || item.provider || "grok_web");
  return String(credentials.plan_type || item.plan_type || item.platform || "OpenAI");
}

function quotaOf(kind: WarehouseKind, item: Row) {
  const health = record(item.warehouse_health);
  const usage = record(health.usage);
  if (kind === "cpa") {
    const remaining = usage.primary_remaining_percent;
    const code = usage.code_review_remaining_percent;
    if (remaining !== undefined && remaining !== null) return `周 ${Number(remaining).toFixed(1)}%${code !== undefined && code !== null ? ` / 审查 ${Number(code).toFixed(1)}%` : ""}`;
    return health.status === "quota_exhausted" ? "额度耗尽" : "未读取";
  }
  if (kind === "grok2api") {
    const quota = record(item.quota);
    if (quota.remaining !== undefined && quota.limit !== undefined) return `${Number(quota.remaining).toFixed(0)} / ${Number(quota.limit).toFixed(0)}`;
    if (quota.usagePercent !== undefined) return `已用 ${Number(quota.usagePercent).toFixed(1)}%`;
    return "未读取";
  }
  const data = record(usage.data);
  const five = record(data.five_hour);
  const windowStats = record(five.window_stats);
  const extra = record(item.extra);
  if (windowStats.remaining_percent !== undefined) return `5h 剩余 ${Number(windowStats.remaining_percent).toFixed(1)}%`;
  if (extra.codex_5h_used_percent !== undefined) return `5h 已用 ${Number(extra.codex_5h_used_percent).toFixed(1)}%`;
  if (extra.codex_7d_used_percent !== undefined) return `7d 已用 ${Number(extra.codex_7d_used_percent).toFixed(1)}%`;
  return "未读取";
}

function healthOf(item: Row) {
  const health = record(item.warehouse_health);
  return String(health.status || (health.ok === true ? "ok" : health.ok === false ? "failed" : "unknown"));
}

function healthVariant(status: string): "success" | "secondary" | "danger" | "outline" | "warning" | "info" {
  if (["ok", "active", "healthy"].includes(status)) return "success";
  if (["dead", "failed", "disabled"].includes(status)) return "danger";
  if (["quota_low", "quota_exhausted"].includes(status)) return "warning";
  return "outline";
}

export function WarehouseManagerCard() {
  const [active, setActive] = useState<WarehouseKind>("sub2api");
  const [subConfig, setSubConfig] = useState<Sub2ApiConfig | null>(null);
  const [cpaConfig, setCpaConfig] = useState<CpaConfig | null>(null);
  const [grok2apiConfig, setGrok2ApiConfig] = useState<Grok2ApiConfig | null>(null);
  const [groups, setGroups] = useState<Row[]>([]);
  const [items, setItems] = useState<Row[]>([]);
  const [stats, setStats] = useState<Record<string, number>>({});
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("all");
  const [groupId, setGroupId] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [total, setTotal] = useState(0);
  const [selected, setSelected] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [lastResult, setLastResult] = useState<Record<string, unknown> | null>(null);

  const config = active === "sub2api" ? subConfig : active === "cpa" ? cpaConfig : grok2apiConfig;
  const enabled = Boolean(config?.enabled);
  const pageIds = useMemo(() => items.map((item) => idOf(active, item)).filter(Boolean), [active, items]);
  const allPageSelected = pageIds.length > 0 && pageIds.every((id) => selected.includes(id));
  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  const loadConfigs = async () => {
    const [subData, cpaData, grok2apiData] = await Promise.all([fetchSub2ApiConfig(), fetchCpaConfig(), fetchGrok2ApiConfig()]);
    setSubConfig(subData.sub2api_config);
    setCpaConfig(cpaData.cpa_config);
    setGrok2ApiConfig(grok2apiData.grok2api_config);
    return { sub2api: subData.sub2api_config, cpa: cpaData.cpa_config, grok2api: grok2apiData.grok2api_config };
  };

  const loadGroups = async () => {
    try {
      const data = await fetchSub2ApiGroups();
      setGroups((data.sub2api_groups.items || []) as Row[]);
    } catch {
      setGroups([]);
    }
  };

  const loadAccounts = async (
    nextPage = page,
    kind = active,
    filters: { search?: string; status?: string; groupId?: string; pageSize?: number } = {},
  ) => {
    const querySearch = filters.search ?? search;
    const queryStatus = filters.status ?? status;
    const queryGroupId = filters.groupId ?? groupId;
    const queryPageSize = filters.pageSize ?? pageSize;
    setLoading(true);
    try {
      if (kind === "sub2api") {
        const data = await fetchSub2ApiAccounts({ page: nextPage, pageSize: queryPageSize, groupId: queryGroupId, search: querySearch, status: queryStatus });
        setItems((data.sub2api_accounts.items || []) as Row[]);
        setTotal(Number(data.sub2api_accounts.total || 0));
        setStats({ total: Number(data.sub2api_accounts.total || 0) });
        setPage(Number(data.sub2api_accounts.page || nextPage));
      } else if (kind === "cpa") {
        const data = await fetchCpaAccounts({ page: nextPage, pageSize: queryPageSize, search: querySearch, status: queryStatus });
        setItems((data.cpa_accounts.items || []) as Row[]);
        setTotal(Number(data.cpa_accounts.total || 0));
        setStats(data.cpa_accounts.stats || {});
        setPage(Number(data.cpa_accounts.page || nextPage));
      } else {
        const data = await fetchGrok2ApiAccounts({ page: nextPage, pageSize: queryPageSize, search: querySearch, status: queryStatus });
        setItems((data.grok2api_accounts.items || []) as Row[]);
        setTotal(Number(data.grok2api_accounts.total || 0));
        setStats({ total: Number(data.grok2api_accounts.total || 0) });
        setPage(Number(data.grok2api_accounts.page || nextPage));
      }
      setSelected([]);
    } catch (error) {
      setItems([]);
      setTotal(0);
      toast.error(error instanceof Error ? error.message : "读取仓库账号失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void (async () => {
      setLoading(true);
      try {
        const configs = await loadConfigs();
        const initialKind: WarehouseKind = configs.sub2api.enabled ? "sub2api" : configs.grok2api.enabled ? "grok2api" : "cpa";
        setActive(initialKind);
        if (configs[initialKind].enabled) {
          if (initialKind === "sub2api") await loadGroups();
          await loadAccounts(1, initialKind);
        }
      } catch (error) {
        toast.error(error instanceof Error ? error.message : "加载仓库管理失败");
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const switchWarehouse = async (kind: WarehouseKind) => {
    setActive(kind);
    setPage(1);
    setSearch("");
    setStatus("all");
    setGroupId("");
    setSelected([]);
    setLastResult(null);
    const nextConfig = kind === "sub2api" ? subConfig : kind === "cpa" ? cpaConfig : grok2apiConfig;
    if (!nextConfig?.enabled) {
      setItems([]);
      setTotal(0);
      setLoading(false);
      return;
    }
    if (kind === "sub2api" && !groups.length) await loadGroups();
    await loadAccounts(1, kind, { search: "", status: "all", groupId: "" });
  };

  const togglePage = () => {
    setSelected((current) => allPageSelected ? current.filter((id) => !pageIds.includes(id)) : Array.from(new Set([...current, ...pageIds])));
  };

  const toggleOne = (id: string) => setSelected((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id]);

  const runTest = async (ids = selected) => {
    setBusy("test");
    try {
      if (active === "sub2api") {
        const data = await testSub2ApiAccounts({ account_ids: ids, concurrency: subConfig?.test_concurrency || 5 });
        setLastResult(data.sub2api_test);
        toast.success(`sub2api 测活完成：成功 ${Number(data.sub2api_test.success || 0)}，失败 ${Number(data.sub2api_test.fail || 0)}`);
      } else if (active === "cpa") {
        const data = await testCpaAccounts(ids);
        setLastResult(data.cpa_test);
        toast.success(`CPA 测活完成：成功 ${Number(data.cpa_test.success || 0)}，失败 ${Number(data.cpa_test.fail || 0)}`);
      } else {
        if (!ids.length) return toast.warning("请先选择 grok2api 账号");
        const data = await refreshGrok2ApiAccounts(ids);
        setLastResult(data.grok2api_refresh);
        toast.success(`grok2api 额度刷新完成：${Number(data.grok2api_refresh.succeeded || 0)}/${Number(data.grok2api_refresh.total || ids.length)}`);
      }
      await loadAccounts(page);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "仓库测活失败");
    } finally {
      setBusy("");
    }
  };

  const loadUsage = async (ids = selected) => {
    if (!ids.length) return toast.warning("请先选择账号");
    setBusy("usage");
    try {
      if (active === "sub2api") {
        const data = await fetchSub2ApiAccountsUsage(ids);
        setLastResult(data.sub2api_usage);
        toast.success(`额度读取完成：${Number(data.sub2api_usage.success || 0)}/${Number(data.sub2api_usage.total || 0)}`);
      } else if (active === "cpa") {
        const data = await testCpaAccounts(ids);
        setLastResult(data.cpa_test);
        toast.success(`CPA 额度读取完成：${Number(data.cpa_test.success || 0)}/${Number(data.cpa_test.total || 0)}`);
      } else {
        const data = await refreshGrok2ApiAccounts(ids);
        setLastResult(data.grok2api_refresh);
        toast.success(`grok2api 额度刷新完成：${Number(data.grok2api_refresh.succeeded || 0)}/${Number(data.grok2api_refresh.total || ids.length)}`);
      }
      await loadAccounts(page);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "额度读取失败");
    } finally {
      setBusy("");
    }
  };

  const runAction = async (action: "enable" | "disable" | "delete", ids = selected) => {
    if (!ids.length) return toast.warning("请先选择账号");
    if (action === "delete" && !window.confirm(`确定删除选中的 ${ids.length} 个 ${active === "sub2api" ? "sub2api" : active === "cpa" ? "CPA" : "grok2api"} 账号？`)) return;
    setBusy(action);
    try {
      let result: Record<string, unknown>;
      if (active === "sub2api") {
        result = (await actOnSub2ApiAccounts(ids, action)).sub2api_action;
      } else if (active === "cpa") {
        result = (await actOnCpaAccounts(ids, action)).cpa_action;
      } else {
        result = (await actOnGrok2ApiAccounts(ids, action)).grok2api_action;
      }
      setLastResult(result);
      toast.success(`${action === "delete" ? "删除" : action === "enable" ? "启用" : "停用"}完成：${Number(result.success || 0)}/${Number(result.total || 0)}`);
      await loadAccounts(Math.min(page, totalPages));
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "仓库操作失败");
    } finally {
      setBusy("");
    }
  };

  const toggleSchedule = async () => {
    if (!config) return;
    if (active === "grok2api") return;
    const wasScheduled = active === "sub2api" ? Boolean(subConfig?.scheduled_enabled) : Boolean(cpaConfig?.scheduled_enabled);
    setBusy("schedule");
    try {
      if (active === "sub2api") {
        const data = await updateSub2ApiConfig({ scheduled_enabled: !subConfig?.scheduled_enabled });
        setSubConfig(data.sub2api_config);
      } else {
        const data = await updateCpaConfig({ scheduled_enabled: !cpaConfig?.scheduled_enabled });
        setCpaConfig(data.cpa_config);
      }
      toast.success(wasScheduled ? "已停止定期测活" : "已开启定期测活");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "更新定时测活失败");
    } finally {
      setBusy("");
    }
  };

  const statCards = active === "cpa"
    ? [["总账号", stats.total ?? total], ["启用", stats.active ?? 0], ["停用", stats.disabled ?? 0], ["异常", stats.failed ?? 0], ["已测额度", stats.tested ?? 0]]
    : active === "grok2api"
      ? [["Web SSO 账号", total], ["当前页", items.length], ["已选择", selected.length], ["Provider", "grok_web"], ["能力", "Imagine"]]
      : [["筛选账号", total], ["当前页", items.length], ["已选择", selected.length], ["定期测活", subConfig?.scheduled_enabled ? "开启" : "关闭"], ["巡检间隔", `${subConfig?.check_interval_minutes || 60} 分`]];

  return (
    <Card className="overflow-hidden">
      <CardHeader className="border-b border-slate-200/80">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <CardTitle className="flex items-center gap-2 text-xl text-slate-950"><BoxesIcon /><span>中转仓库管理</span></CardTitle>
            <CardDescription className="mt-2 text-slate-600">管理能力统一，账号池仍按 sub2api、CPA 与 grok2api 分开查询和操作。</CardDescription>
          </div>
          <div className="flex border border-slate-200 bg-slate-50 p-1">
            <Button variant={active === "sub2api" ? "default" : "ghost"} className={`h-9 ${active === "sub2api" ? "bg-emerald-600 text-white hover:bg-emerald-700" : "text-slate-600"}`} onClick={() => void switchWarehouse("sub2api")}><Database className="size-4" />sub2api</Button>
            <Button variant={active === "cpa" ? "default" : "ghost"} className={`h-9 ${active === "cpa" ? "bg-emerald-600 text-white hover:bg-emerald-700" : "text-slate-600"}`} onClick={() => void switchWarehouse("cpa")}><ServerCog className="size-4" />CPA</Button>
            <Button variant={active === "grok2api" ? "default" : "ghost"} className={`h-9 ${active === "grok2api" ? "bg-emerald-600 text-white hover:bg-emerald-700" : "text-slate-600"}`} onClick={() => void switchWarehouse("grok2api")}><Images className="size-4" />grok2api</Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4 p-5">
        {!enabled ? (
          <div className="border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">当前 {active === "sub2api" ? "sub2api" : active === "cpa" ? "CPA" : "grok2api"} 仓库未启用，请先在“中转仓库配置”中保存地址和凭据。</div>
        ) : null}

        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-5">
          {statCards.map(([label, value]) => <div key={String(label)} className="border border-slate-200 bg-slate-50 px-3 py-3"><div className="text-[11px] text-slate-500">{label}</div><div className="mt-1 text-lg font-semibold tabular-nums text-slate-950">{String(value)}</div></div>)}
        </div>

        <div className="grid gap-2 sm:grid-cols-2 2xl:grid-cols-[minmax(260px,1fr)_180px_180px_120px_auto]">
          <div className="relative"><Search className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-slate-400" /><Input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索邮箱 / 文件名 / 账号 ID" className="pl-9" disabled={!enabled} /></div>
          <select value={status} onChange={(event) => setStatus(event.target.value)} className="h-11 border border-slate-200 bg-white px-3 text-sm" disabled={!enabled}><option value="all">全部状态</option><option value="active">启用</option><option value="disabled">停用</option><option value="failed">异常</option></select>
          {active === "sub2api" ? <select value={groupId} onChange={(event) => setGroupId(event.target.value)} className="h-11 border border-slate-200 bg-white px-3 text-sm" disabled={!enabled}><option value="">全部分组</option>{groups.map((item) => { const id = String(item.id || item.group_id || ""); return id ? <option key={id} value={id}>{String(item.name || item.title || id)}</option> : null; })}</select> : <div className="hidden 2xl:block" />}
          <select value={pageSize} onChange={(event) => { setPageSize(Number(event.target.value)); setPage(1); }} className="h-11 border border-slate-200 bg-white px-3 text-sm" disabled={!enabled}><option value={20}>20/页</option><option value={50}>50/页</option><option value={100}>100/页</option><option value={200}>200/页</option></select>
          <Button className="h-11 bg-emerald-600 text-white hover:bg-emerald-700" onClick={() => void loadAccounts(1)} disabled={!enabled || loading}><RefreshCw className={`size-4 ${loading ? "animate-spin" : ""}`} />查询</Button>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-2 border-y border-slate-200 py-3">
          <div className="text-xs text-slate-500">已选 {selected.length} 个；不选择账号时“一键测活”会检查当前仓库全部账号。</div>
          <div className="flex flex-wrap gap-2">
            {active !== "grok2api" ? <Button variant="outline" className="h-9 bg-white" onClick={() => void toggleSchedule()} disabled={!enabled || Boolean(busy)}><Power className="size-4" />{config && "scheduled_enabled" in config && config.scheduled_enabled ? "停止定期测活" : "开启定期测活"}</Button> : null}
            <Button variant="outline" className="h-9 bg-white" onClick={() => void runTest()} disabled={!enabled || Boolean(busy)}>{busy === "test" ? <LoaderCircle className="size-4 animate-spin" /> : <Activity className="size-4" />}一键测活</Button>
            <Button variant="outline" className="h-9 bg-white" onClick={() => void loadUsage()} disabled={!enabled || !selected.length || Boolean(busy)}>{busy === "usage" ? <LoaderCircle className="size-4 animate-spin" /> : <Gauge className="size-4" />}额度</Button>
            <Button variant="outline" className="h-9 bg-white" onClick={() => void runAction("enable")} disabled={!enabled || !selected.length || Boolean(busy)}><Play className="size-4" />启用</Button>
            <Button variant="outline" className="h-9 bg-white" onClick={() => void runAction("disable")} disabled={!enabled || !selected.length || Boolean(busy)}><Ban className="size-4" />停用</Button>
            <Button variant="outline" className="h-9 border-rose-200 bg-rose-50 text-rose-700 hover:bg-rose-100" onClick={() => void runAction("delete")} disabled={!enabled || !selected.length || Boolean(busy)}><Trash2 className="size-4" />删除</Button>
          </div>
        </div>

        <div className="overflow-hidden border border-slate-200">
          <div className="max-h-[650px] overflow-auto">
            <table className="w-full min-w-[1180px] table-fixed border-separate border-spacing-0 text-left text-sm">
              <colgroup><col style={{ width: 52 }} /><col style={{ width: 320 }} /><col style={{ width: 130 }} /><col style={{ width: 150 }} /><col style={{ width: 210 }} /><col style={{ width: 230 }} /><col style={{ width: 220 }} /></colgroup>
              <thead className="sticky top-0 z-10 bg-slate-50 text-xs font-semibold text-slate-500"><tr><th className="border-r border-b border-slate-200 px-3 py-3"><Checkbox checked={allPageSelected} onCheckedChange={togglePage} disabled={!pageIds.length} /></th><th className="border-r border-b border-slate-200 px-3 py-3">账号</th><th className="border-r border-b border-slate-200 px-3 py-3">状态</th><th className="border-r border-b border-slate-200 px-3 py-3">类型</th><th className="border-r border-b border-slate-200 px-3 py-3">额度</th><th className="border-r border-b border-slate-200 px-3 py-3">最近测活</th><th className="border-b border-slate-200 px-3 py-3">操作</th></tr></thead>
              <tbody>
                {loading ? <tr><td colSpan={7} className="h-36 text-center"><LoaderCircle className="mx-auto size-5 animate-spin text-slate-400" /></td></tr> : !items.length ? <tr><td colSpan={7} className="h-36 text-center text-slate-500">暂无账号</td></tr> : items.map((item) => {
                  const id = idOf(active, item);
                  const rowStatus = statusOf(active, item);
                  const health = record(item.warehouse_health);
                  const healthStatus = active === "grok2api" ? String(item.authStatus || (item.enabled === false ? "disabled" : "active")).toLowerCase() : healthOf(item);
                  const isSelected = selected.includes(id);
                  return <tr key={id} className={isSelected ? "bg-emerald-50" : "bg-white hover:bg-slate-50"}>
                    <td className="border-r border-b border-slate-100 px-3 py-3"><Checkbox checked={isSelected} onCheckedChange={() => toggleOne(id)} /></td>
                    <td className="border-r border-b border-slate-100 px-3 py-3"><div className="truncate font-mono text-xs font-semibold text-slate-950" title={emailOf(item)}>{emailOf(item)}</div><div className="mt-1 truncate font-mono text-[11px] text-slate-400" title={id}>{active === "cpa" ? id : `ID ${id}`}</div></td>
                    <td className="border-r border-b border-slate-100 px-3 py-3"><div className="flex flex-wrap gap-1.5"><Badge variant={rowStatus === "active" ? "success" : rowStatus === "disabled" ? "secondary" : "danger"} className="rounded-md">{rowStatus}</Badge><Badge variant={healthVariant(healthStatus)} className="rounded-md">{healthStatus}</Badge></div></td>
                    <td className="border-r border-b border-slate-100 px-3 py-3"><Badge variant="outline" className="rounded-md">{planOf(active, item)}</Badge></td>
                    <td className="border-r border-b border-slate-100 px-3 py-3 text-xs font-semibold text-slate-700">{quotaOf(active, item)}</td>
                    <td className="border-r border-b border-slate-100 px-3 py-3 text-xs text-slate-600"><div>{formatTime(active === "grok2api" ? item.webTierSyncedAt || item.lastUsedAt || item.createdAt : health.tested_at || health.checked_at)}</div>{active === "grok2api" && item.lastError ? <div className="mt-1 line-clamp-2 text-[11px] text-rose-600" title={String(item.lastError)}>{String(item.lastError)}</div> : health.error ? <div className="mt-1 line-clamp-2 text-[11px] text-rose-600" title={String(health.error)}>{String(health.error)}</div> : null}</td>
                    <td className="border-b border-slate-100 px-3 py-3"><div className="flex flex-wrap gap-1.5"><Button variant="outline" className="h-7 bg-white px-2 text-xs" onClick={() => void runTest([id])} disabled={Boolean(busy)}>测活</Button><Button variant="outline" className="h-7 bg-white px-2 text-xs" onClick={() => void loadUsage([id])} disabled={Boolean(busy)}>额度</Button><Button variant="outline" className="h-7 border-rose-200 bg-rose-50 px-2 text-xs text-rose-700" onClick={() => void runAction("delete", [id])} disabled={Boolean(busy)}>删除</Button></div></td>
                  </tr>;
                })}
              </tbody>
            </table>
          </div>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-3 text-xs text-slate-500"><span>共 {total} 个，{page}/{totalPages} 页</span><div className="flex gap-2"><Button variant="outline" className="h-8 bg-white" onClick={() => void loadAccounts(Math.max(1, page - 1))} disabled={page <= 1 || loading}>上一页</Button><Button variant="outline" className="h-8 bg-white" onClick={() => void loadAccounts(Math.min(totalPages, page + 1))} disabled={page >= totalPages || loading}>下一页</Button></div></div>

        {lastResult ? <details className="border border-slate-200 bg-slate-50 p-3"><summary className="cursor-pointer text-xs font-semibold text-slate-700">最近一次仓库操作结果</summary><pre className="mt-3 max-h-72 overflow-auto whitespace-pre-wrap text-[11px] leading-5 text-slate-600">{JSON.stringify(lastResult, null, 2)}</pre></details> : null}
      </CardContent>
    </Card>
  );
}

function BoxesIcon() {
  return <span className="flex size-7 items-center justify-center bg-emerald-50 text-emerald-700"><Warehouse className="size-4" /></span>;
}
