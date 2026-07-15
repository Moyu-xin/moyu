"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity,
  CheckCircle2,
  DatabaseZap,
  Gauge,
  GitBranch,
  HardDrive,
  KeyRound,
  LoaderCircle,
  MemoryStick,
  RefreshCw,
  RotateCcw,
  Save,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import {
  checkSystemGit,
  cleanupSystem,
  fetchSystemMetrics,
  fetchSystemOverview,
  resetSystemSettings,
  rotateSystemAuthKey,
  updateSystemGit,
  updateSystemSettings,
  type SystemGitStatus,
  type SystemMemorySample,
  type SystemOverview,
  type SystemSettings,
} from "@/lib/api";
import { getStoredAuthKey, setStoredAuthKey } from "@/store/auth";

const DEFAULT_SETTINGS: SystemSettings = {
  poll_interval_seconds: 3,
  memory_warning_percent: 80,
  memory_baseline_mb: 220,
  memory_per_worker_mb: 96,
};

const CLEANUP_OPTIONS = [
  { key: "logs", label: "运行日志", detail: "清空 logs 中的日志内容" },
  { key: "python_cache", label: "Python 缓存", detail: "清理 __pycache__ 与测试缓存" },
  { key: "frontend_cache", label: "前端构建缓存", detail: "清理 .next、out 与类型缓存" },
] as const;

