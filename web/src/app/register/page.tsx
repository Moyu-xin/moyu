"use client";

import { useEffect, useRef, useState, type CSSProperties } from "react";
import {
  Activity,
  BellRing,
  Boxes,
  CheckCircle2,
  Clock3,
  GraduationCap,
  Link2,
  ListChecks,
  LoaderCircle,
  Mail,
  Mails,
  Network,
  Play,
  Power,
  RefreshCw,
  RotateCcw,
  Save,
  Settings,
  Square,
  Trash2,
  UserPlus,
  Warehouse,
} from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { BrandMark } from "@/components/brand-mark";
import webConfig from "@/constants/common-env";
import { checkSystemGit, fetchVersion, prefetchManagementData, restartSystem, type RegisterConfig, type SystemGitStatus } from "@/lib/api";
import { useAuthGuard } from "@/lib/use-auth-guard";
import { getStoredAuthKey } from "@/store/auth";

import { AccountPoolCard } from "./components/account-pool-card";
import { GarbageHealthCard } from "./components/garbage-health-card";
import { K12WorkspaceCard } from "./components/k12-workspace-card";
import { MailboxPoolCard } from "./components/mailbox-pool-card";
import { OutlookMailboxInventoryCard } from "./components/outlook-mailbox-inventory-card";
import { PartialAccountCard } from "./components/partial-account-card";
import { PlusLinkCard } from "./components/plus-link-card";
import { ProxyPoolCard } from "./components/proxy-pool-card";
import { RegisterCard } from "./components/register-card";
import { SystemSettingsCard } from "./components/system-settings-card";
import { WarehouseConfigCard } from "./components/warehouse-config-card";
import { WarehouseManagerCard } from "./components/warehouse-manager-card";
import { useSettingsStore } from "./store";

type ManageSection = "register" | "proxy" | "mail" | "accounts" | "garbage" | "warehouse" | "k12" | "plus" | "system";
type MailView = "inventory" | "config";
type AccountView = "inventory" | "partials";
type WarehouseView = "config" | "management";

const T = {
  app: "摸鱼机",
  running: "运行中",
  stopped: "已停止",
  currentModule: "当前工作区",
};

const sections: Array<{
  key: ManageSection;
  title: string;
  description: string;
  detail: string;
  icon: typeof UserPlus;
}> = [
  { key: "register", title: "注册", description: "注册参数与实时日志", detail: "从微软邮箱库存或动态邮箱提供商领取邮箱，完成后写入账号库存。", icon: UserPlus },
  { key: "proxy", title: "代理配置", description: "订阅、节点池、Mihomo", detail: "维护注册与测活使用的出口代理。", icon: Network },
  { key: "mail", title: "邮箱", description: "库存与提供商配置", detail: "管理微软邮箱库存、分组、收件和令牌，并维护 Gmail、Cloudflare 等邮箱提供商。", icon: Mail },
  { key: "accounts", title: "账号", description: "成品与半成品库存", detail: "管理成品账号、半成品账号、OAuth、测活、额度以及中转仓库导入。", icon: Boxes },
  { key: "garbage", title: "捡垃圾", description: "JSON / CPA / sub2api 扫描测活", detail: "扫描用户选择目录中的 GPT 账号 JSON，自动识别 CPA 与 sub2api 格式，批量筛出仍可用账号并输出报告。", icon: Trash2 },
  { key: "plus", title: "Plus 提链", description: "从库存或令牌提取 Plus 长链", detail: "可选择成品、半成品账号，或使用手动 Token / Session 执行 Plus 链接提取任务。", icon: Link2 },
  { key: "warehouse", title: "中转仓库", description: "配置与账号管理", detail: "维护 sub2api、CPA 的连接和巡检策略，并查询管理远端账号。", icon: Warehouse },
  { key: "k12", title: "K12 空间", description: "库存账号加入 K12", detail: "从账号库存挑选账号，批量加入 team 空间。", icon: GraduationCap },
  { key: "system", title: "系统设置", description: "监控、维护与版本更新", detail: "管理登录密钥、实时资源监控、日志缓存、Git 更新和项目维护。", icon: Settings },
];

