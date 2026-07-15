"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Copy, LoaderCircle, Play, RefreshCw, Save, Search, Square, Trash2, UploadCloud, Zap } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import {
  clearGarbageHealthTask,
  exportGarbageHealth,
  fetchGarbageHealthState,
  fetchSub2ApiGroups,
  importGarbageHealthToCpa,
  importGarbageHealthToSub2Api,
  scanGarbageHealth,
  selectGarbageHealthDirectory,
  startGarbageHealthTask,
  stopGarbageHealthTask,
  updateGarbageHealthConfig,
  type GarbageHealthConfig,
  type GarbageHealthResult,
  type GarbageHealthState,
} from "@/lib/api";

const DEFAULT_CONFIG: GarbageHealthConfig = {
  input_dir: "",
  output_dir: "",
  proxy: "",
  concurrency: 12,
  timeout: 45,
  backend: "auto",
  refresh_tokens: true,
  include_archives: true,
  check_models: false,
  check_quota: true,
  keep_alive_only: true,
  keep_with_quota_only: false,
  realtime_import_sub2api: false,
  sub2api_group_id: "",
  auto_delete_dead: false,
  limit: 0,
};

function numberText(value: unknown) {
  const n = Number(value || 0);
  return Number.isFinite(n) ? n.toLocaleString("zh-CN") : "0";
}

function dateText(value?: string) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
}

function statusVariant(status?: string): "success" | "danger" | "warning" | "info" | "secondary" {
  if (!status) return "secondary";
  if (status === "ok") return "success";
  if (["banned", "token_invalidated", "unauthorized", "missing_token"].includes(status)) return "danger";
  if (["timeout", "network_error", "blocked", "rate_limited", "upstream_error"].includes(status)) return "warning";
  return "info";
}

function levelClass(level?: string) {
  if (level === "danger" || level === "error") return "text-rose-300";
  if (level === "warning") return "text-amber-300";
  if (level === "success") return "text-emerald-300";
  return "text-slate-200";
}

function normalizeDraft(draft: GarbageHealthConfig): GarbageHealthConfig {
  return {
    ...draft,
    input_dir: String(draft.input_dir || "").trim(),
    output_dir: String(draft.output_dir || "").trim(),
    proxy: String(draft.proxy || "").trim(),
    concurrency: Math.max(1, Math.min(64, Math.round(Number(draft.concurrency || DEFAULT_CONFIG.concurrency)))),
    timeout: Math.max(5, Math.min(180, Math.round(Number(draft.timeout || DEFAULT_CONFIG.timeout)))),
    backend: ["auto", "curl", "requests"].includes(String(draft.backend)) ? draft.backend : "auto",
    refresh_tokens: Boolean(draft.refresh_tokens),
    include_archives: Boolean(draft.include_archives),
    check_models: Boolean(draft.check_models),
    check_quota: Boolean(draft.check_quota),
    keep_alive_only: Boolean(draft.keep_alive_only),
    keep_with_quota_only: Boolean(draft.keep_with_quota_only),
    realtime_import_sub2api: Boolean(draft.realtime_import_sub2api),
    sub2api_group_id: String(draft.sub2api_group_id || "").trim(),
    auto_delete_dead: Boolean(draft.auto_delete_dead),
    limit: Math.max(0, Math.min(200000, Math.round(Number(draft.limit || 0)))),
  };
}

function reportName(key: string) {
  const names: Record<string, string> = {
    summary_csv: "汇总 CSV",
    alive_safe_csv: "存活安全 CSV",
    results_json: "完整结果 JSON",
    alive_accounts_json: "存活账号 JSON",
    alive_sub2api_json: "存活 sub2api JSON",
    alive_cpa_json: "存活 CPA JSON",
    skipped_json: "跳过记录 JSON",
  };
  return names[key] || key;
}

