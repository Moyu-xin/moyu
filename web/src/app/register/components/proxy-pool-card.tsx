"use client";

import { useEffect, useMemo, useState } from "react";
import { Download, LoaderCircle, Network, Play, Plus, RefreshCw, Save, ServerCog, Square, Trash2, Zap } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  clearProxyPoolNodes,
  deleteProxyPoolNode,
  deleteProxyPoolSubscription,
  fetchManagedMihomoStatus,
  fetchProxyPool,
  importProxyNodes,
  importProxySubscriptions,
  installManagedMihomo,
  previewProxyAssignments,
  startManagedMihomo,
  stopManagedMihomo,
  syncProxyPoolMihomo,
  testProxyPoolNode,
  updateProxyPool,
  type ProxyPool,
  type ProxyPoolNode,
  type ManagedMihomoStatus,
} from "@/lib/api";
import { useSettingsStore } from "../store";

const Z = {
  title: "代理配置",
  desc: "每次注册任务轮换节点，单次流程内固定出口；支持直接代理、项目托管 Mihomo 和外部 Mihomo。",
  enabled: "已启用", disabled: "未启用", nodes: "节点", refresh: "刷新", save: "保存", syncMihomo: "读取 Mihomo 节点",
  multi: "启用多节点", nodeMode: "节点模式", fallback: "兜底代理", mihomo: "启用 Mihomo 端口池",
  api: "控制 API", group: "策略组", secret: "密钥", host: "代理主机", port: "端口基数", scheme: "协议",
  importSub: "导入订阅链接", importNode: "导入直接代理", addSub: "导入订阅", addNode: "导入节点", clear: "清空节点",
  pool: "节点池", filter: "筛选节点", detail: "节点详情", subList: "订阅列表", select: "请选择一个节点", delete: "删除", test: "测活当前节点",
  name: "名称", type: "类型", url: "代理URL", server: "服务器", status: "状态", latency: "延迟", updated: "更新时间", none: "暂无节点", ok: "成功", failed: "失败",
};

function formatTime(value?: string) {
  if (!value) return "-";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString();
}

