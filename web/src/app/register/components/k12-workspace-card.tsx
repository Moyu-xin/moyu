"use client";

import { useEffect, useMemo, useState } from "react";
import { CheckCircle2, LoaderCircle, Play, RefreshCw, RotateCcw, Save, Settings2, Square, Warehouse } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  fetchK12State,
  resetK12Task,
  startK12Task,
  stopK12Task,
  updateK12Config,
  type AccountItem,
  type K12Config,
  type K12State,
} from "@/lib/api";

type K12ConfigDraft = {
  enabled: boolean;
  join_route: string;
  accept_invite: boolean;
  invite_timeout: number;
  outlook_team_ids_text: string;
  gmail_team_ids_text: string;
};

function joinLines(values?: string[]) {
  return (values || []).map(String).filter(Boolean).join("\n");
}

function splitLines(value: string) {
  return String(value || "")
    .replace(/,/g, "\n")
    .split("\n")
    .map((item) => item.trim())
    .filter((item, index, items) => Boolean(item) && items.indexOf(item) === index);
}

function configDraft(config?: K12Config): K12ConfigDraft {
  return {
    enabled: Boolean(config?.enabled),
    join_route: config?.join_route || "request",
    accept_invite: config?.accept_invite !== false,
    invite_timeout: Number(config?.invite_timeout) || 180,
    outlook_team_ids_text: joinLines(config?.team_ids?.outlook),
    gmail_team_ids_text: joinLines(config?.team_ids?.gmail),
  };
}

function formatSeconds(value: unknown) {
  const number = Number(value || 0);
  if (!Number.isFinite(number) || number <= 0) return "0 秒";
  if (number < 60) return `${number.toFixed(number % 1 ? 1 : 0)} 秒`;
  const minutes = Math.floor(number / 60);
  const seconds = Math.round(number % 60);
  return `${minutes} 分 ${seconds} 秒`;
}

function statusBadge(value?: string) {
  const text = String(value || "-");
  const ok = ["ok", "accepted_attempted", "invite_received", "request_sent"].includes(text);
  const failed = ["failed", "error", "request_failed"].includes(text) || text.includes("failed");
  return <Badge variant={ok ? "success" : failed ? "danger" : "secondary"} className="rounded-md">{text}</Badge>;
}

function accountSelector(account: AccountItem) {
  return String(account.inventory_id || account.inventory_key || account.email || "");
}

