"use client";

import { Activity, Clock3, Terminal } from "lucide-react";

import { Badge } from "@/components/ui/badge";

import { useSettingsStore } from "../store";

function formatSeconds(value: unknown) {
  const number = Number(value || 0);
  if (!Number.isFinite(number) || number <= 0) return "0 秒";
  if (number < 60) return `${number.toFixed(number % 1 ? 1 : 0)} 秒`;
  const minutes = Math.floor(number / 60);
  const seconds = Math.round(number % 60);
  return `${minutes} 分 ${seconds} 秒`;
}

function logLevel(level?: string) {
  if (level === "red") return { label: "ERROR", className: "border-rose-500/25 bg-rose-500/10 text-rose-300" };
  if (level === "green") return { label: "SUCCESS", className: "border-emerald-500/25 bg-emerald-500/10 text-emerald-300" };
  if (level === "yellow") return { label: "WARNING", className: "border-amber-500/25 bg-amber-500/10 text-amber-300" };
  return { label: "INFO", className: "border-sky-500/25 bg-sky-500/10 text-sky-300" };
}

export function RegisterCard() {
  const config = useSettingsStore((state) => state.registerConfig);
  const isLoading = useSettingsStore((state) => state.isLoadingRegister);

  if (isLoading) {
    return (
      <div className="flex min-h-[560px] flex-col gap-3 rounded-2xl border border-slate-200 bg-white p-4">
        <div className="grid grid-cols-4 gap-2 xl:grid-cols-8">
          {Array.from({ length: 8 }).map((_, index) => <div key={index} className="h-14 animate-pulse rounded-lg bg-slate-100" />)}
        </div>
        <div className="flex-1 animate-pulse rounded-xl bg-slate-950" />
      </div>
    );
  }

  if (!config) return null;

  const stats = config.stats || { success: 0, fail: 0, done: 0, running: 0, threads: config.threads };
  const logs = config.logs || [];
  const savedAccounts = stats.stored_total ?? stats.registered_total ?? stats.current_available ?? 0;
  const statItems = [
    ["成功", stats.success || 0],
    ["失败", stats.fail || 0],
    ["完成", stats.done || 0],
    ["运行 / 线程", `${stats.running || 0} / ${stats.threads || config.threads}`],
    ["成功率", `${stats.success_rate || 0}%`],
    ["平均单号", formatSeconds(stats.avg_seconds)],
    ["已保存账号", savedAccounts],
    ["目标数量", config.total],
  ];

  return (
    <section className="flex min-h-[620px] flex-col overflow-hidden rounded-2xl border border-slate-200/90 bg-white shadow-sm lg:h-[calc(100dvh-248px)]">
      <div className="shrink-0 border-b border-slate-200 bg-slate-50/80 px-4 py-3">
        <div className="grid grid-cols-2 gap-y-3 sm:grid-cols-4 2xl:grid-cols-8">
            {statItems.map(([label, value]) => (
              <div key={String(label)} className="border-l border-slate-200 px-3 first:border-l-0 first:pl-0">
                <div className="text-[11px] font-medium text-slate-500">{label}</div>
                <div className="mt-1 truncate text-base font-semibold tabular-nums text-slate-950">{value}</div>
              </div>
            ))}
        </div>
      </div>

      <div className="flex min-h-0 flex-1 flex-col p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <Terminal className="size-5 text-slate-700" />
            <h3 className="text-lg font-semibold text-slate-950">实时运行日志</h3>
            <Badge variant={config.enabled ? "success" : "secondary"} className="rounded-md">{config.enabled ? "运行中" : "已停止"}</Badge>
          </div>
          <div className="flex items-center gap-3 text-xs text-slate-500">
            <span className="inline-flex items-center gap-1.5"><Clock3 className="size-3.5" />{formatSeconds(stats.elapsed_seconds)}</span>
            <span>{logs.length} 条</span>
          </div>
        </div>

        <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl border border-slate-800 bg-[#0b1220] shadow-[0_22px_45px_-32px_rgba(15,23,42,0.9)]">
          <div className="flex h-11 shrink-0 items-center justify-between border-b border-slate-700/70 bg-slate-800 px-4">
            <div className="flex items-center gap-2 text-xs font-medium text-slate-300"><Activity className="size-4 text-emerald-400" />register-runtime</div>
            <span className="text-[11px] text-slate-500">live</span>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3 font-mono text-xs leading-6 text-slate-200">
            {logs.length === 0 ? (
              <div className="flex h-full min-h-[360px] items-center justify-center text-slate-500">暂无运行日志</div>
            ) : (
              logs.slice().reverse().map((item, index) => {
                const tone = logLevel(item.level);
                return (
                  <div key={`${item.time}-${index}`} className="grid min-w-0 grid-cols-[72px_76px_minmax(0,1fr)] items-start gap-2 border-b border-slate-800/80 py-1.5 last:border-0">
                    <span className="tabular-nums text-slate-500">{new Date(item.time).toLocaleTimeString()}</span>
                    <span className={`inline-flex h-6 items-center justify-center rounded border px-1.5 text-[10px] font-semibold ${tone.className}`}>{tone.label}</span>
                    <span className="min-w-0 whitespace-pre-wrap break-words text-slate-200">{item.text}</span>
                  </div>
                );
              })
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