function formatBytes(value: number | undefined) {
  const bytes = Number(value || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function formatUptime(seconds: number | undefined) {
  const total = Math.max(0, Number(seconds || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  return hours ? `${hours} 小时 ${minutes} 分` : `${minutes} 分钟`;
}

function riskLabel(risk: string | undefined) {
  if (risk === "high") return "高风险";
  if (risk === "medium") return "需留意";
  return "稳定";
}

function riskClass(risk: string | undefined) {
  if (risk === "high") return "border-rose-200 bg-rose-50 text-rose-700";
  if (risk === "medium") return "border-amber-200 bg-amber-50 text-amber-700";
  return "border-emerald-200 bg-emerald-50 text-emerald-700";
}

function MemoryChart({ samples }: { samples: SystemMemorySample[] }) {
  const points = useMemo(() => {
    const data = samples.slice(-40);
    if (!data.length) return "";
    const max = Math.max(1, ...data.map((item) => item.process.rss_mb));
    return data
      .map((item, index) => {
        const x = data.length === 1 ? 100 : (index / (data.length - 1)) * 100;
        const y = 34 - (item.process.rss_mb / max) * 30;
        return `${x.toFixed(2)},${y.toFixed(2)}`;
      })
      .join(" ");
  }, [samples]);

  return (
    <div className="h-24 w-full overflow-hidden border-y border-slate-100 bg-slate-50/70">
      <svg viewBox="0 0 100 36" preserveAspectRatio="none" className="h-full w-full" role="img" aria-label="进程内存趋势">
        <polyline points={points} fill="none" stroke="rgb(16 185 129)" strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
      </svg>
    </div>
  );
}

function GitSummary({ git }: { git: SystemGitStatus }) {
  return (
    <div className="grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-4">
      <div><p className="text-xs text-slate-500">分支</p><p className="mt-1 truncate font-semibold text-slate-900">{git.branch || "未初始化"}</p></div>
      <div><p className="text-xs text-slate-500">提交</p><p className="mt-1 font-mono font-semibold text-slate-900">{git.commit || "-"}</p></div>
      <div><p className="text-xs text-slate-500">远端差异</p><p className="mt-1 font-semibold text-slate-900">领先 {git.ahead || 0} / 落后 {git.behind || 0}</p></div>
      <div><p className="text-xs text-slate-500">工作区</p><p className={`mt-1 font-semibold ${git.dirty ? "text-amber-700" : "text-emerald-700"}`}>{git.dirty ? `${git.dirty_count || 0} 项未提交` : "干净"}</p></div>
    </div>
  );
}

export function SystemSettingsCard() {
  const [overview, setOverview] = useState<SystemOverview | null>(null);
  const [draft, setDraft] = useState<SystemSettings>(DEFAULT_SETTINGS);
  const [currentKey, setCurrentKey] = useState("");
  const [newKey, setNewKey] = useState("");
  const [confirmKey, setConfirmKey] = useState("");
  const [cleanupCategories, setCleanupCategories] = useState<string[]>(CLEANUP_OPTIONS.map((item) => item.key));
  const [busy, setBusy] = useState("");

  const loadOverview = useCallback(async () => {
    const data = await fetchSystemOverview();
    setOverview(data);
    setDraft(data.settings);
  }, []);

  useEffect(() => {
    void Promise.all([loadOverview(), getStoredAuthKey().then(setCurrentKey)]).catch((error) => toast.error(error instanceof Error ? error.message : "系统状态加载失败"));
  }, [loadOverview]);

  useEffect(() => {
    const seconds = overview?.settings.poll_interval_seconds || 3;
    const timer = window.setInterval(() => {
      void fetchSystemMetrics()
        .then(({ memory }) => setOverview((value) => value ? { ...value, memory } : value))
        .catch(() => undefined);
    }, seconds * 1000);
    return () => window.clearInterval(timer);
  }, [overview?.settings.poll_interval_seconds]);

  const run = async (name: string, operation: () => Promise<void>) => {
    setBusy(name);
    try {
      await operation();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "操作失败");
    } finally {
      setBusy("");
    }
  };

  const saveSettings = () => run("settings", async () => {
    const result = await updateSystemSettings(draft);
    setOverview((value) => value ? { ...value, settings: result.settings } : value);
    setDraft(result.settings);
    toast.success("系统配置已热更新");
  });

  const rotateKey = () => run("auth", async () => {
    await rotateSystemAuthKey(currentKey, newKey, confirmKey);
    await setStoredAuthKey(newKey.trim());
    setCurrentKey(newKey.trim());
    setNewKey("");
    setConfirmKey("");
    toast.success("登录密钥已立即生效");
  });

  const clean = () => {
    if (!cleanupCategories.length) return toast.error("至少选择一类缓存");
    if (!window.confirm("确认清理所选日志和缓存？账号、邮箱、代理库存不会被删除。")) return;
    void run("cleanup", async () => {
      const result = await cleanupSystem(cleanupCategories);
      setOverview((value) => value ? { ...value, cleanup: result.remaining } : value);
      if (result.errors.length) toast.warning(`已处理 ${result.removed_files} 个文件，${result.errors.length} 项失败`);
      else toast.success(`已释放 ${formatBytes(result.removed_bytes)}`);
    });
  };

  const reset = () => {
    if (!window.confirm("确认一键重置系统设置？登录密钥、账号、邮箱和代理库存会保留。")) return;
    void run("reset", async () => {
      const result = await resetSystemSettings();
      setDraft(result.settings);
      setOverview((value) => value ? { ...value, settings: result.settings } : value);
      toast.success("系统设置已恢复默认值并立即生效");
    });
  };

  const checkGit = () => run("git-check", async () => {
    const { git } = await checkSystemGit();
    setOverview((value) => value ? { ...value, git } : value);
    toast.success(git.update_available ? `发现 ${git.behind || 0} 个远端更新` : "当前已是最新版本");
  });

  const updateGit = () => {
    if (!window.confirm("确认执行 Git 快进更新？更新完成后需要重启项目。")) return;
    void run("git-update", async () => {
      const result = await updateSystemGit();
      setOverview((value) => value ? { ...value, git: result.git } : value);
      toast.success(result.updated ? "更新完成，请重启项目" : "当前已是最新版本");
    });
  };

  if (!overview) {
    return <Card className="min-h-72 items-center justify-center"><LoaderCircle className="size-5 animate-spin text-slate-400" /></Card>;
  }

  const memory = overview.memory;
  const cleanup = overview.cleanup;

  return (
    <Card>
      <CardHeader className="border-b border-slate-200/80">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <CardTitle className="flex items-center gap-2 text-lg"><Gauge className="size-5 text-emerald-600" />系统运行中心</CardTitle>
            <p className="mt-2 text-sm text-slate-500">资源监控、密钥、维护和版本更新集中管理</p>
          </div>
          <div className="flex items-center gap-2">
            <Badge variant="outline" className="border-emerald-200 bg-emerald-50 text-emerald-700"><span className="mr-1.5 size-1.5 rounded-full bg-emerald-500" />实时</Badge>
            <Badge variant="outline" className="border-sky-200 bg-sky-50 text-sky-700"><CheckCircle2 className="mr-1 size-3.5" />热更新</Badge>
            <Button variant="outline" size="icon" title="刷新系统状态" onClick={() => void run("refresh", loadOverview)} disabled={Boolean(busy)}><RefreshCw className={busy === "refresh" ? "animate-spin" : ""} /></Button>
          </div>
        </div>
      </CardHeader>

      <CardContent className="p-0">
        <section className="border-b border-slate-200/80 px-6 py-5">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <div><h3 className="font-semibold text-slate-950">内存与负载</h3><p className="mt-1 text-sm text-slate-500">按当前注册线程数估算峰值，每 {overview.settings.poll_interval_seconds} 秒刷新</p></div>
            <Badge variant="outline" className={riskClass(memory.prediction.risk)}>{riskLabel(memory.prediction.risk)}</Badge>
          </div>
          <div className="grid border border-slate-200 sm:grid-cols-2 lg:grid-cols-4">
            {[
              { icon: MemoryStick, label: "进程内存", value: `${memory.process.rss_mb.toFixed(1)} MB`, detail: `已运行 ${formatUptime(memory.process.uptime_seconds)}` },
              { icon: Activity, label: "预测峰值", value: `${memory.prediction.predicted_peak_mb.toFixed(1)} MB`, detail: `${memory.prediction.registration_threads} 个注册线程` },
              { icon: Gauge, label: "系统占用", value: `${memory.system.percent.toFixed(1)}%`, detail: `预测 ${memory.prediction.projected_system_percent.toFixed(1)}%` },
              { icon: HardDrive, label: "可用内存", value: `${(memory.system.available_mb / 1024).toFixed(1)} GB`, detail: `总计 ${(memory.system.total_mb / 1024).toFixed(1)} GB` },
            ].map((item, index) => {
              const Icon = item.icon;
              const borders = index === 1 ? "border-t border-slate-200 sm:border-l sm:border-t-0" : index === 2 ? "border-t border-slate-200 lg:border-l lg:border-t-0" : index === 3 ? "border-t border-slate-200 sm:border-l lg:border-t-0" : "";
              return <div key={item.label} className={`min-h-28 p-4 ${borders}`}><Icon className="size-4 text-emerald-600" /><p className="mt-3 text-xs text-slate-500">{item.label}</p><p className="mt-1 text-xl font-semibold text-slate-950">{item.value}</p><p className="mt-1 text-xs text-slate-500">{item.detail}</p></div>;
            })}
          </div>
          <MemoryChart samples={memory.history} />
          <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <label className="text-sm text-slate-600">刷新间隔（秒）<Input type="number" min={2} max={30} value={draft.poll_interval_seconds} onChange={(event) => setDraft({ ...draft, poll_interval_seconds: Number(event.target.value) })} className="mt-1.5" /></label>
            <label className="text-sm text-slate-600">预警阈值（%）<Input type="number" min={50} max={98} value={draft.memory_warning_percent} onChange={(event) => setDraft({ ...draft, memory_warning_percent: Number(event.target.value) })} className="mt-1.5" /></label>
            <label className="text-sm text-slate-600">基础占用（MB）<Input type="number" min={32} max={8192} value={draft.memory_baseline_mb} onChange={(event) => setDraft({ ...draft, memory_baseline_mb: Number(event.target.value) })} className="mt-1.5" /></label>
            <label className="text-sm text-slate-600">每线程预算（MB）<Input type="number" min={8} max={2048} value={draft.memory_per_worker_mb} onChange={(event) => setDraft({ ...draft, memory_per_worker_mb: Number(event.target.value) })} className="mt-1.5" /></label>
          </div>
          <div className="mt-3 flex justify-end"><Button onClick={() => void saveSettings()} disabled={Boolean(busy)}><Save />保存并热更新</Button></div>
        </section>

        <section className="border-b border-slate-200/80 px-6 py-5">
          <div className="mb-4 flex items-center gap-2"><KeyRound className="size-4 text-emerald-600" /><h3 className="font-semibold text-slate-950">登录密钥</h3><Badge variant="outline">{overview.auth.source === "environment" ? "环境变量管理" : "本地配置"}</Badge></div>
          <div className="grid gap-3 lg:grid-cols-3">
            <label className="text-sm text-slate-600">当前密钥<Input type="password" autoComplete="current-password" value={currentKey} onChange={(event) => setCurrentKey(event.target.value)} className="mt-1.5" disabled={!overview.auth.rotatable} /></label>
            <label className="text-sm text-slate-600">新密钥<Input type="password" autoComplete="new-password" value={newKey} onChange={(event) => setNewKey(event.target.value)} className="mt-1.5" disabled={!overview.auth.rotatable} /></label>
            <label className="text-sm text-slate-600">确认新密钥<Input type="password" autoComplete="new-password" value={confirmKey} onChange={(event) => setConfirmKey(event.target.value)} className="mt-1.5" disabled={!overview.auth.rotatable} /></label>
          </div>
          <div className="mt-3 flex flex-wrap items-center justify-between gap-3 text-xs text-slate-500"><span>{overview.auth.rotatable ? "修改后当前浏览器会同步新密钥，无需重启。" : "当前由 CHATGPT2API_AUTH_KEY 管理，请修改环境变量后重启。"}</span><Button variant="outline" onClick={() => void rotateKey()} disabled={!overview.auth.rotatable || !currentKey || !newKey || !confirmKey || Boolean(busy)}><ShieldCheck />更换密钥</Button></div>
        </section>

        <section className="border-b border-slate-200/80 px-6 py-5">
          <div className="mb-4 flex items-center gap-2"><Trash2 className="size-4 text-emerald-600" /><h3 className="font-semibold text-slate-950">日志与缓存</h3><Badge variant="outline">{formatBytes(cleanup.total_bytes)}</Badge></div>
          <div className="grid gap-3 md:grid-cols-3">
            {CLEANUP_OPTIONS.map((item) => {
              const status = cleanup.categories[item.key];
              return <label key={item.key} className="flex min-h-20 cursor-pointer items-start gap-3 border border-slate-200 p-3"><Checkbox checked={cleanupCategories.includes(item.key)} onCheckedChange={(checked) => setCleanupCategories((values) => checked ? [...values, item.key] : values.filter((value) => value !== item.key))} /><span><span className="block text-sm font-semibold text-slate-900">{item.label}</span><span className="mt-1 block text-xs leading-5 text-slate-500">{item.detail}<br />{status?.files || 0} 个文件 / {formatBytes(status?.bytes)}</span></span></label>;
            })}
          </div>
          <div className="mt-3 flex justify-end"><Button variant="outline" onClick={clean} disabled={Boolean(busy)}>{busy === "cleanup" ? <LoaderCircle className="animate-spin" /> : <DatabaseZap />}清理所选项</Button></div>
        </section>

        <section className="border-b border-slate-200/80 px-6 py-5">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3"><div className="flex items-center gap-2"><GitBranch className="size-4 text-emerald-600" /><h3 className="font-semibold text-slate-950">Git 更新</h3></div><Badge variant="outline" className={overview.git.dirty ? "border-amber-200 bg-amber-50 text-amber-700" : "border-emerald-200 bg-emerald-50 text-emerald-700"}>{overview.git.update_supported ? "可安全更新" : "更新已锁定"}</Badge></div>
          <GitSummary git={overview.git} />
          {overview.git.reason ? <p className="mt-3 text-xs text-amber-700">{overview.git.reason}</p> : null}
          <p className="mt-2 truncate text-xs text-slate-500" title={overview.git.remote || ""}>{overview.git.remote || "尚未配置 origin"}</p>
          <div className="mt-4 flex justify-end gap-2"><Button variant="outline" onClick={() => void checkGit()} disabled={Boolean(busy) || !overview.git.is_repo}><RefreshCw className={busy === "git-check" ? "animate-spin" : ""} />检测更新</Button><Button onClick={updateGit} disabled={Boolean(busy) || !overview.git.update_supported || !overview.git.update_available}><GitBranch />快进更新</Button></div>
        </section>

        <section className="px-6 py-5">
          <div className="flex flex-wrap items-center justify-between gap-4"><div><div className="flex items-center gap-2"><RotateCcw className="size-4 text-rose-600" /><h3 className="font-semibold text-slate-950">一键重置</h3></div><p className="mt-2 text-sm text-slate-500">恢复本页系统参数默认值；保留登录密钥、账号、邮箱与代理库存。</p></div><Button variant="destructive" onClick={reset} disabled={Boolean(busy)}>{busy === "reset" ? <LoaderCircle className="animate-spin" /> : <RotateCcw />}一键重置</Button></div>
        </section>
      </CardContent>
    </Card>
  );
}