function SidebarSystemActions({ onGitStatus }: { onGitStatus: (git: SystemGitStatus) => void }) {
  const [busy, setBusy] = useState("");

  const checkUpdate = async () => {
    setBusy("check");
    try {
      const { git } = await checkSystemGit();
      onGitStatus(git);
      if (!git.is_repo) toast.error(git.reason || "当前目录不是 Git 仓库");
      else if (git.update_available) toast.success(`发现 ${git.behind || 0} 个远端更新，请前往系统设置更新`);
      else toast.success("当前已是最新版本");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "更新检测失败");
    } finally {
      setBusy("");
    }
  };

  const restart = async () => {
    if (!window.confirm("确认重启摸鱼机？服务恢复后页面会自动刷新。")) return;
    setBusy("restart");
    try {
      await restartSystem();
      toast.success("重启任务已提交");
      window.setTimeout(() => window.location.reload(), 7000);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "重启失败");
      setBusy("");
    }
  };

  return (
    <div className="grid grid-cols-2 gap-2">
      <Button variant="outline" size="sm" className="justify-center" onClick={() => void checkUpdate()} disabled={Boolean(busy)} title="检测 Git 远端更新">
        <RefreshCw className={busy === "check" ? "animate-spin" : ""} />检测更新
      </Button>
      <Button variant="outline" size="sm" className="justify-center border-rose-200 text-rose-700 hover:bg-rose-50 hover:text-rose-800" onClick={() => void restart()} disabled={Boolean(busy)} title="重启本地项目">
        {busy === "restart" ? <LoaderCircle className="animate-spin" /> : <Power />}重启项目
      </Button>
    </div>
  );
}