export function K12WorkspaceCard() {
  const [state, setState] = useState<K12State | null>(null);
  const [config, setConfig] = useState<K12ConfigDraft | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [onlyUnprocessed, setOnlyUnprocessed] = useState(false);
  const [joinK12, setJoinK12] = useState(true);
  const [importSub2Api, setImportSub2Api] = useState(true);
  const [importCpa, setImportCpa] = useState(false);
  const [healthCheck, setHealthCheck] = useState(true);

  const load = async (silent = false) => {
    if (!silent) setIsLoading(true);
    try {
      const data = await fetchK12State();
      setState(data.k12);
      setConfig((current) => current || configDraft(data.k12.config));
    } catch (error) {
      if (!silent) toast.error(error instanceof Error ? error.message : "K12 状态加载失败");
    } finally {
      if (!silent) setIsLoading(false);
    }
  };

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(true), 2000);
    return () => window.clearInterval(timer);
  }, []);

  const accounts = useMemo(() => (state?.account_pool?.accounts || []) as AccountItem[], [state]);
  const stats = state?.stats || { success: 0, fail: 0, done: 0, running: 0, total: 0, elapsed_seconds: 0, success_rate: 0 };
  const logs = state?.logs || [];
  const sub2apiEnabled = Boolean(state?.sub2api_config?.enabled);
  const cpaEnabled = Boolean(state?.cpa_config?.enabled);

  const toggleEmail = (selector: string, checked: boolean) => {
    setSelected((old) => {
      const next = new Set(old);
      if (checked) next.add(selector);
      else next.delete(selector);
      return next;
    });
  };

  const selectAll = () => setSelected(new Set(accounts.map((item) => accountSelector(item)).filter(Boolean)));
  const clearSelection = () => setSelected(new Set());

  const saveConfig = async () => {
    if (!config) return;
    setIsSaving(true);
    try {
      const data = await updateK12Config({
        enabled: config.enabled,
        team_ids: {
          outlook: splitLines(config.outlook_team_ids_text),
          gmail: splitLines(config.gmail_team_ids_text),
        },
        join_route: config.join_route || "request",
        accept_invite: config.accept_invite,
        invite_timeout: Number(config.invite_timeout) || 180,
      });
      setState(data.k12);
      setConfig(configDraft(data.k12.config));
      toast.success("K12 配置已保存");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "K12 配置保存失败");
    } finally {
      setIsSaving(false);
    }
  };

  const run = async () => {
    setIsSaving(true);
    try {
      const emails = Array.from(selected);
      const data = await startK12Task({
        emails,
        only_unprocessed: onlyUnprocessed,
        join_k12: joinK12,
        import_to_sub2api: importSub2Api,
        import_to_cpa: importCpa,
        health_check: healthCheck,
      });
      setState(data.k12);
      toast.success("K12 库存处理已启动");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "K12 任务启动失败");
    } finally {
      setIsSaving(false);
    }
  };

  const stop = async () => {
    setIsSaving(true);
    try {
      const data = await stopK12Task();
      setState(data.k12);
      toast.success("K12 任务已停止");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "K12 任务停止失败");
    } finally {
      setIsSaving(false);
    }
  };

  const reset = async () => {
    setIsSaving(true);
    try {
      const data = await resetK12Task();
      setState(data.k12);
      toast.success("K12 统计已重置");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "K12 重置失败");
    } finally {
      setIsSaving(false);
    }
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center rounded-2xl border border-slate-200 bg-white/80 p-10">
        <LoaderCircle className="size-5 animate-spin text-slate-400" />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {config && (
        <section className="border border-slate-200 bg-white p-4 shadow-sm">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="flex items-center gap-3">
              <span className="flex size-10 items-center justify-center bg-sky-50 text-sky-700"><Settings2 className="size-5" /></span>
              <div>
                <h3 className="text-base font-semibold text-slate-950">K12 配置</h3>
                <p className="mt-1 text-xs text-slate-500">Team、邀请路由和等待参数</p>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <Badge variant={config.enabled ? "success" : "secondary"} className="rounded-md">{config.enabled ? "已启用" : "未启用"}</Badge>
              <Button onClick={() => void saveConfig()} disabled={isSaving || state?.enabled} className="h-9 bg-sky-600 text-white hover:bg-sky-700">
                {isSaving ? <LoaderCircle className="size-4 animate-spin" /> : <Save className="size-4" />}保存配置
              </Button>
            </div>
          </div>

          <div className="mt-4 grid gap-4 lg:grid-cols-2">
            <label className="flex cursor-pointer items-center gap-3 border border-slate-200 bg-slate-50 px-3 py-3 text-sm font-medium text-slate-800">
              <Checkbox checked={config.enabled} onCheckedChange={(checked) => setConfig({ ...config, enabled: Boolean(checked) })} disabled={state?.enabled} />
              <span>K12 注册模式</span>
            </label>
            <label className="flex cursor-pointer items-center gap-3 border border-slate-200 bg-slate-50 px-3 py-3 text-sm font-medium text-slate-800">
              <Checkbox checked={config.accept_invite} onCheckedChange={(checked) => setConfig({ ...config, accept_invite: Boolean(checked) })} disabled={state?.enabled} />
              <span>自动接受邀请</span>
            </label>
            <label className="space-y-1.5">
              <span className="text-xs font-semibold text-slate-600">Outlook team ID</span>
              <Textarea value={config.outlook_team_ids_text} onChange={(event) => setConfig({ ...config, outlook_team_ids_text: event.target.value })} disabled={state?.enabled} className="min-h-28 bg-white font-mono text-xs" />
            </label>
            <label className="space-y-1.5">
              <span className="text-xs font-semibold text-slate-600">Gmail team ID</span>
              <Textarea value={config.gmail_team_ids_text} onChange={(event) => setConfig({ ...config, gmail_team_ids_text: event.target.value })} disabled={state?.enabled} className="min-h-28 bg-white font-mono text-xs" />
            </label>
            <label className="space-y-1.5">
              <span className="text-xs font-semibold text-slate-600">邀请路由</span>
              <select value={config.join_route} onChange={(event) => setConfig({ ...config, join_route: event.target.value })} disabled={state?.enabled} className="h-10 w-full border border-slate-200 bg-white px-3 text-sm">
                <option value="request">request</option>
                <option value="accept">accept</option>
              </select>
            </label>
            <label className="space-y-1.5">
              <span className="text-xs font-semibold text-slate-600">邀请等待（秒）</span>
              <Input type="number" min={10} max={3600} value={String(config.invite_timeout)} onChange={(event) => setConfig({ ...config, invite_timeout: Number(event.target.value) || 180 })} disabled={state?.enabled} />
            </label>
          </div>
        </section>
      )}

      <div className="grid gap-3 md:grid-cols-3 xl:grid-cols-6">
        {[
          ["运行时长", formatSeconds(stats.elapsed_seconds)],
          ["成功率", `${stats.success_rate || 0}%`],
          ["成功 / 失败", `${stats.success || 0} / ${stats.fail || 0}`],
          ["进度", `${stats.done || 0} / ${stats.total || 0}`],
          ["sub2api", sub2apiEnabled ? "已启用" : "未启用"],
          ["CPA", cpaEnabled ? "已启用" : "未启用"],
        ].map(([label, value]) => (
          <div key={String(label)} className="rounded-2xl border border-slate-200 bg-white/90 px-4 py-3 shadow-sm">
            <div className="text-xs text-slate-500">{label}</div>
            <div className="mt-1 text-lg font-semibold text-slate-950">{value}</div>
          </div>
        ))}
      </div>

      <div className="grid overflow-hidden rounded-[28px] border border-slate-200/80 bg-white/90 shadow-sm xl:grid-cols-[minmax(0,0.95fr)_minmax(0,1.05fr)]">
        <section className="min-w-0 space-y-4 border-b border-slate-200 p-4 xl:border-r xl:border-b-0">
          <div className="flex items-start justify-between gap-3">
            <div className="flex items-center gap-3">
              <div className="flex size-10 items-center justify-center rounded-xl border border-sky-200 bg-sky-50">
                <Warehouse className="size-5 text-sky-700" />
              </div>
              <div>
                <h3 className="text-lg font-semibold tracking-tight text-slate-950">{"K12 库存加入"}</h3>
                <p className="mt-1 text-xs leading-5 text-slate-500">{"选择已注册库存账号，刷新 AT 后加入 K12 空间，再分别导入所选中转仓库并测活。"}</p>
              </div>
            </div>
            <Badge variant={state?.enabled ? "success" : "secondary"} className="rounded-md">
              {state?.enabled ? "运行中" : "已停止"}
            </Badge>
          </div>

          <div className="grid gap-2 md:grid-cols-2">
            <label className="flex items-center gap-3 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-700 cursor-pointer">
              <Checkbox checked={onlyUnprocessed} onCheckedChange={(checked) => setOnlyUnprocessed(Boolean(checked))} disabled={state?.enabled} />
              <span>{"仅处理未加入/未完成账号"}</span>
            </label>
            <label className="flex items-center gap-3 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-700 cursor-pointer">
              <Checkbox checked={joinK12} onCheckedChange={(checked) => setJoinK12(Boolean(checked))} disabled={state?.enabled} />
              <span>{"执行 K12 加入"}</span>
            </label>
            <label className="flex items-center gap-3 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-700 cursor-pointer">
              <Checkbox checked={importSub2Api} onCheckedChange={(checked) => setImportSub2Api(Boolean(checked))} disabled={state?.enabled} />
              <span>{"导入 sub2api"}</span>
            </label>
            <label className="flex items-center gap-3 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-700 cursor-pointer">
              <Checkbox checked={importCpa} onCheckedChange={(checked) => setImportCpa(Boolean(checked))} disabled={state?.enabled} />
              <span>{"导入 CPA"}</span>
            </label>
            <label className="flex items-center gap-3 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-700 cursor-pointer">
              <Checkbox checked={healthCheck} onCheckedChange={(checked) => setHealthCheck(Boolean(checked))} disabled={state?.enabled} />
              <span>{"入库后测活"}</span>
            </label>
          </div>

          <div className="flex flex-wrap gap-2">
            <Button onClick={selectAll} variant="outline" className="rounded-xl border-slate-200 bg-white cursor-pointer hover:bg-slate-50" disabled={state?.enabled}>{"全选"}</Button>
            <Button onClick={clearSelection} variant="outline" className="rounded-xl border-slate-200 bg-white cursor-pointer hover:bg-slate-50" disabled={state?.enabled}>{"清空"}</Button>
            <Button onClick={() => void load()} variant="outline" className="rounded-xl border-slate-200 bg-white cursor-pointer hover:bg-slate-50" disabled={isSaving}>
              <RefreshCw className="size-4" /> {"刷新"}
            </Button>
            <Button onClick={() => void reset()} variant="outline" className="rounded-xl border-slate-200 bg-white cursor-pointer hover:bg-slate-50" disabled={isSaving || state?.enabled}>
              <RotateCcw className="size-4" /> {"重置"}
            </Button>
            <Button onClick={() => void (state?.enabled ? stop() : run())} className="rounded-xl bg-sky-600 text-white hover:bg-sky-700 cursor-pointer font-semibold" disabled={isSaving}>
              {isSaving ? <LoaderCircle className="size-4 animate-spin" /> : state?.enabled ? <Square className="size-4" /> : <Play className="size-4" />}
              {state?.enabled ? "停止" : "开始处理"}
            </Button>
          </div>

          <div className="flex items-start gap-2 rounded-xl border border-sky-200 bg-sky-50 px-3 py-2 text-xs leading-5 text-sky-800">
            <CheckCircle2 className="mt-0.5 size-4 shrink-0" />
            <span>{"未勾选账号时默认处理全部库存；已勾选时只处理选中账号。"}</span>
          </div>

          <div className="overflow-x-auto rounded-xl border border-slate-200">
            <div className="grid min-w-[720px] grid-cols-[38px_minmax(0,1.4fr)_minmax(0,0.8fr)_minmax(0,0.8fr)_minmax(0,0.8fr)_minmax(0,0.8fr)] bg-slate-50 px-3 py-2 text-xs font-semibold text-slate-500">
              <div />
              <div>{"账号"}</div>
              <div>{"K12"}</div>
              <div>{"sub2api"}</div>
              <div>{"CPA"}</div>
              <div>{"测活"}</div>
            </div>
            <div className="max-h-[420px] min-w-[720px] overflow-y-auto divide-y divide-slate-100">
              {accounts.length === 0 ? (
                <div className="p-4 text-sm text-slate-500">{"暂无库存账号"}</div>
              ) : accounts.map((account) => (
                <div key={accountSelector(account)} className="grid grid-cols-[38px_minmax(0,1.4fr)_minmax(0,0.8fr)_minmax(0,0.8fr)_minmax(0,0.8fr)_minmax(0,0.8fr)] items-center gap-2 px-3 py-2 text-sm">
                  <div className="flex items-center justify-center">
                    <Checkbox checked={selected.has(accountSelector(account))} onCheckedChange={(checked) => toggleEmail(accountSelector(account), Boolean(checked))} disabled={state?.enabled} />
                  </div>
                  <div className="min-w-0">
                    <div className="truncate font-semibold text-slate-900 font-mono text-xs">{account.email}</div>
                    <div className="truncate text-[10px] text-slate-500 font-mono mt-0.5">{account.k12_team_id || account.workspace_id || "-"}</div>
                  </div>
                  <div>{statusBadge(account.k12_join_status)}</div>
                  <div>{statusBadge(account.sub2api_import_status)}</div>
                  <div>{statusBadge(account.cpa_import_status)}</div>
                  <div>{statusBadge(account.sub2api_health_status || account.cpa_health_status)}</div>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section className="flex min-h-0 min-w-0 flex-col p-4 bg-slate-50/50">
          <div className="flex items-start justify-between gap-3">
            <div>
              <h3 className="text-lg font-semibold tracking-tight text-slate-950">{"K12 任务日志"}</h3>
              <p className="mt-1 text-sm text-slate-500">{"显示重登录、K12 加入、sub2api / CPA 入库和测活过程。"}</p>
            </div>
            <Badge variant="secondary" className="rounded-md">{logs.length} {"条"}</Badge>
          </div>
          <div className="mt-4 h-[620px] overflow-y-auto rounded-xl border border-slate-200 bg-slate-950 p-3 font-mono text-xs leading-6 text-slate-100 shadow-[inset_0_1px_4px_rgba(0,0,0,0.3)]">
            {logs.length === 0 ? (
              <div className="text-slate-500 text-center py-12">{"暂无日志"}</div>
            ) : logs.slice().reverse().map((item, index) => (
              <div key={`${item.time}-${index}`} className={item.level === "red" ? "text-rose-300" : item.level === "green" ? "text-emerald-300" : item.level === "yellow" ? "text-amber-300" : "text-slate-200"}>
                <span className="text-slate-500">{new Date(item.time).toLocaleTimeString()}</span>
                <span className="pl-2">{item.text}</span>
              </div>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}