export function ProxyPoolCard() {
  const [pool, setPool] = useState<ProxyPool | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [subText, setSubText] = useState("");
  const [allowPrivateSubscription, setAllowPrivateSubscription] = useState(false);
  const [nodeText, setNodeText] = useState("");
  const [query, setQuery] = useState("");
  const [selectedNodeId, setSelectedNodeId] = useState("");
  const managedThreads = useSettingsStore((state) => Math.max(1, Number(state.registerConfig?.threads) || 1));
  const [managedStatus, setManagedStatus] = useState<ManagedMihomoStatus | null>(null);
  const [assignmentPreview, setAssignmentPreview] = useState<Array<{ proxy?: string; source?: string; label?: string; slot?: number; error?: string }>>([]);

  const selectedNode = useMemo<ProxyPoolNode | undefined>(() =>
    pool?.nodes.find((item) => item.id === selectedNodeId), [pool, selectedNodeId]);

  const filteredNodes = useMemo(() => {
    const q = query.trim().toLowerCase();
    const nodes = pool?.nodes || [];
    return q ? nodes.filter((item) => `${item.name} ${item.type} ${item.server} ${item.proxy_url}`.toLowerCase().includes(q)) : nodes;
  }, [pool, query]);

  const patchPool = (updates: Partial<ProxyPool>) => setPool((cur) => (cur ? { ...cur, ...updates } : cur));
  const patchMihomo = (updates: Partial<ProxyPool["mihomo"]>) => setPool((cur) => {
    if (!cur) return cur;
    return { ...cur, mihomo: { ...cur.mihomo, ...updates } };
  });
  const patchManaged = (updates: Partial<ProxyPool["managed_mihomo"]>) => setPool((cur) => {
    if (!cur) return cur;
    return { ...cur, managed_mihomo: { ...cur.managed_mihomo, ...updates } };
  });

  const load = async () => {
    setLoading(true);
    try {
      const [data, status] = await Promise.all([fetchProxyPool(), fetchManagedMihomoStatus()]);
      setPool(data.proxy_pool);
      setManagedStatus(status.managed_mihomo);
      if (!selectedNodeId && data.proxy_pool.nodes[0]) {
        setSelectedNodeId(data.proxy_pool.nodes[0].id);
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : Z.title + Z.failed);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const save = async () => {
    if (!pool) return;
    setSaving(true);
    try {
      const data = await updateProxyPool({
        enabled: pool.enabled,
        mode: pool.mode,
        strategy: pool.strategy,
        node_mode: pool.node_mode,
        fallback_proxy: pool.fallback_proxy,
        node_blacklist: pool.node_blacklist,
        fastest_mode: pool.fastest_mode,
        max_retry_count: pool.max_retry_count,
        retry_delay: pool.retry_delay,
        mihomo: pool.mihomo,
        managed_mihomo: pool.managed_mihomo,
        subscriptions: pool.subscriptions,
      });
      setPool(data.proxy_pool);
      toast.success(Z.save + Z.ok);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : Z.save + Z.failed);
    } finally {
      setSaving(false);
    }
  };

  const patchSubscription = (id: string, enabled: boolean) => setPool((current) => current ? {
    ...current,
    subscriptions: current.subscriptions.map((item) => item.id === id ? { ...item, enabled } : item),
  } : current);

  const installManaged = async () => {
    setSaving(true);
    try {
      const data = await installManagedMihomo();
      setPool(data.proxy_pool);
      setManagedStatus(data.managed_mihomo);
      toast.success("项目托管 Mihomo 核心安装完成");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "安装托管 Mihomo 失败");
    } finally {
      setSaving(false);
    }
  };

  const startManaged = async () => {
    if (!pool) return;
    setSaving(true);
    try {
      await updateProxyPool({
        node_mode: "managed_mihomo",
        managed_mihomo: { ...pool.managed_mihomo, enabled: true },
        subscriptions: pool.subscriptions,
      });
      const data = await startManagedMihomo(managedThreads);
      setPool(data.proxy_pool);
      setManagedStatus(data.managed_mihomo);
      toast.success(`托管 Mihomo 已启动 ${data.managed_mihomo.running} 个线程出口`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "启动托管 Mihomo 失败");
    } finally {
      setSaving(false);
    }
  };

  const stopManaged = async () => {
    setSaving(true);
    try {
      const data = await stopManagedMihomo();
      setPool(data.proxy_pool);
      setManagedStatus(data.managed_mihomo);
      toast.success("托管 Mihomo 已停止");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "停止托管 Mihomo 失败");
    } finally {
      setSaving(false);
    }
  };

  const previewAssignments = async () => {
    setSaving(true);
    try {
      const data = await previewProxyAssignments(managedThreads, managedThreads * 2);
      setPool(data.proxy_pool);
      setManagedStatus(data.managed_mihomo);
      setAssignmentPreview(data.assignments || []);
      const failed = (data.assignments || []).filter((item) => item.error).length;
      if (failed) toast.warning(`分配预览完成，${failed} 个任务回退`);
      else toast.success("节点轮换预览完成");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "节点分配预览失败");
    } finally {
      setSaving(false);
    }
  };

  const addSubscriptions = async () => {
    const lines = subText.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    if (!lines.length) return toast.error(Z.importSub);
    setSaving(true);
    try {
      const data = await importProxySubscriptions({
        items: lines.map((url, i) => ({ name: `${Z.importSub}${i + 1}`, url, allow_private: allowPrivateSubscription })),
        allow_private: allowPrivateSubscription,
        fetch: true
      });
      setPool(data.proxy_pool);
      setSubText("");
      toast.success(`${Z.addSub} ${Z.ok}`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : Z.addSub + Z.failed);
    } finally {
      setSaving(false);
    }
  };

  const addNodes = async () => {
    if (!nodeText.trim()) return toast.error(Z.importNode);
    setSaving(true);
    try {
      const data = await importProxyNodes(nodeText);
      setPool(data.proxy_pool);
      setNodeText("");
      toast.success(`${Z.addNode} ${Z.ok}`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : Z.addNode + Z.failed);
    } finally {
      setSaving(false);
    }
  };

  const syncMihomo = async () => {
    setSaving(true);
    try {
      const data = await syncProxyPoolMihomo();
      setPool(data.proxy_pool);
      toast.success(`${Z.syncMihomo} ${Z.ok}`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : Z.syncMihomo + Z.failed);
    } finally {
      setSaving(false);
    }
  };

  const testNode = async (node: ProxyPoolNode) => {
    setSaving(true);
    try {
      const data = await testProxyPoolNode(node.id, node.proxy_url || "");
      setPool(data.proxy_pool);
      const testOk = (data.proxy_test as any)?.ok;
      toast.success(testOk ? `${Z.test} ${Z.ok}` : `${Z.test} ${Z.failed}`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : Z.test + Z.failed);
    } finally {
      setSaving(false);
    }
  };

  const removeNode = async (id: string) => {
    setSaving(true);
    try {
      const data = await deleteProxyPoolNode(id);
      setPool(data.proxy_pool);
      setSelectedNodeId(data.proxy_pool.nodes[0]?.id || "");
    } finally {
      setSaving(false);
    }
  };

  const clearNodes = async () => {
    setSaving(true);
    try {
      const data = await clearProxyPoolNodes();
      setPool(data.proxy_pool);
      setSelectedNodeId("");
    } finally {
      setSaving(false);
    }
  };

  const removeSubscription = async (id: string) => {
    setSaving(true);
    try {
      const data = await deleteProxyPoolSubscription(id);
      setPool(data.proxy_pool);
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center rounded-[30px] border border-slate-200 bg-white/80 p-10">
        <LoaderCircle className="size-5 animate-spin text-slate-400" />
      </div>
    );
  }

  if (!pool) return null;

  return (
    <Card className="overflow-hidden">
      <CardHeader className="gap-3 border-b border-slate-200 p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex items-center gap-3">
            <div className="flex size-10 items-center justify-center rounded-xl border border-emerald-200 bg-emerald-50">
              <Network className="size-5 text-emerald-700" />
            </div>
            <div>
              <CardTitle className="text-lg text-slate-950">{Z.title}</CardTitle>
              <CardDescription className="text-slate-500">{Z.desc}</CardDescription>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={pool.enabled ? "success" : "secondary"} className="rounded-md">
              {pool.enabled ? Z.enabled : Z.disabled}
            </Badge>
            <Badge variant="secondary" className="rounded-md">
              {pool.node_mode === "managed_mihomo"
                ? `启用订阅 ${pool.subscriptions.filter((item) => item.enabled).length}/${pool.subscriptions.length}`
                : `${Z.nodes} ${pool.stats?.enabled || 0}/${pool.stats?.total || 0}`}
            </Badge>
            <Button variant="outline" className="h-9 rounded-xl cursor-pointer" onClick={() => void load()} disabled={saving}>
              <RefreshCw className="size-4" />{Z.refresh}
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="grid gap-4 p-4 2xl:grid-cols-[minmax(0,1fr)_360px]">
        <div className="space-y-4">
          <div className="grid gap-3 rounded-xl border border-slate-200 bg-slate-50 p-3 lg:grid-cols-2 2xl:grid-cols-5">
            <label className="flex items-center gap-3 rounded-xl border border-white bg-white px-3 py-2 text-sm cursor-pointer">
              <Checkbox checked={pool.enabled} onCheckedChange={(c) => patchPool({ enabled: Boolean(c) })} />
              <span>{Z.multi}</span>
            </label>
            <div className="space-y-1">
              <label className="text-xs font-semibold text-slate-700">{Z.nodeMode}</label>
              <select
                value={pool.node_mode || "direct_proxy"}
                onChange={(e) => patchPool({ node_mode: e.target.value })}
                className="h-10 w-full rounded-xl border border-slate-200 bg-white px-3 text-sm"
              >
                <option value="direct_proxy">直接 HTTP / SOCKS</option>
                <option value="managed_mihomo">项目托管 Mihomo</option>
                <option value="mihomo_ports">外部 Clash / Mihomo</option>
              </select>
            </div>
            <div className="space-y-1">
              <label className="text-xs font-semibold text-slate-700">节点分配</label>
              <select value={pool.mode || "per_task"} onChange={(event) => patchPool({ mode: event.target.value })} className="h-10 w-full rounded-xl border border-slate-200 bg-white px-3 text-sm">
                <option value="per_task">每次注册切换节点</option>
                <option value="per_thread">每个线程固定节点</option>
              </select>
            </div>
            <div className="space-y-1">
              <label className="text-xs font-semibold text-slate-700">轮换策略</label>
              <select value={pool.strategy || "round_robin"} onChange={(event) => patchPool({ strategy: event.target.value })} className="h-10 w-full rounded-xl border border-slate-200 bg-white px-3 text-sm">
                <option value="round_robin">顺序轮询</option>
                <option value="random">随机节点</option>
              </select>
            </div>
            <div className="space-y-1">
              <label className="text-xs font-semibold text-slate-700">{Z.fallback}</label>
              <Input
                value={pool.fallback_proxy || ""}
                onChange={(e) => patchPool({ fallback_proxy: e.target.value })}
                placeholder="http://127.0.0.1:7890"
                className="h-10 rounded-xl bg-white"
              />
            </div>
            <div className="flex flex-wrap items-center gap-2 lg:col-span-2 2xl:col-span-5">
              <Button className="h-9 rounded-xl bg-emerald-600 px-4 text-white hover:bg-emerald-700 cursor-pointer" onClick={() => void save()} disabled={saving}>
                {saving ? <LoaderCircle className="size-4 animate-spin text-white" /> : <Save className="size-4" />}{Z.save}
              </Button>
              <Badge variant={pool.mode === "per_task" ? "success" : "warning"} className="rounded-md">
                {pool.mode === "per_task" ? "任务完成后换节点" : "线程始终固定节点"}
              </Badge>
              <span className="text-xs text-slate-500">单次注册流程内不会中途切换出口。</span>
            </div>
          </div>

          {pool.node_mode === "managed_mihomo" ? (
            <div className="grid gap-3 rounded-xl border border-emerald-200 bg-emerald-50 p-3 md:grid-cols-4">
              <div className="flex flex-wrap items-center justify-between gap-2 md:col-span-4">
                <div className="flex items-center gap-2 text-sm font-semibold text-emerald-950"><ServerCog className="size-4" />项目托管 Mihomo</div>
                <div className="flex gap-2"><Badge variant={managedStatus?.installed ? "success" : "secondary"} className="rounded-md">{managedStatus?.installed ? "核心已安装" : "核心未安装"}</Badge><Badge variant={(managedStatus?.running || 0) > 0 ? "success" : "secondary"} className="rounded-md">运行 {managedStatus?.running || 0}</Badge><Badge variant={(managedStatus?.quarantined?.length || 0) > 0 ? "warning" : "secondary"} className="rounded-md">隔离 {managedStatus?.quarantined?.length || 0}</Badge></div>
              </div>
              <label className="flex items-center gap-2 rounded-xl border border-white bg-white px-3 py-2 text-sm cursor-pointer">
                <Checkbox checked={Boolean(pool.managed_mihomo?.enabled)} onCheckedChange={(checked) => patchManaged({ enabled: Boolean(checked) })} />
                <span>启用托管核心</span>
              </label>
              <label className="flex items-center gap-2 rounded-xl border border-white bg-white px-3 py-2 text-sm cursor-pointer">
                <Checkbox checked={Boolean(pool.managed_mihomo?.auto_download)} onCheckedChange={(checked) => patchManaged({ auto_download: Boolean(checked) })} />
                <span>缺失时自动安装</span>
              </label>
              <div className="space-y-1 md:col-span-2"><label className="text-xs font-semibold text-emerald-900">托管核心路径</label><Input value={managedStatus?.default_executable || "data/managed_mihomo/mihomo.exe"} disabled className="h-10 rounded-xl bg-slate-100" /></div>
              <div className="space-y-1"><label className="text-xs font-semibold text-emerald-900">代理端口基数</label><Input value={String(pool.managed_mihomo?.proxy_port_base || 42000)} onChange={(event) => patchManaged({ proxy_port_base: Number(event.target.value) || 42000 })} className="h-10 rounded-xl bg-white" /></div>
              <div className="space-y-1"><label className="text-xs font-semibold text-emerald-900">API 端口基数</label><Input value={String(pool.managed_mihomo?.api_port_base || 43000)} onChange={(event) => patchManaged({ api_port_base: Number(event.target.value) || 43000 })} className="h-10 rounded-xl bg-white" /></div>
              <div className="space-y-1"><label className="text-xs font-semibold text-emerald-900">注册线程出口数</label><Input value={String(managedThreads)} readOnly disabled className="h-10 rounded-xl bg-white" /></div>
              <div className="flex flex-wrap items-end gap-2 md:col-span-4">
                <Button variant="outline" className="h-9 rounded-xl border-emerald-200 bg-white" onClick={() => void installManaged()} disabled={saving}><Download className="size-4" />安装核心</Button>
                <Button className="h-9 rounded-xl bg-emerald-600 text-white hover:bg-emerald-700" onClick={() => void startManaged()} disabled={saving || !pool.subscriptions.some((item) => item.enabled)}>{saving ? <LoaderCircle className="size-4 animate-spin" /> : <Play className="size-4" />}启动出口</Button>
                <Button variant="outline" className="h-9 rounded-xl border-emerald-200 bg-white" onClick={() => void previewAssignments()} disabled={saving || !(managedStatus?.running || 0)}><RefreshCw className="size-4" />验证轮换</Button>
                <Button variant="outline" className="h-9 rounded-xl border-rose-200 bg-white text-rose-700" onClick={() => void stopManaged()} disabled={saving || !(managedStatus?.running || 0)}><Square className="size-4" />停止</Button>
              </div>
              {managedStatus?.instances?.length ? <div className="grid gap-2 md:col-span-4 md:grid-cols-2">{managedStatus.instances.map((item) => <div key={item.slot} className="flex items-center justify-between gap-2 rounded-lg border border-emerald-200 bg-white px-3 py-2 text-xs"><span className="font-semibold text-slate-700">线程 {item.slot} · {item.proxy_url}</span><span className="max-w-[50%] truncate text-emerald-700">{item.selected || "等待分配"}</span></div>)}</div> : null}
              {assignmentPreview.length ? <div className="md:col-span-4 overflow-hidden rounded-lg border border-emerald-200 bg-white"><div className="grid grid-cols-[70px_70px_minmax(0,1fr)] border-b border-emerald-100 bg-emerald-50 px-3 py-2 text-xs font-semibold text-emerald-900"><span>任务</span><span>线程</span><span>分配节点</span></div>{assignmentPreview.map((item, index) => <div key={`${index}-${item.slot}-${item.label}`} className="grid grid-cols-[70px_70px_minmax(0,1fr)] border-b border-slate-100 px-3 py-2 text-xs last:border-0"><span>{index + 1}</span><span>{item.slot || "-"}</span><span className={item.error ? "truncate text-rose-700" : "truncate text-slate-700"}>{item.error || item.label || item.proxy || "-"}</span></div>)}</div> : null}
              {managedStatus?.last_error ? <div className="md:col-span-4 rounded-lg border border-rose-200 bg-rose-50 p-2 text-xs text-rose-700">{managedStatus.last_error}</div> : null}
            </div>
          ) : null}

          {pool.node_mode === "mihomo_ports" ? (
            <div className="grid gap-3 rounded-xl border border-sky-200 bg-sky-50 p-3 md:grid-cols-4">
              <label className="flex items-center gap-3 md:col-span-2 cursor-pointer"><Checkbox checked={Boolean(pool.mihomo?.enabled)} onCheckedChange={(c) => patchMihomo({ enabled: Boolean(c) })} /><span className="text-sm font-semibold text-sky-950">外部 Clash / Mihomo</span></label>
              <label className="flex items-center gap-3 md:col-span-2 cursor-pointer"><Checkbox checked={Boolean(pool.mihomo?.thread_port_mode)} onCheckedChange={(c) => patchMihomo({ thread_port_mode: Boolean(c) })} /><span className="text-sm font-semibold text-sky-950">每线程独立端口</span></label>
              <Input value={pool.mihomo.api_base || ""} onChange={(e) => patchMihomo({ api_base: e.target.value })} placeholder={Z.api} className="h-10 rounded-xl bg-white md:col-span-2" />
              <Input value={pool.mihomo.selector || "GLOBAL"} onChange={(e) => patchMihomo({ selector: e.target.value })} placeholder={Z.group} className="h-10 rounded-xl bg-white" />
              <Input value={pool.mihomo.secret || ""} onChange={(e) => patchMihomo({ secret: e.target.value })} placeholder={pool.mihomo.has_secret ? String(pool.mihomo.secret_preview) : Z.secret} className="h-10 rounded-xl bg-white" />
              <Input value={pool.mihomo.proxy_host || "127.0.0.1"} onChange={(e) => patchMihomo({ proxy_host: e.target.value })} placeholder={Z.host} className="h-10 rounded-xl bg-white" />
              <Input value={String(pool.mihomo.proxy_port_base || 7890)} onChange={(e) => patchMihomo({ proxy_port_base: Number(e.target.value) || 7890 })} placeholder={Z.port} className="h-10 rounded-xl bg-white" />
              <select value={pool.mihomo.proxy_scheme || "http"} onChange={(e) => patchMihomo({ proxy_scheme: e.target.value })} className="h-10 rounded-xl border border-sky-200 bg-white px-3 text-sm"><option value="http">HTTP</option><option value="socks5h">SOCKS5</option></select>
              <Button variant="outline" className="h-10 rounded-xl border-sky-200 bg-white px-4 text-sky-700" onClick={() => void syncMihomo()} disabled={saving || !pool.mihomo?.enabled}><RefreshCw className="size-4" />{Z.syncMihomo}</Button>
              {!pool.mihomo?.thread_port_mode ? <div className="md:col-span-4 rounded-lg border border-amber-200 bg-amber-50 p-2 text-xs text-amber-800">单一代理端口只能使用一个全局选择器，多线程运行时节点会互相覆盖。需要并发隔离时使用项目托管模式。</div> : null}
            </div>
          ) : null}

          <div className="grid gap-3 rounded-xl border border-slate-200 bg-slate-50 p-3">
            <div className="text-sm font-semibold text-slate-700">节点过滤与切换</div>
            <div className="grid gap-3 md:grid-cols-3">
              <div className="space-y-1">
                <label className="text-xs font-semibold text-slate-600">节点黑名单（逗号分隔，过滤受限地区）</label>
                <Input
                  value={Array.isArray(pool.node_blacklist) ? pool.node_blacklist.join(", ") : ""}
                  onChange={(e) => patchPool({ node_blacklist: e.target.value.split(",").map(s => s.trim()).filter(Boolean) })}
                  placeholder="港, HK, 台, TW, 中国, CN"
                  className="h-10 rounded-xl bg-white"
                />
              </div>
              <div className="space-y-1">
                <label className="text-xs font-semibold text-slate-600">切换失败重试节点数</label>
                <Input
                  value={String(pool.max_retry_count || 3)}
                  onChange={(e) => patchPool({ max_retry_count: Number(e.target.value) || 3 })}
                  placeholder="3"
                  className="h-10 rounded-xl bg-white"
                />
              </div>
              <div className="space-y-1"><label className="text-xs font-semibold text-slate-600">重试间隔（秒）</label><Input value={String(pool.retry_delay ?? 1)} onChange={(event) => patchPool({ retry_delay: Number(event.target.value) || 0 })} className="h-10 rounded-xl bg-white" /></div>
            </div>
            <div className="flex items-center gap-4">
              <label className="flex items-center gap-2 rounded-xl border border-white bg-white px-3 py-2 text-sm cursor-pointer">
                <Checkbox
                  checked={Boolean(pool.fastest_mode)}
                  onCheckedChange={(c) => patchPool({ fastest_mode: Boolean(c) })}
                />
                <span>优先使用已测活的低延迟节点</span>
              </label>
            </div>
          </div>

          <div className="grid gap-3 md:grid-cols-2">
            <div className="space-y-2 rounded-xl border border-slate-200 bg-white p-3">
              <div className="text-sm font-semibold">{Z.importSub}</div>
              <Textarea
                value={subText}
                onChange={(e) => setSubText(e.target.value)}
                placeholder="https://..."
                className="min-h-24 rounded-xl font-mono text-xs"
              />
              <label className="flex items-start gap-2 text-xs text-slate-600">
                <Checkbox checked={allowPrivateSubscription} onCheckedChange={(value) => setAllowPrivateSubscription(Boolean(value))} />
                <span>允许访问可信内网订阅地址（默认拒绝本机、私网和链路本地地址）</span>
              </label>
              <Button className="h-9 rounded-xl bg-emerald-600 text-white cursor-pointer" onClick={() => void addSubscriptions()} disabled={saving}>
                <Plus className="size-4" />{Z.addSub}
              </Button>
            </div>
            <div className="space-y-2 rounded-xl border border-slate-200 bg-white p-3">
              <div className="text-sm font-semibold">{Z.importNode}</div>
              <Textarea
                value={nodeText}
                onChange={(e) => setNodeText(e.target.value)}
                placeholder="http://user:pass@host:port"
                className="min-h-24 rounded-xl font-mono text-xs"
              />
              <div className="flex gap-2">
                <Button className="h-9 rounded-xl bg-emerald-600 text-white cursor-pointer" onClick={() => void addNodes()} disabled={saving}>
                  <Plus className="size-4" />{Z.addNode}
                </Button>
                <Button variant="outline" className="h-9 rounded-xl border-rose-200 bg-rose-50 text-rose-700 cursor-pointer" onClick={() => void clearNodes()} disabled={saving || !pool.nodes.length}>
                  {Z.clear}
                </Button>
              </div>
            </div>
          </div>

          {pool.node_mode !== "managed_mihomo" ? <div className="space-y-2 rounded-xl border border-slate-200 bg-white p-3">
            <div className="flex items-center justify-between gap-3">
              <div className="text-sm font-semibold">{Z.pool}</div>
              <Input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={Z.filter}
                className="h-9 max-w-xs rounded-xl"
              />
            </div>
            <div className="max-h-[460px] overflow-auto rounded-xl border border-slate-200">
              {!filteredNodes.length ? (
                <div className="p-4 text-sm text-slate-500">{Z.none}</div>
              ) : (
                filteredNodes.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => setSelectedNodeId(item.id)}
                    className={`flex w-full items-center justify-between gap-3 border-b border-slate-100 px-3 py-2 text-left text-sm last:border-b-0 cursor-pointer ${selectedNodeId === item.id ? "bg-emerald-50" : "hover:bg-slate-50"}`}
                  >
                    <span className="min-w-0">
                      <span className="block truncate font-semibold text-slate-900">{item.name}</span>
                      <span className="mt-1 block truncate font-mono text-xs text-slate-500">{item.proxy_url || `${item.type}://${item.server}:${item.port}`}</span>
                    </span>
                    <Badge variant={item.status === "ok" ? "success" : "secondary"} className="shrink-0 rounded-md">
                      {item.status || (item.enabled ? Z.enabled : Z.disabled)}
                    </Badge>
                  </button>
                ))
              )}
            </div>
          </div> : null}
        </div>

        <aside className="space-y-4">
          {pool.node_mode !== "managed_mihomo" ? <div className="rounded-xl border border-slate-200 bg-slate-50 p-3">
            <div className="mb-3 flex items-center justify-between">
              <div className="text-sm font-semibold">{Z.detail}</div>
              {selectedNode ? (
                <Button variant="outline" className="h-8 rounded-lg border-rose-200 bg-rose-50 px-2 text-xs text-rose-700 cursor-pointer" onClick={() => void removeNode(selectedNode.id)} disabled={saving}>
                  <Trash2 className="size-3.5" />{Z.delete}
                </Button>
              ) : null}
            </div>
            {!selectedNode ? (
              <div className="text-sm text-slate-500 py-4 text-center">{Z.select}</div>
            ) : (
              <div className="space-y-3 text-xs">
                {[
                  [Z.name, selectedNode.name],
                  [Z.type, selectedNode.type],
                  [Z.url, selectedNode.proxy_url || (pool.node_mode === "managed_mihomo" ? "由项目托管 Mihomo 解析" : "外部 Mihomo")],
                  [Z.server, selectedNode.server || "-"],
                  [Z.port, selectedNode.port || "-"],
                  [Z.status, selectedNode.status || "unknown"],
                  [Z.latency, selectedNode.latency_ms ? `${selectedNode.latency_ms}ms` : "-"],
                  [Z.updated, formatTime(selectedNode.updated_at)]
                ].map(([l, v]) => (
                  <div key={l} className="space-y-1">
                    <div className="font-semibold text-slate-500">{l}</div>
                    <div className="break-all rounded-lg border border-slate-200 bg-white p-2 font-mono text-slate-800">{v}</div>
                  </div>
                ))}
                <Button className="h-9 w-full rounded-xl bg-emerald-600 text-white cursor-pointer mt-1" onClick={() => void testNode(selectedNode)} disabled={saving || pool.node_mode === "managed_mihomo" || (!selectedNode.proxy_url && !pool.mihomo?.enabled)}>
                  <Zap className="size-4" />{Z.test}
                </Button>
              </div>
            )}
          </div> : null}

          <div className="rounded-xl border border-slate-200 bg-white p-3">
            <div className="mb-2 flex items-center justify-between">
              <div className="text-sm font-semibold">{Z.subList}</div>
              <Badge variant="secondary" className="rounded-md">
                {pool.subscriptions.length} 条
              </Badge>
            </div>
            <div className="max-h-[320px] space-y-2 overflow-auto">
              {!pool.subscriptions.length ? (
                <div className="text-sm text-slate-400 py-4 text-center">无订阅列表</div>
              ) : (
                pool.subscriptions.map((item) => (
                  <div key={item.id} className="rounded-xl border border-slate-200 bg-slate-50 p-2 text-xs">
                    <div className="flex items-center justify-between gap-2">
                      <div className="flex min-w-0 items-start gap-2">
                        <Checkbox checked={item.enabled} onCheckedChange={(checked) => patchSubscription(item.id, Boolean(checked))} />
                        <div className="min-w-0">
                        <div className="truncate font-semibold text-slate-900">{item.name}</div>
                        <div className="truncate font-mono text-slate-500 mt-0.5">{item.url}</div>
                        </div>
                      </div>
                      <Button variant="ghost" className="h-7 px-2 text-rose-600 cursor-pointer" onClick={() => void removeSubscription(item.id)} disabled={saving}>
                        <Trash2 className="size-3.5" />
                      </Button>
                    </div>
                    {item.last_error ? (
                      <div className="mt-1 rounded bg-rose-50 p-1 text-rose-700 font-mono text-[10px] break-all">{item.last_error}</div>
                    ) : (
                      <div className="mt-1 text-slate-500 text-[11px]">{Z.nodes}: {item.last_imported || 0}</div>
                    )}
                  </div>
                ))
              )}
            </div>
          </div>
        </aside>
      </CardContent>
    </Card>
  );
}
