"use client";

import { Activity, AlertTriangle, CheckCircle2, Cpu, Gauge, ListTodo, LoaderCircle, Play, RefreshCw, RotateCw, Save, ServerCog, Square, Wifi } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { controlGrokSolver, fetchGrokSolver, updateRegisterConfig, type GrokRegisterConfig, type GrokSolverStatus } from "@/lib/api";

import { useSettingsStore } from "../store";

function statusPresentation(status: GrokSolverStatus | null) {
  if (!status?.reachable) return { label: "已停止", detail: "本地端口未监听", variant: "secondary" as const, tone: "text-slate-600" };
  if (status.browser_ready === false) return { label: "浏览器异常", detail: "HTTP 服务可达，但浏览器驱动不可用", variant: "danger" as const, tone: "text-rose-700" };
  if (status.managed && status.running && status.browser_ready === true) return { label: "运行正常", detail: "主程序已接管，浏览器池可用", variant: "success" as const, tone: "text-emerald-700" };
  if (!status.managed) return { label: "外部进程占用", detail: status.health_supported ? "Solver 可用，但不是由当前服务启动" : "仅确认端口可达，无法验证浏览器状态", variant: "warning" as const, tone: "text-amber-700" };
  return { label: "启动中", detail: "正在等待浏览器池完成初始化", variant: "info" as const, tone: "text-sky-700" };
}

function Metric({ icon: Icon, label, value, detail }: { icon: typeof Cpu; label: string; value: string; detail: string }) {
  return (
    <div className="min-w-0 border-l border-slate-200 pl-4 first:border-l-0 first:pl-0">
      <div className="flex items-center gap-1.5 text-xs font-semibold text-slate-500"><Icon className="size-3.5" />{label}</div>
      <div className="mt-1 truncate text-lg font-semibold tabular-nums text-slate-950">{value}</div>
      <div className="mt-0.5 truncate text-[11px] text-slate-500" title={detail}>{detail}</div>
    </div>
  );
}