export function GarbageHealthCard() {
  const [state, setState] = useState<GarbageHealthState | null>(null);
  const [draft, setDraft] = useState<GarbageHealthConfig>(DEFAULT_CONFIG);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [selectingDirectory, setSelectingDirectory] = useState(false);
  const dirtyRef = useRef(false);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [sub2apiGroups, setSub2ApiGroups] = useState<Array<Record<string, unknown>>>([]);
  const [loadingGroups, setLoadingGroups] = useState(false);

  const syncState = (next: GarbageHealthState, syncDraft = false) => {
    setState(next);
    if (syncDraft || !dirtyRef.current) {
      setDraft({ ...DEFAULT_CONFIG, ...(next.config || {}) });
      dirtyRef.current = false;
    }
  };

  const load = async (silent = false, syncDraft = false) => {
    if (!silent) setLoading(true);
    try {
      const data = await fetchGarbageHealthState();
      syncState(data.garbage_health, syncDraft);
    } catch (error) {
      if (!silent) toast.error(error instanceof Error ? error.message : "捡垃圾状态加载失败");
    } finally {
      if (!silent) setLoading(false);
    }
  };

  const loadSub2ApiGroups = async (showError = false) => {
    setLoadingGroups(true);
    try {
      const data = await fetchSub2ApiGroups();
      setSub2ApiGroups(data.sub2api_groups?.items || []);
    } catch (error) {
      setSub2ApiGroups([]);
      if (showError) toast.error(error instanceof Error ? error.message : "sub2api 分组加载失败");
    } finally {
      setLoadingGroups(false);
    }
  };

  useEffect(() => {
    void load(false, true);
    void loadSub2ApiGroups(false);
    const timer = window.setInterval(() => void load(true), 2500);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const updateDraft = <K extends keyof GarbageHealthConfig>(key: K, value: GarbageHealthConfig[K]) => {
    setDraft((current) => ({ ...current, [key]: value }));
    dirtyRef.current = true;
  };

  const doSave = async () => {
    setSaving(true);
    try {
      const data = await updateGarbageHealthConfig(normalizeDraft(draft));
      syncState(data.garbage_health, true);
      toast.success("捡垃圾配置已保存");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const doScan = async () => {
    setSaving(true);
    try {
      const data = await scanGarbageHealth(normalizeDraft(draft));
      syncState(data.garbage_health, true);
      const stats = data.garbage_health.last_scan || {};
      toast.success(`扫描完成：选中 ${numberText(stats.selected_accounts)} 个账号`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "扫描失败");
    } finally {
      setSaving(false);
    }
  };

  const doStart = async () => {
    setSaving(true);
    try {
      const data = await startGarbageHealthTask({ clear_results: true, config: normalizeDraft(draft) });
      syncState(data.garbage_health, true);
      toast.success("捡垃圾测活已启动");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "启动失败");
    } finally {
      setSaving(false);
    }
  };

  const doStop = async () => {
    try {
      const data = await stopGarbageHealthTask();
      syncState(data.garbage_health);
      toast.success("已请求停止");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "停止失败");
    }
  };

  const doClear = async () => {
    try {
      const data = await clearGarbageHealthTask();
      syncState(data.garbage_health, true);
      toast.success("已清空日志和结果");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "清空失败");
    }
  };

  const copyText = async (text: string, label = "内容") => {
    if (!text) return toast.error("没有可复制内容");
    await navigator.clipboard.writeText(text);
    toast.success(`${label}已复制`);
  };

  const saveJsonFile = (filename: string, data: unknown) => {
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  };

  const chooseDirectory = async () => {
    setSelectingDirectory(true);
    try {
      const data = await selectGarbageHealthDirectory();
      if (!data.directory.selected || !data.directory.path) return;
      updateDraft("input_dir", data.directory.path);
      toast.success("已选择扫描目录");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "打开目录选择器失败");
    } finally {
      setSelectingDirectory(false);
    }
  };

  const doDownloadExport = async (format: "sub2api" | "cpa" | "full") => {
    setSaving(true);
    try {
      const data = await exportGarbageHealth(format);
      saveJsonFile(data.garbage_export.filename || `garbage-${format}.json`, data.garbage_export.data);
      toast.success(`已下载 ${data.garbage_export.count || 0} 个账号`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "导出失败");
    } finally {
      setSaving(false);
    }
  };

  const doImportSub2Api = async () => {
    setSaving(true);
    try {
      const data = await importGarbageHealthToSub2Api(draft.sub2api_group_id || "");
      toast.success(`已提交导入 sub2api：${Number(data.garbage_import?.count || 0)} 个`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "导入 sub2api 失败");
    } finally {
      setSaving(false);
    }
  };

  const doImportCpa = async () => {
    setSaving(true);
    try {
      const data = await importGarbageHealthToCpa();
      toast.success(`已导入 CPA 仓库：${Number(data.garbage_import?.count || 0)} 个`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "导入 CPA 仓库失败");
    } finally {
      setSaving(false);
    }
  };

  const stats = state?.stats || {
    total: 0,
    checked: 0,
    alive: 0,
    failed: 0,
    running: 0,
    status_counts: {},
  };
  const running = Boolean(state?.enabled);
  const progress = stats.total ? Math.min(100, (Number(stats.checked || 0) / Number(stats.total || 1)) * 100) : 0;
  const statusOptions = useMemo(() => {
    const set = new Set<string>(Object.keys(stats.status_counts || {}));
    (state?.results || []).forEach((item) => item.status && set.add(item.status));
    return Array.from(set).sort();
  }, [state?.results, stats.status_counts]);
  const filteredResults = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return (state?.results || []).filter((item) => {
      const statusOk = statusFilter === "all" || item.status === statusFilter;
      if (!statusOk) return false;
      if (!needle) return true;
      const haystack = `${item.email || ""} ${item.identity || ""} ${item.status || ""} ${item.error || ""} ${item.chatgpt_account_id || ""}`.toLowerCase();
      return haystack.includes(needle);
    });
  }, [query, state?.results, statusFilter]);

  const reportEntries = Object.entries(state?.reports || {});
  const statCards = [
    { label: "JSON 文档", value: stats.json_documents },
    { label: "原始对象", value: stats.raw_account_objects },
    { label: "去重账号", value: stats.deduped_accounts },
    { label: "已测 / 总数", value: `${numberText(stats.checked)}/${numberText(stats.total)}` },
    { label: "存活", value: stats.alive },
    { label: "有额度", value: stats.with_quota },
    { label: "保留", value: stats.kept_accounts },
    { label: "失败", value: stats.failed },
    { label: "导入 成/败/跳", value: `${numberText(stats.realtime_import_success)}/${numberText(stats.realtime_import_failed)}/${numberText(stats.realtime_import_skipped)}` },
    { label: "已删失效", value: stats.deleted_dead_accounts },
  ];

  const groupOptions = useMemo(() => sub2apiGroups.filter((item) => !item.platform || String(item.platform).toLowerCase() === "openai").map((item) => ({
    id: String(item.id ?? item.group_id ?? item.groupId ?? ""),
    name: String(item.name ?? item.group_name ?? item.display_name ?? item.title ?? item.id ?? item.group_id ?? item.groupId ?? "未命名分组"),
  })).filter((item) => item.id), [sub2apiGroups]);

  return (
    <Card className="overflow-hidden">
      <CardHeader className="border-b border-slate-200/80">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <CardTitle className="flex items-center gap-2 text-xl text-slate-950">
              <Trash2 className="size-5 text-emerald-600" />
              捡垃圾
            </CardTitle>
            <CardDescription className="mt-2 text-slate-600">
              扫描本地 JSON / CPA / sub2api 文件，批量测活并输出存活账号报告。
            </CardDescription>
          </div>
          <div className="flex flex-wrap gap-2">
            <Badge variant={running ? "success" : "secondary"}>{running ? "运行中" : "空闲"}</Badge>
            <Badge variant="outline">Backend: {draft.backend}</Badge>
          </div>
        </div>
      </CardHeader>

      <CardContent className="space-y-5 p-5">
        <section className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
          <div className="space-y-4 rounded-[26px] border border-slate-200 bg-slate-50/70 p-4">
            <div className="grid gap-3 lg:grid-cols-2">
              <label className="space-y-1.5 lg:col-span-2">
                <span className="text-xs font-medium text-slate-600">扫描目录</span>
                <div className="flex gap-2">
                  <Input value={draft.input_dir} onChange={(event) => updateDraft("input_dir", event.target.value)} disabled={running} placeholder="请选择包含账号文件的目录" />
                  <Button type="button" variant="outline" className="h-11 shrink-0 rounded-2xl bg-white" onClick={() => void chooseDirectory()} disabled={running || selectingDirectory}>{selectingDirectory ? <LoaderCircle className="size-4 animate-spin" /> : null}选择目录</Button>
                </div>
              </label>
              <label className="space-y-1.5 lg:col-span-2">
                <span className="text-xs font-medium text-slate-600">输出目录（留空则写入扫描目录 health_reports）</span>
                <Input value={draft.output_dir} onChange={(event) => updateDraft("output_dir", event.target.value)} disabled={running} placeholder="可留空" />
              </label>
              <label className="space-y-1.5">
                <span className="text-xs font-medium text-slate-600">并发</span>
                <Input type="number" min={1} max={64} value={draft.concurrency} onChange={(event) => updateDraft("concurrency", Number(event.target.value))} disabled={running} />
              </label>
              <label className="space-y-1.5">
                <span className="text-xs font-medium text-slate-600">超时（秒）</span>
                <Input type="number" min={5} max={180} value={draft.timeout} onChange={(event) => updateDraft("timeout", Number(event.target.value))} disabled={running} />
              </label>
              <label className="space-y-1.5">
                <span className="text-xs font-medium text-slate-600">后端</span>
                <select
                  value={draft.backend}
                  onChange={(event) => updateDraft("backend", event.target.value)}
                  disabled={running}
                  className="h-11 w-full rounded-2xl border border-slate-200 bg-white px-4 text-sm text-slate-900 shadow-sm outline-none transition focus-visible:border-emerald-500 focus-visible:ring-[3px] focus-visible:ring-emerald-100 disabled:cursor-not-allowed disabled:bg-slate-50 disabled:opacity-60"
                >
                  <option value="auto">auto</option>
                  <option value="curl">curl</option>
                  <option value="requests">requests</option>
                </select>
              </label>
              <label className="space-y-1.5">
                <span className="text-xs font-medium text-slate-600">限制数量（0=全部）</span>
                <Input type="number" min={0} value={draft.limit} onChange={(event) => updateDraft("limit", Number(event.target.value))} disabled={running} />
              </label>
            </div>

            <div className="grid gap-2 md:grid-cols-3 xl:grid-cols-6">
              <label className="flex items-center gap-2 rounded-2xl border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700">
                <Checkbox checked={draft.refresh_tokens} onCheckedChange={(checked) => updateDraft("refresh_tokens", checked)} disabled={running} />
                刷新 token 后测活
              </label>
              <label className="flex items-center gap-2 rounded-2xl border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700">
                <Checkbox checked={draft.include_archives} onCheckedChange={(checked) => updateDraft("include_archives", checked)} disabled={running} />
                扫描 zip / 7z
              </label>
              <label className="flex items-center gap-2 rounded-2xl border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700">
                <Checkbox checked={draft.check_models} onCheckedChange={(checked) => updateDraft("check_models", checked)} disabled={running} />
                附加检查 models
              </label>
              <label className="flex items-center gap-2 rounded-2xl border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700">
                <Checkbox checked={draft.check_quota} onCheckedChange={(checked) => updateDraft("check_quota", checked)} disabled={running || draft.realtime_import_sub2api} />
                测活同时测额度
              </label>
              <label className="flex items-center gap-2 rounded-2xl border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700">
                <Checkbox checked={draft.keep_alive_only} onCheckedChange={(checked) => updateDraft("keep_alive_only", checked)} disabled={running} />
                保留存活账号
              </label>
              <label className="flex items-center gap-2 rounded-2xl border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700">
                <Checkbox checked={draft.keep_with_quota_only} onCheckedChange={(checked) => updateDraft("keep_with_quota_only", checked)} disabled={running} />
                只保留有额度账号
              </label>
            </div>

            <div className="grid gap-2 rounded-2xl border border-sky-200 bg-sky-50/70 p-3 lg:grid-cols-[minmax(220px,1fr)_minmax(240px,1fr)_44px_minmax(220px,1fr)] lg:items-end">
              <label className="flex h-11 items-center gap-2 rounded-xl border border-sky-200 bg-white px-3 text-sm font-medium text-sky-900">
                <Checkbox
                  checked={draft.realtime_import_sub2api}
                  onCheckedChange={(checked) => {
                    updateDraft("realtime_import_sub2api", checked);
                    if (checked) updateDraft("check_quota", true);
                    if (checked && !sub2apiGroups.length) void loadSub2ApiGroups(true);
                  }}
                  disabled={running}
                />
                <UploadCloud className="size-4" />
                有额度实时导入
              </label>
              <label className="space-y-1">
                <span className="text-xs font-medium text-sky-900">sub2api 导入分组</span>
                <select
                  value={draft.sub2api_group_id}
                  onChange={(event) => updateDraft("sub2api_group_id", event.target.value)}
                  disabled={running}
                  className="h-11 w-full rounded-xl border border-sky-200 bg-white px-3 text-sm text-slate-900 outline-none focus-visible:border-sky-500 focus-visible:ring-[3px] focus-visible:ring-sky-100 disabled:opacity-60"
                >
                  <option value="">默认分组</option>
                  {groupOptions.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}
                </select>
              </label>
              <Button type="button" variant="outline" size="icon" className="size-11 rounded-xl border-sky-200 bg-white" onClick={() => void loadSub2ApiGroups(true)} disabled={running || loadingGroups} title="刷新 sub2api 分组" aria-label="刷新 sub2api 分组">
                <RefreshCw className={`size-4 ${loadingGroups ? "animate-spin" : ""}`} />
              </Button>
              <label className="flex h-11 items-center gap-2 rounded-xl border border-rose-200 bg-white px-3 text-sm font-medium text-rose-800">
                <Checkbox checked={draft.auto_delete_dead} onCheckedChange={(checked) => updateDraft("auto_delete_dead", checked)} disabled={running} />
                <Trash2 className="size-4" />
                自动删除确认失效
              </label>
            </div>

            <div className="flex flex-wrap gap-2">
              <Button className="h-10 rounded-2xl bg-emerald-600 text-white hover:bg-emerald-700" onClick={() => void doStart()} disabled={saving || running}>
                {saving ? <LoaderCircle className="size-4 animate-spin" /> : <Play className="size-4" />}
                开始测活
              </Button>
              <Button variant="outline" className="h-10 rounded-2xl bg-white" onClick={() => void doScan()} disabled={saving || running}>
                <Search className="size-4" />
                只扫描
              </Button>
              <Button variant="outline" className="h-10 rounded-2xl bg-white" onClick={() => void doSave()} disabled={saving || running}>
                <Save className="size-4" />
                保存配置
              </Button>
              <Button variant="outline" className="h-10 rounded-2xl bg-white" onClick={() => void doStop()} disabled={!running}>
                <Square className="size-4" />
                停止
              </Button>
              <Button variant="outline" className="h-10 rounded-2xl bg-white" onClick={() => void load(false, true)} disabled={loading}>
                {loading ? <LoaderCircle className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
                刷新
              </Button>
              <Button variant="outline" className="h-10 rounded-2xl border-rose-200 bg-rose-50 text-rose-700 hover:bg-rose-100" onClick={() => void doClear()} disabled={running}>
                <Trash2 className="size-4" />
                清空
              </Button>
            </div>

            <div className="flex flex-wrap gap-2 rounded-2xl border border-emerald-200 bg-emerald-50/70 p-3">
              <Button variant="outline" className="h-9 rounded-xl bg-white" onClick={() => void doDownloadExport("full")} disabled={saving || running}>
                下载完整 JSON
              </Button>
              <Button variant="outline" className="h-9 rounded-xl bg-white" onClick={() => void doDownloadExport("sub2api")} disabled={saving || running}>
                下载 sub2api JSON
              </Button>
              <Button variant="outline" className="h-9 rounded-xl bg-white" onClick={() => void doDownloadExport("cpa")} disabled={saving || running}>
                下载 CPA JSON
              </Button>
              <Button className="h-9 rounded-xl bg-sky-600 text-white hover:bg-sky-700" onClick={() => void doImportSub2Api()} disabled={saving || running}>
                一键导入 sub2api
              </Button>
              <Button className="h-9 rounded-xl bg-indigo-600 text-white hover:bg-indigo-700" onClick={() => void doImportCpa()} disabled={saving || running}>
                一键导入 CPA仓库
              </Button>
            </div>
          </div>

          <div className="rounded-[26px] border border-slate-900 bg-slate-950 p-4 text-slate-100 shadow-[0_18px_40px_-28px_rgba(15,23,42,0.65)]">
            <div className="mb-3 flex items-center justify-between">
              <div>
                <div className="text-sm font-semibold">执行日志</div>
                <div className="text-xs text-slate-400">扫描、测活、报告输出都会写入这里</div>
              </div>
              <Badge variant="outline" className="border-slate-700 bg-slate-900 text-slate-300">
                {numberText(state?.logs?.length)} 条
              </Badge>
            </div>
            <div className="h-[330px] overflow-y-auto rounded-2xl border border-slate-800 bg-black/30 p-3 font-mono text-xs leading-5">
              {(state?.logs || []).length ? (
                (state?.logs || []).slice(-240).map((item, index) => (
                  <div key={`${item.time}-${index}`} className={levelClass(item.level)}>
                    <span className="text-slate-500">[{dateText(item.time)}]</span> {item.text}
                  </div>
                ))
              ) : (
                <div className="text-slate-500">暂无日志</div>
              )}
            </div>
          </div>
        </section>

        <section className="grid gap-3 md:grid-cols-5 xl:grid-cols-10">
          {statCards.map((item) => (
            <div key={item.label} className="rounded-2xl border border-slate-200 bg-slate-50/80 p-3">
              <div className="text-xs font-medium text-slate-500">{item.label}</div>
              <div className="mt-1 text-2xl font-semibold tabular-nums text-slate-950">{typeof item.value === "string" ? item.value : numberText(item.value)}</div>
            </div>
          ))}
        </section>

        <section className="rounded-[26px] border border-slate-200 bg-white p-4">
          <div className="mb-2 flex items-center justify-between text-xs text-slate-500">
            <span>测活进度</span>
            <span>{progress.toFixed(1)}%</span>
          </div>
          <div className="h-3 overflow-hidden rounded-full bg-slate-100">
            <div className="h-full rounded-full bg-emerald-500 transition-[width] duration-300" style={{ width: `${progress}%` }} />
          </div>
          <div className="mt-3 flex flex-wrap gap-2">
            {Object.entries(stats.status_counts || {}).map(([status, count]) => (
              <Badge key={status} variant={statusVariant(status)}>
                {status}: {numberText(count)}
              </Badge>
            ))}
          </div>
        </section>

        {reportEntries.length ? (
          <section className="rounded-[26px] border border-emerald-200 bg-emerald-50/60 p-4">
            <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-emerald-950">
              <Zap className="size-4" />
              报告文件
            </div>
            <div className="grid gap-2 lg:grid-cols-2">
              {reportEntries.map(([key, value]) => (
                <div key={key} className="flex min-w-0 items-center justify-between gap-2 rounded-2xl border border-emerald-100 bg-white px-3 py-2">
                  <div className="min-w-0">
                    <div className="text-sm font-medium text-slate-800">{reportName(key)}</div>
                    <div className="truncate text-xs text-slate-500">{value}</div>
                  </div>
                  <Button variant="outline" size="sm" className="bg-white" onClick={() => void copyText(value, reportName(key))}>
                    <Copy className="size-3.5" />
                    复制
                  </Button>
                </div>
              ))}
            </div>
          </section>
        ) : null}

        <section className="rounded-[26px] border border-slate-200 bg-white p-4">
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <Input className="min-w-[260px] flex-1" placeholder="搜索邮箱 / identity / 错误信息" value={query} onChange={(event) => setQuery(event.target.value)} />
            <select
              value={statusFilter}
              onChange={(event) => setStatusFilter(event.target.value)}
              className="h-11 rounded-2xl border border-slate-200 bg-white px-4 text-sm text-slate-900 shadow-sm outline-none transition focus-visible:border-emerald-500 focus-visible:ring-[3px] focus-visible:ring-emerald-100"
            >
              <option value="all">全部状态</option>
              {statusOptions.map((status) => (
                <option key={status} value={status}>
                  {status}
                </option>
              ))}
            </select>
            <Badge variant="outline">显示 {numberText(Math.min(filteredResults.length, 400))} / {numberText(filteredResults.length)}</Badge>
          </div>

          <div className="overflow-hidden rounded-2xl border border-slate-200">
            <div className="max-h-[560px] overflow-auto">
              <table className="w-full min-w-[980px] border-collapse text-sm">
                <thead className="sticky top-0 bg-slate-50 text-left text-xs font-semibold text-slate-500">
                  <tr>
                    <th className="border-b border-slate-200 px-3 py-2">状态</th>
                    <th className="border-b border-slate-200 px-3 py-2">邮箱 / Identity</th>
                    <th className="border-b border-slate-200 px-3 py-2">套餐</th>
                    <th className="border-b border-slate-200 px-3 py-2">额度</th>
                    <th className="border-b border-slate-200 px-3 py-2">HTTP</th>
                    <th className="border-b border-slate-200 px-3 py-2">耗时</th>
                    <th className="border-b border-slate-200 px-3 py-2">检查时间</th>
                    <th className="border-b border-slate-200 px-3 py-2">处理</th>
                    <th className="border-b border-slate-200 px-3 py-2">错误</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredResults.slice(0, 400).map((item: GarbageHealthResult, index) => (
                    <tr key={`${item.identity || item.email || index}-${index}`} className="border-b border-slate-100 last:border-0">
                      <td className="px-3 py-2">
                        <Badge variant={statusVariant(item.status)}>{item.ok ? "ok" : item.status || "unknown"}</Badge>
                      </td>
                      <td className="max-w-[260px] px-3 py-2">
                        <div className="truncate font-medium text-slate-900">{item.email || "-"}</div>
                        <div className="truncate text-xs text-slate-500">{item.identity || item.chatgpt_account_id || "-"}</div>
                      </td>
                      <td className="px-3 py-2 text-slate-700">{item.plan_type || "-"}</td>
                      <td className="px-3 py-2 text-slate-700">
                        {item.quota_status === "known" || item.quota_remaining !== undefined ? (
                          <div className="space-y-0.5 text-xs">
                            <div>5h 剩余 {item.quota_5h_remaining ?? item.quota_remaining ?? "-"}%</div>
                            <div>7d 剩余 {item.quota_7d_remaining ?? "-"}%</div>
                          </div>
                        ) : item.quota_status || "-"}
                      </td>
                      <td className="px-3 py-2 text-slate-700">{item.status_code || "-"}</td>
                      <td className="px-3 py-2 text-slate-700">{item.latency_ms ? `${item.latency_ms}ms` : "-"}</td>
                      <td className="px-3 py-2 text-slate-500">{dateText(item.checked_at)}</td>
                      <td className="px-3 py-2">
                        <div className="flex flex-wrap gap-1">
                          {item.sub2api_import_status ? <Badge variant={item.sub2api_import_status === "ok" ? "success" : item.sub2api_import_status === "queued" ? "info" : item.sub2api_import_status.startsWith("skipped") ? "warning" : "danger"}>导入 {item.sub2api_import_status}</Badge> : null}
                          {item.source_delete_status ? <Badge variant={item.source_delete_status === "deleted" ? "success" : item.source_delete_status === "failed" ? "danger" : "warning"}>删除 {item.source_delete_status}</Badge> : null}
                          {!item.sub2api_import_status && !item.source_delete_status ? <span className="text-xs text-slate-400">-</span> : null}
                        </div>
                      </td>
                      <td className="max-w-[360px] px-3 py-2">
                        <div className="line-clamp-2 text-xs text-rose-600">{item.error || item.refresh_error || item.sub2api_import_error || item.delete_error || "-"}</div>
                      </td>
                    </tr>
                  ))}
                  {!filteredResults.length ? (
                    <tr>
                      <td className="px-3 py-8 text-center text-sm text-slate-400" colSpan={9}>
                        暂无结果
                      </td>
                    </tr>
                  ) : null}
                </tbody>
              </table>
            </div>
          </div>
        </section>
      </CardContent>
    </Card>
  );
}