function ModuleViewTabs<T extends string>({
  label,
  value,
  options,
  onChange,
  onPrefetch,
}: {
  label: string;
  value: T;
  options: Array<{ value: T; label: string; icon: typeof UserPlus }>;
  onChange: (value: T) => void;
  onPrefetch?: (value: T) => void;
}) {
  return (
    <div role="tablist" aria-label={label} className="grid grid-cols-2 border border-slate-200 bg-white p-1 shadow-sm sm:inline-grid sm:min-w-[360px]">
      {options.map((option) => {
        const Icon = option.icon;
        const selected = value === option.value;
        return (
          <button
            key={option.value}
            type="button"
            role="tab"
            aria-selected={selected}
            onClick={() => onChange(option.value)}
            onPointerEnter={() => onPrefetch?.(option.value)}
            onFocus={() => onPrefetch?.(option.value)}
            className={`flex h-10 items-center justify-center gap-2 px-3 text-sm font-semibold transition ${selected ? "bg-slate-950 text-white" : "text-slate-600 hover:bg-slate-50 hover:text-slate-950"}`}
          >
            <Icon className="size-4" />
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

function formatSeconds(value: unknown) {
  const number = Number(value || 0);
  if (!Number.isFinite(number) || number <= 0) return "0 秒";
  if (number < 60) return `${number.toFixed(number % 1 ? 1 : 0)} 秒`;
  return `${Math.floor(number / 60)} 分 ${Math.round(number % 60)} 秒`;
}

function flowModeText(mode: string) {
  if (mode === "k12") return "K12 模式";
  return "Free 模式";
}

function clampSplitWidth(value: number) {
  if (!Number.isFinite(value)) return 284;
  return Math.max(220, Math.min(460, Math.round(value)));
}

function RegisterDataController() {
  const didLoadRef = useRef(false);
  const loadRegister = useSettingsStore((state) => state.loadRegister);
  const setRegisterConfig = useSettingsStore((state) => state.setRegisterConfig);

  useEffect(() => {
    if (!didLoadRef.current) {
      didLoadRef.current = true;
      void loadRegister();
    }
  }, [loadRegister]);

  useEffect(() => {
    let source: EventSource | null = null;
    let closed = false;
    void getStoredAuthKey().then((token) => {
      if (closed || !token) return;
      source = new EventSource(`${webConfig.apiUrl.replace(/\/$/, "")}/api/register/events?token=${encodeURIComponent(token)}`);
      source.onmessage = (event) => setRegisterConfig(JSON.parse(event.data) as RegisterConfig);
    });
    return () => {
      closed = true;
      source?.close();
    };
  }, [setRegisterConfig]);

  return null;
}

function RuntimeControlBar() {
  const config = useSettingsStore((state) => state.registerConfig);
  const isSaving = useSettingsStore((state) => state.isSavingRegister);
  const setFlowMode = useSettingsStore((state) => state.setRegisterFlowMode);
  const setProductMode = useSettingsStore((state) => state.setRegisterProductMode);
  const setTargetMode = useSettingsStore((state) => state.setRegisterMode);
  const setTotal = useSettingsStore((state) => state.setRegisterTotal);
  const setTargetQuota = useSettingsStore((state) => state.setRegisterTargetQuota);
  const setTargetAvailable = useSettingsStore((state) => state.setRegisterTargetAvailable);
  const setThreads = useSettingsStore((state) => state.setRegisterThreads);
  const save = useSettingsStore((state) => state.saveRegister);
  const toggle = useSettingsStore((state) => state.toggleRegister);
  const reset = useSettingsStore((state) => state.resetRegister);
  if (!config) return null;

  const stats = config.stats || { success: 0, fail: 0, done: 0, running: 0, threads: config.threads };
  const total = Math.max(1, Number(config.total || 1));
  const done = Number(stats.done || 0);
  const targetMode = config.mode || "total";
  const targetValue = targetMode === "available"
    ? Math.max(1, Number(config.target_available || 1))
    : targetMode === "quota"
      ? Math.max(1, Number(config.target_quota || 1))
      : total;
  const targetCurrent = targetMode === "available"
    ? Number(stats.current_available || 0)
    : targetMode === "quota"
      ? Number(stats.current_quota || 0)
      : done;
  const progress = Math.max(0, Math.min(100, (targetCurrent / targetValue) * 100));
  const flowMode = config.register_mode || (config.k12_mode ? "k12" : "free");
  const productMode = config.product_mode || "partial";
  const successRate = `${stats.success_rate || 0}%`;
  const isRunning = Boolean(config.enabled);

  const compactStats = [
    { label: targetMode === "total" ? "成功" : "目标进度", value: `${targetCurrent}/${targetValue}` },
    { label: "完成", value: done },
    { label: "失败", value: stats.fail || 0 },
    { label: "成功率", value: successRate },
    { label: "运行/线程", value: `${stats.running || 0}/${config.threads || 1}` },
    { label: "耗时", value: formatSeconds(stats.elapsed_seconds) },
  ];

  return (
    <section className="rounded-2xl border border-slate-200/80 bg-white/95 p-3 shadow-[0_16px_45px_-36px_rgba(15,23,42,0.5)] backdrop-blur">
      <div className="flex flex-col gap-3 2xl:flex-row 2xl:items-center">
        <div className="flex min-w-[190px] items-center gap-3">
          <div className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-slate-950 text-emerald-300">
            <Activity className="size-5" />
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h2 className="truncate text-base font-semibold text-slate-950">{T.app}</h2>
              <Badge variant={isRunning ? "success" : "secondary"} className="rounded-md">{isRunning ? T.running : T.stopped}</Badge>
            </div>
            <div className="mt-1 truncate text-xs text-emerald-700">{flowModeText(flowMode)}{flowMode === "free" ? ` / ${productMode === "finished" ? "成品模式" : "半成品模式"}` : ""}</div>
          </div>
        </div>

        <div className="grid flex-1 grid-cols-3 gap-x-3 gap-y-2 sm:grid-cols-6">
          {compactStats.map((item) => (
            <div key={item.label} className="min-w-0 border-l border-slate-200 pl-3 first:border-l-0 first:pl-0">
              <div className="text-[11px] text-slate-500">{item.label}</div>
              <div className="mt-0.5 truncate text-sm font-semibold tabular-nums text-slate-950">{item.value}</div>
            </div>
          ))}
        </div>

        <div className="flex min-w-[138px] items-center gap-2">
          <div className="h-2 flex-1 overflow-hidden rounded-full bg-slate-100">
            <div className="h-full rounded-full bg-emerald-500 transition-[width] duration-300" style={{ width: `${progress}%` }} />
          </div>
          <span className="w-11 text-right text-xs tabular-nums text-slate-500">{progress.toFixed(1)}%</span>
        </div>

        <div className="grid grid-cols-2 gap-2 md:grid-cols-3 2xl:flex 2xl:items-end">
          <label className="space-y-1">
            <span className="text-[11px] font-medium text-slate-500">注册模式</span>
            <select value={flowMode} onChange={(event) => setFlowMode(event.target.value as "free" | "k12")} disabled={isRunning} className="h-10 w-full rounded-xl border border-slate-200 bg-white px-3 text-sm text-slate-900 outline-none focus-visible:border-emerald-500 focus-visible:ring-[3px] focus-visible:ring-emerald-100 disabled:opacity-60 2xl:w-[170px]">
              <option value="free">Free 模式</option>
              <option value="k12">K12 模式</option>
            </select>
          </label>
          {flowMode === "free" ? (
            <label className="space-y-1">
              <span className="text-[11px] font-medium text-slate-500">账号产物</span>
              <select value={productMode} onChange={(event) => setProductMode(event.target.value as "partial" | "finished")} disabled={isRunning} className="h-10 w-full rounded-xl border border-slate-200 bg-white px-3 text-sm text-slate-900 outline-none focus-visible:border-emerald-500 focus-visible:ring-[3px] focus-visible:ring-emerald-100 disabled:opacity-60 2xl:w-[138px]">
                <option value="partial">半成品模式</option>
                <option value="finished">成品模式</option>
              </select>
            </label>
          ) : null}
          <label className="space-y-1">
            <span className="text-[11px] font-medium text-slate-500">目标模式</span>
            <select value={targetMode} onChange={(event) => setTargetMode(event.target.value as "total" | "available" | "quota")} disabled={isRunning} className="h-10 w-full rounded-xl border border-slate-200 bg-white px-3 text-sm text-slate-900 outline-none focus-visible:border-emerald-500 focus-visible:ring-[3px] focus-visible:ring-emerald-100 disabled:opacity-60 2xl:w-[132px]">
              <option value="total">任务总数</option>
              <option value="available">可用账号</option>
              <option value="quota">剩余额度</option>
            </select>
          </label>
          <label className="space-y-1">
            <span className="text-[11px] font-medium text-slate-500">{targetMode === "available" ? "可用账号目标" : targetMode === "quota" ? "累计剩余额度" : "任务数量"}</span>
            <Input
              value={String(targetMode === "available" ? config.target_available : targetMode === "quota" ? config.target_quota : config.total)}
              onChange={(event) => targetMode === "available" ? setTargetAvailable(event.target.value) : targetMode === "quota" ? setTargetQuota(event.target.value) : setTotal(event.target.value)}
              disabled={isRunning}
              className="h-10 rounded-xl 2xl:w-[116px]"
            />
          </label>
          {targetMode !== "total" ? <label className="space-y-1">
            <span className="text-[11px] font-medium text-slate-500">最大尝试</span>
            <Input value={String(config.total)} onChange={(event) => setTotal(event.target.value)} disabled={isRunning} className="h-10 rounded-xl 2xl:w-[100px]" />
          </label>
          : null}
          <label className="space-y-1">
            <span className="text-[11px] font-medium text-slate-500">线程</span>
            <Input type="number" min={1} max={64} value={String(config.threads)} onChange={(event) => setThreads(event.target.value)} disabled={isRunning} className="h-10 rounded-xl 2xl:w-[82px]" />
          </label>
          <Button className="h-10 rounded-xl bg-emerald-600 px-4 font-semibold text-white hover:bg-emerald-700" onClick={() => void toggle()} disabled={isSaving}>
            {isSaving ? <LoaderCircle className="size-4 animate-spin" /> : isRunning ? <Square className="size-4" /> : <Play className="size-4" />}
            {isRunning ? "停止" : "启动"}
          </Button>
          <Button variant="outline" size="icon" title="保存配置" aria-label="保存配置" className="size-10 rounded-xl border-slate-200 bg-white" onClick={() => void save()} disabled={isSaving || isRunning}>
            <Save className="size-4" />
          </Button>
          <Button variant="outline" size="icon" title="清空日志并重置统计" aria-label="清空日志并重置统计" className="size-10 rounded-xl border-slate-200 bg-white" onClick={() => void reset()} disabled={isSaving || isRunning}>
            <RotateCcw className="size-4" />
          </Button>
        </div>
      </div>
    </section>
  );
}

function RegisterPageContent() {
  const [activeSection, setActiveSection] = useState<ManageSection>("register");
  const [mailView, setMailView] = useState<MailView>("inventory");
  const [accountView, setAccountView] = useState<AccountView>("inventory");
  const [warehouseView, setWarehouseView] = useState<WarehouseView>("config");
  const [residentViews, setResidentViews] = useState<Set<string>>(() => new Set(["register"]));
  const [sidebarWidth, setSidebarWidth] = useState(284);
  const [isResizing, setIsResizing] = useState(false);
  const [appVersion, setAppVersion] = useState("1.0");
  const [gitStatus, setGitStatus] = useState<SystemGitStatus | null>(null);
  const shellRef = useRef<HTMLDivElement | null>(null);
  const didLoadSplitRef = useRef(false);
  const active = sections.find((item) => item.key === activeSection) || sections[0];
  const ActiveIcon = active.icon;
  const splitStyle = { "--register-split-cols": `${sidebarWidth}px 10px minmax(0,1fr)` } as CSSProperties;

  useEffect(() => {
    void fetchVersion().then(({ version }) => setAppVersion(version || "1.0")).catch(() => undefined);
  }, []);

  useEffect(() => {
    let mounted = true;
    const refreshGitStatus = async () => {
      try {
        const { git } = await checkSystemGit();
        if (mounted) setGitStatus(git);
      } catch {
        // Automatic update checks stay silent; manual checks still report errors.
      }
    };
    void refreshGitStatus();
    const timer = window.setInterval(() => void refreshGitStatus(), 15 * 60 * 1000);
    return () => {
      mounted = false;
      window.clearInterval(timer);
    };
  }, []);

  const currentView = (section: ManageSection) => section === "mail" ? mailView : section === "accounts" ? accountView : section === "warehouse" ? warehouseView : "";

  const keepResident = (section: ManageSection, view = currentView(section)) => {
    const key = view ? `${section}:${view}` : section;
    setResidentViews((current) => current.has(key) ? current : new Set([...current, key]));
  };

  const storeLocation = (section: ManageSection, view = currentView(section)) => {
    try {
      const url = new URL(window.location.href);
      url.searchParams.set("section", section);
      if (view) url.searchParams.set("view", view);
      else url.searchParams.delete("view");
      window.history.replaceState(null, "", url.toString());
      window.localStorage.setItem("register-active-section", section);
      if (view) window.localStorage.setItem(`register-${section}-view`, view);
    } catch {
      // ignore browser state failures
    }
  };

  const selectSection = (section: ManageSection) => {
    keepResident(section);
    setActiveSection(section);
    storeLocation(section);
  };

  const selectMailView = (view: MailView) => { keepResident("mail", view); setMailView(view); storeLocation("mail", view); };
  const selectAccountView = (view: AccountView) => { keepResident("accounts", view); setAccountView(view); storeLocation("accounts", view); };
  const selectWarehouseView = (view: WarehouseView) => { setWarehouseView(view); storeLocation("warehouse", view); };

  useEffect(() => {
    try {
      const stored = window.localStorage.getItem("register-sidebar-width");
      if (stored) setSidebarWidth(clampSplitWidth(Number(stored)));
    } catch {
      // ignore localStorage failures
    } finally {
      didLoadSplitRef.current = true;
    }
  }, []);

  useEffect(() => {
    try {
      const url = new URL(window.location.href);
      const fromUrl = url.searchParams.get("section");
      const fromStorage = window.localStorage.getItem("register-active-section");
      const raw = fromUrl || fromStorage || "";
      const legacy: Record<string, { section: ManageSection; view: string }> = {
        "outlook-mailboxes": { section: "mail", view: "inventory" },
        mailboxes: { section: "mail", view: "config" },
        partials: { section: "accounts", view: "partials" },
        sub2api: { section: "warehouse", view: "config" },
        "warehouse-management": { section: "warehouse", view: "management" },
      };
      const mapped = legacy[raw];
      const candidate = mapped?.section || sections.find((item) => item.key === raw)?.key;
      const requestedView = mapped?.view || url.searchParams.get("view") || (candidate ? window.localStorage.getItem(`register-${candidate}-view`) : "") || "";
      if (candidate === "mail" && ["inventory", "config"].includes(requestedView)) setMailView(requestedView as MailView);
      if (candidate === "accounts" && ["inventory", "partials"].includes(requestedView)) setAccountView(requestedView as AccountView);
      if (candidate === "warehouse" && ["config", "management"].includes(requestedView)) setWarehouseView(requestedView as WarehouseView);
      if (candidate) {
        keepResident(candidate, requestedView || currentView(candidate));
        setActiveSection(candidate);
      }
    } catch {
      // ignore browser state failures
    }
  }, []);

  useEffect(() => {
    if (!didLoadSplitRef.current) return;
    try {
      window.localStorage.setItem("register-sidebar-width", String(sidebarWidth));
    } catch {
      // ignore localStorage failures
    }
  }, [sidebarWidth]);

  useEffect(() => {
    if (!isResizing) return;
    const handlePointerMove = (event: PointerEvent) => {
      const shell = shellRef.current;
      if (!shell) return;
      const rect = shell.getBoundingClientRect();
      setSidebarWidth(clampSplitWidth(event.clientX - rect.left));
    };
    const handlePointerUp = () => setIsResizing(false);
    document.body.classList.add("register-resizing");
    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", handlePointerUp);
    window.addEventListener("pointercancel", handlePointerUp);
    return () => {
      document.body.classList.remove("register-resizing");
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", handlePointerUp);
      window.removeEventListener("pointercancel", handlePointerUp);
    };
  }, [isResizing]);

  return (
    <>
      <RegisterDataController />
      <div ref={shellRef} className="register-shell w-full max-w-none min-h-[calc(100dvh-104px)] gap-3 lg:gap-0" style={splitStyle}>
        <aside className="flex flex-col overflow-hidden rounded-2xl border border-slate-200/80 bg-white/90 shadow-[0_18px_55px_-40px_rgba(15,23,42,0.45)] backdrop-blur lg:sticky lg:top-4 lg:h-[calc(100dvh-118px)] lg:min-w-0 lg:rounded-[30px]">
          <div className="hidden border-b border-slate-200/80 p-5 lg:block">
            <div className="flex items-center gap-3">
              <BrandMark className="size-12 drop-shadow-[0_14px_18px_rgba(15,23,42,0.18)]" />
              <div>
                <p className="flex items-center gap-2 text-xs font-medium text-slate-500">本地控制台 <Badge variant="outline" className="h-5 rounded-md px-1.5 text-[10px]">v{appVersion}</Badge></p>
                <h1 className="mt-1 text-lg font-semibold tracking-tight text-slate-950">{T.app}</h1>
              </div>
            </div>
            {gitStatus?.update_available ? (
              <button
                type="button"
                onClick={() => selectSection("system")}
                className="mt-4 w-full rounded-2xl border border-amber-300 bg-amber-50 p-3 text-left text-xs leading-5 text-amber-900 outline-none transition hover:border-amber-400 hover:bg-amber-100 focus-visible:ring-[3px] focus-visible:ring-amber-100"
              >
                <span className="flex items-center gap-2 font-semibold">
                  <BellRing className="size-4" />
                  发现 GitHub 更新
                  <Badge className="ml-auto border-amber-300 bg-white text-amber-800">{gitStatus.behind || 0}</Badge>
                </span>
                <span className="mt-1 block">远端新增 {gitStatus.behind || 0} 个提交，点击前往“系统设置”安全更新。</span>
              </button>
            ) : null}
            <div className="mt-4 rounded-2xl border border-emerald-200 bg-emerald-50/80 p-3 text-xs leading-5 text-emerald-800">
              <div className="flex items-center gap-2 font-semibold text-emerald-900">
                <CheckCircle2 className="size-4" />
                微软邮箱库存到账号库存
              </div>
              <p className="mt-1">注册任务会从微软邮箱库存领取指定分组的邮箱；动态邮箱参数在“邮箱配置”中维护。</p>
            </div>
          </div>

          <nav className="grid flex-1 grid-cols-4 gap-1.5 p-2 sm:grid-cols-9 lg:block lg:space-y-1.5 lg:overflow-y-auto lg:p-3" aria-label="注册机管理模块" role="tablist">
            {sections.map((item) => {
              const Icon = item.icon;
              const isActive = activeSection === item.key;
              return (
                <button
                  key={item.key}
                  type="button"
                  role="tab"
                  aria-selected={isActive}
                  aria-controls={`register-section-${item.key}`}
                  onClick={() => selectSection(item.key)}
                  onPointerEnter={() => void prefetchManagementData(item.key, currentView(item.key))}
                  onFocus={() => void prefetchManagementData(item.key, currentView(item.key))}
                  className={`group flex min-w-0 w-full flex-col items-center gap-1 rounded-xl border px-1 py-2 text-center outline-none transition duration-200 focus-visible:ring-[3px] focus-visible:ring-emerald-100 lg:flex-row lg:gap-3 lg:rounded-2xl lg:px-3 lg:py-3 lg:text-left ${
                    isActive
                      ? "border-emerald-200 bg-emerald-50 text-emerald-950 shadow-sm"
                      : "border-transparent bg-transparent text-slate-700 hover:border-slate-200 hover:bg-slate-50"
                  }`}
                >
                  <span className={`flex size-8 shrink-0 items-center justify-center rounded-lg transition lg:size-10 lg:rounded-xl ${isActive ? "bg-emerald-500 text-white" : "bg-slate-100 text-slate-500 group-hover:bg-white"}`}>
                    <Icon className="size-4" />
                  </span>
                  <span className="min-w-0">
                    <span className="block text-[11px] font-semibold leading-4 sm:text-xs lg:text-sm">{item.title}</span>
                    <span className={`mt-0.5 hidden truncate text-xs lg:block ${isActive ? "text-emerald-700" : "text-slate-500"}`}>{item.description}</span>
                  </span>
                </button>
              );
            })}
          </nav>
          <div className="hidden border-t border-slate-200/80 p-3 lg:block">
            <SidebarSystemActions onGitStatus={setGitStatus} />
          </div>
        </aside>

        <div
          role="separator"
          aria-orientation="vertical"
          aria-label="拖动调整左侧 Tab 栏宽度"
          title="拖动调整左侧 Tab 栏宽度，双击恢复默认"
          tabIndex={0}
          onPointerDown={(event) => {
            event.preventDefault();
            setIsResizing(true);
          }}
          onDoubleClick={() => setSidebarWidth(284)}
          onKeyDown={(event) => {
            if (event.key === "ArrowLeft") {
              event.preventDefault();
              setSidebarWidth((value) => clampSplitWidth(value - 16));
            } else if (event.key === "ArrowRight") {
              event.preventDefault();
              setSidebarWidth((value) => clampSplitWidth(value + 16));
            } else if (event.key === "Home") {
              event.preventDefault();
              setSidebarWidth(220);
            } else if (event.key === "End") {
              event.preventDefault();
              setSidebarWidth(460);
            }
          }}
          className={`register-splitter hidden lg:flex ${isResizing ? "register-splitter-active" : ""}`}
        >
          <span className="register-splitter-line" />
        </div>

        <section id={`register-section-${activeSection}`} role="tabpanel" className="min-w-0 space-y-4 lg:h-[calc(100dvh-118px)] lg:overflow-y-auto lg:pl-3 lg:pr-0">
          {activeSection === "register" ? <RuntimeControlBar /> : null}
          {activeSection !== "register" ? <div className="rounded-[30px] border border-slate-200/80 bg-white/90 p-5 shadow-[0_18px_55px_-42px_rgba(15,23,42,0.38)] backdrop-blur">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div className="flex min-w-0 items-start gap-3">
                <div className="flex size-12 shrink-0 items-center justify-center rounded-[18px] border border-emerald-200 bg-emerald-50 text-emerald-700">
                  <ActiveIcon className="size-5" />
                </div>
                <div className="min-w-0">
                  <p className="text-xs font-medium text-slate-500">{T.currentModule}</p>
                  <h2 className="mt-1 text-2xl font-semibold tracking-tight text-slate-950">{active.title}</h2>
                  <p className="mt-1 max-w-[70ch] text-sm leading-6 text-slate-600">{active.detail}</p>
                </div>
              </div>
              <Badge variant="outline" className="rounded-xl border-slate-200 bg-slate-50 px-3 py-1 text-slate-600">
                {active.description}
              </Badge>
            </div>
          </div> : null}

          {activeSection === "mail" ? <ModuleViewTabs label="邮箱模块" value={mailView} onChange={selectMailView} onPrefetch={(view) => void prefetchManagementData("mail", view)} options={[{ value: "inventory", label: "微软邮箱库存", icon: Mails }, { value: "config", label: "邮箱配置", icon: Mail }]} /> : null}
          {activeSection === "accounts" ? <ModuleViewTabs label="账号模块" value={accountView} onChange={selectAccountView} onPrefetch={(view) => void prefetchManagementData("accounts", view)} options={[{ value: "inventory", label: "成品账号", icon: Boxes }, { value: "partials", label: "半成品账号", icon: Clock3 }]} /> : null}
          {activeSection === "warehouse" ? <ModuleViewTabs label="中转仓库模块" value={warehouseView} onChange={selectWarehouseView} options={[{ value: "config", label: "仓库配置", icon: Warehouse }, { value: "management", label: "账号管理", icon: ListChecks }]} /> : null}

          {activeSection === "register" ? <RegisterCard /> : null}
          {activeSection === "proxy" ? <ProxyPoolCard /> : null}
          {residentViews.has("mail:inventory") ? <div className={activeSection === "mail" && mailView === "inventory" ? "" : "hidden"}><OutlookMailboxInventoryCard active={activeSection === "mail" && mailView === "inventory"} /></div> : null}
          {activeSection === "mail" && mailView === "config" ? <MailboxPoolCard /> : null}
          {residentViews.has("accounts:inventory") ? <div className={activeSection === "accounts" && accountView === "inventory" ? "" : "hidden"}><AccountPoolCard active={activeSection === "accounts" && accountView === "inventory"} /></div> : null}
          {residentViews.has("accounts:partials") ? <div className={activeSection === "accounts" && accountView === "partials" ? "" : "hidden"}><PartialAccountCard active={activeSection === "accounts" && accountView === "partials"} /></div> : null}
          {activeSection === "garbage" ? <GarbageHealthCard /> : null}
          {activeSection === "plus" ? <PlusLinkCard /> : null}
          {activeSection === "warehouse" && warehouseView === "config" ? <WarehouseConfigCard /> : null}
          {activeSection === "warehouse" && warehouseView === "management" ? <WarehouseManagerCard /> : null}
          {activeSection === "k12" ? <K12WorkspaceCard /> : null}
          {activeSection === "system" ? <SystemSettingsCard /> : null}
        </section>
      </div>
    </>
  );
}

export default function RegisterPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);
  if (isCheckingAuth || !session || session.role !== "admin") {
    return (
      <div className="flex min-h-[40vh] items-center justify-center rounded-[30px] border border-slate-200/80 bg-white/80">
        <LoaderCircle className="size-5 animate-spin text-slate-400" />
      </div>
    );
  }
  return <RegisterPageContent />;
}