export function CaptchaConfigCard() {
  const config = useSettingsStore((state) => state.registerConfig);
  const setRegisterConfig = useSettingsStore((state) => state.setRegisterConfig);
  const loadRegister = useSettingsStore((state) => state.loadRegister);
  const [status, setStatus] = useState<GrokSolverStatus | null>(config?.grok?.solver_status || null);
  const [draft, setDraft] = useState<GrokRegisterConfig | null>(config?.grok || null);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState<"start" | "stop" | "restart" | "refresh" | "save" | "">("");

  const refreshStatus = useCallback(async (silent = false) => {
    if (!silent) setBusy("refresh");
    try {
      const data = await fetchGrokSolver();
      setStatus(data.grok_solver);
    } catch (error) {
      if (!silent) toast.error(error instanceof Error ? error.message : "读取 Solver 状态失败");
    } finally {
      if (!silent) setBusy("");
    }
  }, []);

  useEffect(() => {
    void refreshStatus(true);
    const timer = window.setInterval(() => void refreshStatus(true), 5000);
    return () => window.clearInterval(timer);
  }, [refreshStatus]);

  useEffect(() => {
    if (!dirty && config?.grok) setDraft(config.grok);
  }, [config?.grok, dirty]);

  if (!config?.grok || !draft) return null;
  const grok = draft;
  const disabled = Boolean(config.enabled);
  const state = statusPresentation(status);
  const health = status?.health || {};
  const updateDraft = (updates: Partial<GrokRegisterConfig>) => {
    setDraft((current) => current ? { ...current, ...updates } : current);
    setDirty(true);
  };
  const save = async () => {
    setBusy("save");
    try {
      const data = await updateRegisterConfig({ grok });
      setRegisterConfig(data.register);
      setDraft(data.register.grok || grok);
      setDirty(false);
      toast.success("打码配置已保存");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存打码配置失败");
    } finally {
      setBusy("");
    }
  };
  const control = async (action: "start" | "stop" | "restart") => {
    setBusy(action);
    try {
      const data = await controlGrokSolver(action, { url: grok.local_solver_url, threads: grok.solver_threads });
      setStatus(data.grok_solver);
      await loadRegister(true);
      toast.success(action === "start" ? "本地 Solver 已启动" : action === "stop" ? "本地 Solver 已停止" : `本地 Solver 已按 ${grok.solver_threads} 并发重启`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Solver 操作失败");
    } finally {
      setBusy("");
    }
  };

  return (
    <section className="overflow-hidden border border-slate-200 bg-white shadow-sm">
      <header className="flex flex-wrap items-start justify-between gap-4 border-b border-slate-200 bg-slate-50 px-5 py-4">
        <div className="flex items-start gap-3">
          <span className="flex size-11 shrink-0 items-center justify-center bg-slate-950 text-emerald-300"><ServerCog className="size-5" /></span>
          <div>
            <h3 className="text-base font-semibold text-slate-950">Turnstile Solver</h3>
            <p className="mt-1 text-xs leading-5 text-slate-500">集中管理 Grok 注册使用的验证码服务、浏览器并发与运行状态。</p>
          </div>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          <Badge variant={state.variant} className="h-8 rounded-md px-3">{state.label}</Badge>
          <Button variant="outline" size="sm" className="h-9 bg-white" onClick={() => void refreshStatus()} disabled={Boolean(busy)} title="刷新 Solver 状态">
            <RefreshCw className={busy === "refresh" ? "animate-spin" : ""} />刷新
          </Button>
          {grok.captcha_provider === "local" && !status?.reachable ? (
            <Button size="sm" className="h-9 bg-emerald-600 text-white hover:bg-emerald-700" onClick={() => void control("start")} disabled={Boolean(busy) || disabled || dirty} title={dirty ? "请先保存打码配置" : "启动本地 Solver"}>
              {busy === "start" ? <LoaderCircle className="animate-spin" /> : <Play />}启动
            </Button>
          ) : null}
          {grok.captcha_provider === "local" && status?.reachable ? (
            <>
              <Button variant="outline" size="sm" className="h-9 bg-white" onClick={() => void control("restart")} disabled={Boolean(busy) || disabled || dirty} title={dirty ? "请先保存打码配置" : "按已保存配置重启"}>
                {busy === "restart" ? <LoaderCircle className="animate-spin" /> : <RotateCw />}按配置重启
              </Button>
              <Button variant="outline" size="sm" className="h-9 border-rose-200 bg-white text-rose-700 hover:bg-rose-50" onClick={() => void control("stop")} disabled={Boolean(busy) || disabled}>
                {busy === "stop" ? <LoaderCircle className="animate-spin" /> : <Square />}停止
              </Button>
            </>
          ) : null}
        </div>
      </header>

      <div className={`border-b px-5 py-3 text-sm ${status?.browser_ready === false ? "border-rose-200 bg-rose-50" : !status?.managed && status?.reachable ? "border-amber-200 bg-amber-50" : "border-emerald-200 bg-emerald-50"}`}>
        <div className={`flex items-center gap-2 font-semibold ${state.tone}`}>
          {status?.browser_ready === false || (!status?.managed && status?.reachable) ? <AlertTriangle className="size-4" /> : <CheckCircle2 className="size-4" />}
          {state.detail}
        </div>
        {!status?.managed && status?.reachable ? <p className="mt-1 text-xs leading-5 text-amber-800">“按配置重启”会先关闭已确认属于本项目的旧 Solver，再按下方并发重新启动并接管。</p> : null}
      </div>

      <div className="grid gap-4 border-b border-slate-200 px-5 py-4 sm:grid-cols-2 xl:grid-cols-6">
        <Metric icon={Activity} label="服务状态" value={status?.reachable ? "在线" : "离线"} detail={status?.state || "stopped"} />
        <Metric icon={Cpu} label="实际并发" value={String(status?.actual_threads ?? health.configured_threads ?? "-")} detail={`配置值 ${grok.solver_threads}`} />
        <Metric icon={Gauge} label="浏览器池" value={`${health.available_browsers ?? "-"}/${health.connected_browsers ?? "-"}`} detail="空闲 / 已连接" />
        <Metric icon={ListTodo} label="任务槽位" value={`${health.active_tasks ?? "-"}/${health.max_inflight_tasks ?? "-"}`} detail={`排队 ${health.queued_tasks ?? 0}`} />
        <Metric icon={Wifi} label="监听 PID" value={String(status?.listener_pid || status?.pid || "-")} detail={status?.managed ? "主程序管理" : "外部或未知"} />
        <Metric icon={ServerCog} label="浏览器" value={health.browser_type || status?.browser_type || "Camoufox"} detail={status?.health_supported ? "支持健康检查" : "旧版进程无健康检查"} />
      </div>

      <div className="space-y-5 p-5">
        <div>
          <div className="mb-2 text-xs font-semibold text-slate-600">打码方式</div>
          <div className="inline-flex overflow-hidden border border-slate-200 bg-slate-50 p-0.5">
            <button type="button" onClick={() => updateDraft({ captcha_provider: "local" })} disabled={disabled} className={`h-9 px-4 text-sm font-semibold ${grok.captcha_provider === "local" ? "bg-slate-950 text-white" : "text-slate-600 hover:bg-white"}`}>本地 Solver</button>
            <button type="button" onClick={() => updateDraft({ captcha_provider: "yescaptcha" })} disabled={disabled} className={`h-9 px-4 text-sm font-semibold ${grok.captcha_provider === "yescaptcha" ? "bg-slate-950 text-white" : "text-slate-600 hover:bg-white"}`}>YesCaptcha</button>
          </div>
        </div>

        {grok.captcha_provider === "local" ? (
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
            <label className="space-y-1.5 md:col-span-2"><span className="text-xs font-semibold text-slate-600">本地 Solver 地址</span><Input value={grok.local_solver_url} disabled={disabled} onChange={(event) => updateDraft({ local_solver_url: event.target.value })} /></label>
            <label className="space-y-1.5"><span className="text-xs font-semibold text-slate-600">浏览器并发</span><Input type="number" min={1} max={4} value={String(grok.solver_threads)} disabled={disabled} onChange={(event) => updateDraft({ solver_threads: Math.max(1, Math.min(4, Number(event.target.value) || 1)) })} /></label>
            <label className="space-y-1.5"><span className="text-xs font-semibold text-slate-600">打码超时/秒</span><Input type="number" min={30} max={300} value={String(grok.captcha_timeout)} disabled={disabled} onChange={(event) => updateDraft({ captcha_timeout: Math.max(30, Math.min(300, Number(event.target.value) || 120)) })} /></label>
            <label className="flex min-h-11 items-center gap-2 border border-slate-200 bg-slate-50 px-3 md:col-span-2 xl:col-span-4"><Checkbox checked={grok.solver_auto_start} disabled={disabled} onCheckedChange={(checked) => updateDraft({ solver_auto_start: Boolean(checked) })} /><span><span className="block text-sm font-semibold text-slate-900">注册时自动启动并校验 Solver</span><span className="mt-0.5 block text-xs text-slate-500">发现旧进程、浏览器异常或实际并发不一致时，按保存配置重启。</span></span></label>
          </div>
        ) : (
          <div className="grid gap-4 md:grid-cols-2">
            <label className="space-y-1.5"><span className="text-xs font-semibold text-slate-600">YesCaptcha API Key</span><Input type="password" value={grok.yescaptcha_key === "***" ? "" : grok.yescaptcha_key || ""} placeholder={grok.has_yescaptcha_key ? "已配置，留空保持不变" : "clientKey"} disabled={disabled} onChange={(event) => updateDraft({ yescaptcha_key: event.target.value })} /></label>
            <label className="space-y-1.5"><span className="text-xs font-semibold text-slate-600">API Endpoint</span><Input value={grok.yescaptcha_endpoint || ""} placeholder="留空使用默认地址" disabled={disabled} onChange={(event) => updateDraft({ yescaptcha_endpoint: event.target.value })} /></label>
            <label className="space-y-1.5"><span className="text-xs font-semibold text-slate-600">打码超时/秒</span><Input type="number" min={30} max={300} value={String(grok.captcha_timeout)} disabled={disabled} onChange={(event) => updateDraft({ captcha_timeout: Math.max(30, Math.min(300, Number(event.target.value) || 120)) })} /></label>
          </div>
        )}

        <div className="flex items-center justify-end gap-3 border-t border-slate-200 pt-4">
          {dirty ? <span className="text-xs text-amber-700">有未保存更改</span> : null}
          <Button className="h-10 bg-emerald-600 text-white hover:bg-emerald-700" onClick={() => void save()} disabled={Boolean(busy) || disabled || !dirty}>
            {busy === "save" ? <LoaderCircle className="animate-spin" /> : <Save />}保存打码配置
          </Button>
        </div>
      </div>
    </section>
  );
}
