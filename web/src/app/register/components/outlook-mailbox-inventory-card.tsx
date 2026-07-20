"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowDownAZ,
  Check,
  ChevronDown,
  ChevronUp,
  Clock3,
  Copy,
  Download,
  Eye,
  EyeOff,
  Folder,
  FolderCog,
  FolderPlus,
  Inbox,
  KeyRound,
  LayoutList,
  List,
  LoaderCircle,
  Mail,
  MailOpen,
  Pencil,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  createMailboxGroup,
  deleteMailbox,
  deleteMailboxGroup,
  fetchMailboxBatches,
  fetchOutlookMailboxMessages,
  fetchOutlookMailboxPool,
  importMailboxes,
  outboundMailboxes,
  refreshOutlookMailboxTokens,
  renameMailboxGroup,
  resetMailboxStates,
  updateMailboxGroups,
  type MailboxCodeMessage,
  type MailboxItem,
  type MailboxPool,
  type InventoryBatch,
  type OutlookTokenRefreshResult,
} from "@/lib/api";

import { useSettingsStore } from "../store";

const stateText: Record<string, string> = {
  unused: "未使用",
  in_use: "占用中",
  used: "已使用",
  failed: "注册失败",
  token_invalid: "令牌失效",
  region_blocked: "地区受限",
  existing_account: "已有账号",
  flow_blocked: "流程受阻",
  cloudflare_blocked: "Cloudflare 拦截",
};

const avatarStyles = [
  "bg-sky-600 text-white",
  "bg-emerald-600 text-white",
  "bg-violet-600 text-white",
  "bg-amber-500 text-white",
  "bg-rose-600 text-white",
  "bg-cyan-700 text-white",
];

function stateVariant(state: string) {
  if (state === "unused") return "success" as const;
  if (["in_use", "region_blocked", "flow_blocked", "cloudflare_blocked"].includes(state)) return "warning" as const;
  if (state === "used") return "secondary" as const;
  return "danger" as const;
}

function formatTime(value?: string) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function itemTimestamp(item: MailboxItem) {
  const value = new Date(item.token_refreshed_at || item.updated_at || item.created_at || 0).getTime();
  return Number.isNaN(value) ? 0 : value;
}

function groupName(item?: Pick<MailboxItem, "group"> | null) {
  return String(item?.group || "默认");
}

function avatarStyle(value: string) {
  const total = Array.from(value).reduce((sum, char) => sum + char.charCodeAt(0), 0);
  return avatarStyles[total % avatarStyles.length];
}

function messageKey(message: MailboxCodeMessage, index: number) {
  return String(message.message_id || `${message.received_at || "message"}-${index}`);
}

function messageRecipients(message: MailboxCodeMessage | null, fallback = "") {
  const recipients = (message?.recipients || []).map((value) => String(value || "").trim()).filter(Boolean);
  return recipients.length ? recipients.join(", ") : String(message?.mailbox || fallback || "-");
}

function saveTextFile(name: string, text: string) {
  const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

async function writeClipboardText(value: string) {
  try {
    await navigator.clipboard.writeText(value);
    return;
  } catch {
    const textarea = document.createElement("textarea");
    textarea.value = value;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.left = "-9999px";
    document.body.appendChild(textarea);
    textarea.select();
    const copied = document.execCommand("copy");
    textarea.remove();
    if (!copied) throw new Error("clipboard unavailable");
  }
}

export function OutlookMailboxInventoryCard({ active = true }: { active?: boolean }) {
  const loadedRef = useRef(false);
  const fullPoolLoadedRef = useRef(false);
  const [pool, setPool] = useState<MailboxPool | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [selectedEmail, setSelectedEmail] = useState("");
  const [selectedEmails, setSelectedEmails] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [groupQuery, setGroupQuery] = useState("");
  const [groupFilter, setGroupFilter] = useState("all");
  const [stateFilter, setStateFilter] = useState("all");
  const [sortMode, setSortMode] = useState<"updated" | "email">("updated");
  const [viewMode, setViewMode] = useState<"standard" | "compact">("standard");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [showSecret, setShowSecret] = useState(false);
  const [showImport, setShowImport] = useState(false);
  const [showOutbound, setShowOutbound] = useState(false);
  const [showGroupCreator, setShowGroupCreator] = useState(false);
  const [importText, setImportText] = useState("");
  const [importGroup, setImportGroup] = useState("默认");
  const [batchGroup, setBatchGroup] = useState("默认");
  const [newGroupName, setNewGroupName] = useState("");
  const [managedGroup, setManagedGroup] = useState("默认");
  const [renamedGroup, setRenamedGroup] = useState("");
  const [mailMode, setMailMode] = useState<"auto" | "graph" | "imap">("auto");
  const [mailLoading, setMailLoading] = useState(false);
  const [messages, setMessages] = useState<MailboxCodeMessage[]>([]);
  const [messageEmail, setMessageEmail] = useState("");
  const [selectedMessageKey, setSelectedMessageKey] = useState("");
  const [messageDetailOpen, setMessageDetailOpen] = useState(true);
  const [copiedCode, setCopiedCode] = useState("");
  const [refreshingTokens, setRefreshingTokens] = useState(false);
  const [refreshResults, setRefreshResults] = useState<OutlookTokenRefreshResult[]>([]);
  const [batches, setBatches] = useState<InventoryBatch[]>([]);
  const [outboundBatch, setOutboundBatch] = useState("");
  const [outboundQuantity, setOutboundQuantity] = useState(10);
  const [outboundFormat, setOutboundFormat] = useState<"credentials" | "json">("credentials");
  const setRegisterConfig = useSettingsStore((state) => state.setRegisterConfig);

  const selected = useMemo(() => pool?.items.find((item) => item.email === selectedEmail), [pool, selectedEmail]);
  const groups = useMemo(() => {
    const values = new Set<string>(["默认"]);
    (pool?.groups || []).forEach((group) => values.add(String(group || "默认")));
    (pool?.items || []).forEach((item) => values.add(groupName(item)));
    return Array.from(values).filter(Boolean).sort((a, b) => a.localeCompare(b, "zh-Hans-CN"));
  }, [pool]);
  const groupRecords = useMemo(
    () => pool?.group_records || groups.map((name) => ({ name, count: (pool?.items || []).filter((item) => groupName(item) === name).length })),
    [groups, pool],
  );
  const visibleGroupRecords = useMemo(() => {
    const text = groupQuery.trim().toLowerCase();
    return groupRecords.filter((group) => !text || group.name.toLowerCase().includes(text));
  }, [groupQuery, groupRecords]);
  const filtered = useMemo(() => {
    const text = query.trim().toLowerCase();
    return (pool?.items || [])
      .filter((item) => {
        const group = groupName(item);
        const searchText = `${item.email} ${group} ${item.state_reason || ""} ${item.token_refresh_error || ""}`.toLowerCase();
        return (groupFilter === "all" || group === groupFilter)
          && (stateFilter === "all" || item.state === stateFilter)
          && (!text || searchText.includes(text));
      })
      .sort((left, right) => sortMode === "email"
        ? left.email.localeCompare(right.email)
        : itemTimestamp(right) - itemTimestamp(left));
  }, [groupFilter, pool, query, sortMode, stateFilter]);
  const effectivePageSize = pageSize <= 0 ? Math.max(1, filtered.length) : pageSize;
  const totalPages = Math.max(1, Math.ceil(filtered.length / effectivePageSize));
  const currentPage = Math.min(page, totalPages);
  const pageItems = filtered.slice((currentPage - 1) * effectivePageSize, currentPage * effectivePageSize);
  const pageEmails = pageItems.map((item) => item.email);
  const allPageSelected = pageEmails.length > 0 && pageEmails.every((email) => selectedEmails.includes(email));
  const selectedMessage = useMemo(
    () => messages.find((message, index) => messageKey(message, index) === selectedMessageKey) || messages[0] || null,
    [messages, selectedMessageKey],
  );
  const refreshSummary = useMemo(() => ({
    success: refreshResults.filter((result) => result.ok).length,
    failed: refreshResults.filter((result) => !result.ok).length,
  }), [refreshResults]);

  useEffect(() => { if (page > totalPages) setPage(totalPages); }, [page, totalPages]);
  useEffect(() => {
    if (!groups.includes(managedGroup)) setManagedGroup(groups[0] || "默认");
    if (!groups.includes(batchGroup)) setBatchGroup(groups[0] || "默认");
    if (!groups.includes(importGroup)) setImportGroup(groups[0] || "默认");
  }, [batchGroup, groups, importGroup, managedGroup]);
  useEffect(() => { setRenamedGroup(managedGroup); }, [managedGroup]);

  const load = async (silent = false, preview = false) => {
    if (!silent) setLoading(true);
    try {
      const [data, batchData] = await Promise.all([
        fetchOutlookMailboxPool({ force: silent, preview }),
        fetchMailboxBatches().catch(() => ({ batches: [] as InventoryBatch[] })),
      ]);
      setPool(data.mailboxes);
      setBatches(batchData.batches);
      fullPoolLoadedRef.current = !data.mailboxes.page_info?.partial;
      setSelectedEmails((current) => current.filter((email) => data.mailboxes.items.some((item) => item.email === email)));
      setSelectedEmail((current) => current && data.mailboxes.items.some((item) => item.email === current) ? current : data.mailboxes.items[0]?.email || "");
    } catch (error) {
      if (!silent) toast.error(error instanceof Error ? error.message : "加载微软邮箱库存失败");
    } finally {
      if (!silent) setLoading(false);
    }
  };

  useEffect(() => {
    if (!active) return;
    const firstLoad = !loadedRef.current;
    if (firstLoad) void load(false, true).finally(() => { loadedRef.current = true; });
    else setLoading(false);
    const timer = window.setInterval(() => {
      if (!fullPoolLoadedRef.current) void load(true, true);
    }, 30000);
    return () => window.clearInterval(timer);
  }, [active]);

  useEffect(() => {
    const hasActiveFilter = Boolean(query.trim()) || groupFilter !== "all" || stateFilter !== "all";
    if (!active || loading || !pool?.page_info?.partial || !hasActiveFilter) return;
    const timer = window.setTimeout(() => { void load(false, false); }, 250);
    return () => window.clearTimeout(timer);
  }, [active, groupFilter, loading, pool?.page_info?.partial, query, stateFilter]);

  const toggleEmail = (email: string) => setSelectedEmails((current) => current.includes(email) ? current.filter((item) => item !== email) : [...current, email]);
  const togglePage = () => setSelectedEmails((current) => allPageSelected ? current.filter((email) => !pageEmails.includes(email)) : Array.from(new Set([...current, ...pageEmails])));

  const selectGroup = (group: string) => {
    setGroupFilter(group);
    setPage(1);
    const groupItems = (pool?.items || []).filter((item) => group === "all" || groupName(item) === group);
    if (!groupItems.some((item) => item.email === selectedEmail)) {
      setSelectedEmail(groupItems[0]?.email || "");
      setMessages([]);
      setMessageEmail("");
      setSelectedMessageKey("");
    }
    if (group !== "all") {
      setManagedGroup(group);
      setBatchGroup(group);
      setImportGroup(group);
    }
  };

  const selectMailbox = (email: string) => {
    setSelectedEmail(email);
    if (messageEmail !== email) {
      setMessages([]);
      setMessageEmail("");
      setSelectedMessageKey("");
    }
  };

  const doImport = async () => {
    if (!importText.trim()) return toast.error("请粘贴微软邮箱数据");
    setSaving(true);
    try {
      const data = await importMailboxes(importText, "", "outlook_token", importGroup.trim() || "默认");
      setPool({ ...data.mailboxes, items: data.mailboxes.items.filter((item) => item.provider_type === "outlook_token") });
      setRegisterConfig(data.register);
      setImportText("");
      setShowImport(false);
      toast.success(`微软邮箱已导入到「${importGroup.trim() || "默认"}」`);
      await load(true);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "微软邮箱导入失败");
    } finally {
      setSaving(false);
    }
  };

  const moveSelectedGroup = async () => {
    if (!selectedEmails.length) return toast.error("请先选择邮箱");
    const group = batchGroup.trim() || "默认";
    setSaving(true);
    try {
      const data = await updateMailboxGroups(selectedEmails, group, "outlook_token");
      setRegisterConfig(data.register);
      setGroupFilter(group);
      toast.success(`已移动 ${selectedEmails.length} 个邮箱到「${group}」`);
      await load(true);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "更新分组失败");
    } finally {
      setSaving(false);
    }
  };

  const createGroup = async () => {
    const name = newGroupName.trim();
    if (!name) return toast.error("请输入新分组名称");
    setSaving(true);
    try {
      const data = await createMailboxGroup(name);
      setRegisterConfig(data.register);
      setNewGroupName("");
      setShowGroupCreator(false);
      setManagedGroup(name);
      setBatchGroup(name);
      setImportGroup(name);
      setGroupFilter(name);
      await load(true);
      toast.success(`分组「${name}」已创建`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "创建分组失败");
    } finally {
      setSaving(false);
    }
  };

  const renameGroup = async () => {
    const target = renamedGroup.trim();
    if (managedGroup === "默认") return toast.error("默认分组不能重命名");
    if (!target || target === managedGroup) return;
    setSaving(true);
    try {
      const previous = managedGroup;
      const data = await renameMailboxGroup(previous, target);
      setRegisterConfig(data.register);
      setManagedGroup(target);
      setBatchGroup((current) => current === previous ? target : current);
      setImportGroup((current) => current === previous ? target : current);
      setGroupFilter((current) => current === previous ? target : current);
      await load(true);
      toast.success(`分组已重命名为「${target}」`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "重命名分组失败");
    } finally {
      setSaving(false);
    }
  };

  const removeGroup = async () => {
    if (managedGroup === "默认") return toast.error("默认分组不能删除");
    if (!window.confirm(`删除分组「${managedGroup}」？组内邮箱会迁移到“默认”。`)) return;
    setSaving(true);
    try {
      const data = await deleteMailboxGroup(managedGroup, "默认");
      setRegisterConfig(data.register);
      setManagedGroup("默认");
      setBatchGroup("默认");
      setImportGroup("默认");
      setGroupFilter("默认");
      await load(true);
      toast.success("分组已删除，组内邮箱已迁移到默认分组");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "删除分组失败");
    } finally {
      setSaving(false);
    }
  };

  const resetStates = async (scope: "failed" | "all") => {
    setSaving(true);
    try {
      const data = await resetMailboxStates(scope);
      setRegisterConfig(data.register);
      await load(true);
      toast.success(scope === "all" ? "已重置微软邮箱状态" : "已释放失败和受阻邮箱");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "重置状态失败");
    } finally {
      setSaving(false);
    }
  };

  const deleteSelected = async () => {
    if (!selectedEmails.length) return;
    if (!window.confirm(`删除选中的 ${selectedEmails.length} 个微软邮箱？`)) return;
    setSaving(true);
    try {
      let lastRegister: unknown = null;
      for (const email of selectedEmails) {
        const data = await deleteMailbox(email);
        lastRegister = data.register;
      }
      if (lastRegister) setRegisterConfig(lastRegister as never);
      setSelectedEmails([]);
      setSelectedEmail("");
      setMessages([]);
      setMessageEmail("");
      await load(true);
      toast.success("已删除选中微软邮箱");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "删除邮箱失败");
    } finally {
      setSaving(false);
    }
  };

  const exportSelected = () => {
    const items = (pool?.items || []).filter((item) => selectedEmails.includes(item.email));
    const text = items.map((item) => item.line || `${item.email}----${item.password || ""}----${item.client_id || ""}----${item.refresh_token || ""}`).join("\n");
    if (!text) return toast.error("请先选择要导出的邮箱");
    saveTextFile(`outlook-mailboxes-${Date.now()}.txt`, `${text}\n`);
  };

  const outboundInventory = async () => {
    setSaving(true);
    try {
      const data = await outboundMailboxes({
        quantity: Math.max(0, outboundQuantity),
        inventory_batch_id: outboundBatch,
        provider_type: "outlook_token",
        format: outboundFormat,
      });
      setBatches(data.batches);
      const payload = typeof data.mailbox_outbound.data === "string"
        ? data.mailbox_outbound.data
        : JSON.stringify(data.mailbox_outbound.data, null, 2);
      saveTextFile(data.mailbox_outbound.filename || `mailboxes-${Date.now()}.txt`, `${payload}\n`);
      toast.success(`已出库 ${data.mailbox_outbound.count} 个邮箱`);
      await load(true);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "邮箱出库失败");
    } finally {
      setSaving(false);
    }
  };

  const fetchMessages = async (email = selectedEmail) => {
    if (!email) return toast.error("请先选择邮箱");
    setSelectedEmail(email);
    setMailLoading(true);
    setMessages([]);
    setSelectedMessageKey("");
    try {
      const data = await fetchOutlookMailboxMessages(email, { limit: 20, mode: mailMode, request_timeout: 30 });
      const nextMessages = data.mailbox_messages.messages || [];
      setMessages(nextMessages);
      setMessageEmail(email);
      setSelectedMessageKey(nextMessages.length ? messageKey(nextMessages[0], 0) : "");
      setMessageDetailOpen(Boolean(nextMessages.length));
      toast.success(`已读取 ${data.mailbox_messages.count || 0} 封最近邮件`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "读取微软邮箱失败");
    } finally {
      setMailLoading(false);
    }
  };

  const copyVerificationCode = async (code?: string) => {
    const value = String(code || "").trim();
    if (!value) return;
    try {
      await writeClipboardText(value);
      setCopiedCode(value);
      toast.success(`验证码 ${value} 已复制`);
      window.setTimeout(() => setCopiedCode((current) => current === value ? "" : current), 1600);
    } catch {
      toast.error("验证码复制失败");
    }
  };

  const refreshTokens = async (emails: string[]) => {
    const targets = Array.from(new Set(emails.filter(Boolean)));
    if (!targets.length) return toast.error("请先选择要刷新令牌的邮箱");
    setRefreshingTokens(true);
    setRefreshResults([]);
    try {
      const data = await refreshOutlookMailboxTokens(targets, { mode: mailMode, request_timeout: 30, concurrency: 5 });
      setPool(data.mailboxes);
      setRefreshResults(data.results || []);
      const success = Number(data.summary.success || 0);
      const failed = Number(data.summary.failed || 0);
      if (failed) toast.warning(`令牌刷新完成：成功 ${success}，失败 ${failed}`);
      else toast.success(`令牌刷新成功：${success} 个`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "刷新微软令牌失败");
    } finally {
      setRefreshingTokens(false);
    }
  };

  const stats = pool?.stats || {};
  const activeGroupLabel = groupFilter === "all" ? "全部邮箱" : groupFilter;

  return (
    <section className="overflow-hidden rounded-lg border border-slate-200 bg-white shadow-sm">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 px-4 py-3">
        <div className="flex min-w-0 items-center gap-3">
          <div className="flex size-10 shrink-0 items-center justify-center rounded-lg bg-sky-50 text-sky-700">
            <Inbox className="size-5" />
          </div>
          <div className="min-w-0">
            <h2 className="truncate text-lg font-bold text-slate-950">微软邮箱库存</h2>
            <p className="truncate text-xs text-slate-500">Outlook / Hotmail OAuth 邮箱管理</p>
          </div>
          <div className="hidden items-center gap-3 border-l border-slate-200 pl-3 text-xs text-slate-500 lg:flex">
            <span>总数 <strong className="ml-1 text-slate-900">{stats.total || 0}</strong></span>
            <span>未使用 <strong className="ml-1 text-emerald-700">{stats.unused || 0}</strong></span>
            <span>令牌失效 <strong className="ml-1 text-rose-700">{stats.token_invalid || 0}</strong></span>
          </div>
        </div>

        <div className="flex flex-wrap items-center justify-end gap-2">
          <div className="flex h-9 items-center rounded-lg border border-slate-200 bg-slate-50 p-0.5">
            <button
              type="button"
              className={`flex h-8 items-center gap-1.5 rounded-md px-3 text-xs font-semibold ${viewMode === "standard" ? "bg-white text-slate-900 shadow-sm" : "text-slate-500"}`}
              onClick={() => setViewMode("standard")}
            >
              <LayoutList className="size-3.5" />标准
            </button>
            <button
              type="button"
              className={`flex h-8 items-center gap-1.5 rounded-md px-3 text-xs font-semibold ${viewMode === "compact" ? "bg-white text-slate-900 shadow-sm" : "text-slate-500"}`}
              onClick={() => setViewMode("compact")}
            >
              <List className="size-3.5" />紧凑
            </button>
          </div>
          <Button className="h-9 rounded-lg bg-sky-600 px-3 text-white hover:bg-sky-700" onClick={() => setShowImport((value) => !value)}>
            {showImport ? <X className="size-4" /> : <Plus className="size-4" />}{showImport ? "关闭导入" : "添加账号"}
          </Button>
          <Button variant="outline" className="h-9 rounded-lg px-3" onClick={exportSelected} disabled={!selectedEmails.length}>
            <Download className="size-4" />导出
          </Button>
          <Button variant="outline" className="h-9 rounded-lg px-3" onClick={() => setShowOutbound((value) => !value)}>
            <Download className="size-4" />库存出库
          </Button>
          <Button variant="outline" className="h-9 rounded-lg px-3" onClick={() => void refreshTokens(selectedEmails)} disabled={refreshingTokens || !selectedEmails.length}>
            {refreshingTokens ? <LoaderCircle className="size-4 animate-spin" /> : <KeyRound className="size-4" />}刷新 Token
          </Button>
          <Button variant="outline" className="size-9 rounded-lg p-0" onClick={() => setShowSecret((value) => !value)} title={showSecret ? "隐藏凭据" : "显示凭据"} aria-label={showSecret ? "隐藏凭据" : "显示凭据"}>
            {showSecret ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
          </Button>
          <Button variant="outline" className={pool?.page_info?.partial ? "h-9 rounded-lg px-3 text-xs" : "size-9 rounded-lg p-0"} onClick={() => void load()} disabled={loading || saving} title={pool?.page_info?.partial ? `加载全部 ${pool.page_info.total} 个邮箱` : "刷新库存"} aria-label={pool?.page_info?.partial ? "加载全部邮箱" : "刷新库存"}>
            <RefreshCw className={`size-4 ${loading ? "animate-spin" : ""}`} />
            {pool?.page_info?.partial ? `加载全部 ${pool.page_info.total}` : null}
          </Button>
        </div>
      </header>

      {showImport ? (
        <div className="grid gap-3 border-b border-slate-200 bg-sky-50/60 p-4 lg:grid-cols-[220px_minmax(320px,1fr)_160px]">
          <div>
            <label className="mb-1.5 block text-xs font-semibold text-slate-600">导入分组</label>
            <select value={importGroup} onChange={(event) => setImportGroup(event.target.value)} className="h-10 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm">
              {groups.map((group) => <option key={group} value={group}>{group}</option>)}
            </select>
          </div>
          <div>
            <label className="mb-1.5 block text-xs font-semibold text-slate-600">微软邮箱数据</label>
            <Textarea value={importText} onChange={(event) => setImportText(event.target.value)} placeholder="email----password----client_id----refresh_token" className="min-h-20 rounded-lg bg-white font-mono text-xs" />
          </div>
          <div className="flex items-end">
            <Button className="h-10 w-full rounded-lg bg-sky-600 text-white hover:bg-sky-700" onClick={() => void doImport()} disabled={saving}>
              {saving ? <LoaderCircle className="size-4 animate-spin" /> : <Upload className="size-4" />}导入库存
            </Button>
          </div>
        </div>
      ) : null}

      {showOutbound ? (
        <div className="grid gap-3 border-b border-slate-200 bg-emerald-50/60 p-4 md:grid-cols-[minmax(240px,1fr)_120px_150px_auto]">
          <label className="space-y-1.5">
            <span className="text-xs font-semibold text-slate-600">导入批次</span>
            <select value={outboundBatch} onChange={(event) => setOutboundBatch(event.target.value)} className="h-10 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm">
              <option value="">全部未出库邮箱</option>
              {batches.map((batch) => <option key={batch.id} value={batch.id}>{batch.id} · 可出 {batch.available}/{batch.total}</option>)}
            </select>
          </label>
          <label className="space-y-1.5">
            <span className="text-xs font-semibold text-slate-600">出库数量</span>
            <Input type="number" min={0} value={outboundQuantity} onChange={(event) => setOutboundQuantity(Math.max(0, Number(event.target.value) || 0))} className="h-10 bg-white" />
          </label>
          <label className="space-y-1.5">
            <span className="text-xs font-semibold text-slate-600">导出格式</span>
            <select value={outboundFormat} onChange={(event) => setOutboundFormat(event.target.value as "credentials" | "json")} className="h-10 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm">
              <option value="credentials">邮箱凭据文本</option>
              <option value="json">完整 JSON</option>
            </select>
          </label>
          <div className="flex items-end">
            <Button className="h-10 bg-emerald-700 text-white hover:bg-emerald-800" onClick={() => void outboundInventory()} disabled={saving}>
              {saving ? <LoaderCircle className="size-4 animate-spin" /> : <Download className="size-4" />}确认出库
            </Button>
          </div>
        </div>
      ) : null}

      <div className="grid min-h-0 xl:h-[clamp(540px,calc(100vh-220px),640px)] xl:grid-cols-[180px_minmax(280px,0.9fr)_minmax(300px,1.35fr)] xl:overflow-hidden">
        <aside className="flex h-[320px] min-h-0 flex-col border-b border-slate-200 bg-slate-50/60 xl:h-auto xl:border-b-0 xl:border-r">
          <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
            <div className="flex items-center gap-2 text-sm font-bold text-slate-900"><FolderCog className="size-4 text-sky-600" />分组</div>
            <Button variant="outline" className="size-8 rounded-lg p-0" onClick={() => setShowGroupCreator((value) => !value)} title="新建分组" aria-label="新建分组">
              {showGroupCreator ? <X className="size-3.5" /> : <FolderPlus className="size-3.5" />}
            </Button>
          </div>

          <div className="space-y-2 border-b border-slate-200 p-3">
            <div className="relative">
              <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-slate-400" />
              <Input value={groupQuery} onChange={(event) => setGroupQuery(event.target.value)} placeholder="搜索分组" className="h-9 rounded-lg bg-white pl-9" />
            </div>
            {showGroupCreator ? (
              <div className="grid grid-cols-[1fr_auto] gap-2">
                <Input value={newGroupName} onChange={(event) => setNewGroupName(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void createGroup(); }} placeholder="新分组名称" className="h-9 rounded-lg bg-white" />
                <Button className="size-9 rounded-lg bg-sky-600 p-0 text-white hover:bg-sky-700" onClick={() => void createGroup()} disabled={saving} title="创建分组" aria-label="创建分组"><Plus className="size-4" /></Button>
              </div>
            ) : null}
          </div>

          <div className="min-h-0 flex-1 overflow-auto overscroll-contain py-2 [scrollbar-gutter:stable]">
            <button
              type="button"
              onClick={() => selectGroup("all")}
              className={`flex w-full items-center gap-3 border-l-2 px-4 py-3 text-left text-sm ${groupFilter === "all" ? "border-sky-600 bg-sky-50 font-semibold text-sky-950" : "border-transparent text-slate-700 hover:bg-white"}`}
            >
              <span className={`size-2.5 rounded-full ${groupFilter === "all" ? "bg-sky-600" : "bg-slate-400"}`} />
              <span className="min-w-0 flex-1 truncate">全部邮箱</span>
              <span className="rounded-full bg-white px-2 py-0.5 text-xs font-semibold text-slate-600">{stats.total || 0}</span>
            </button>
            {visibleGroupRecords.map((group, index) => (
              <button
                key={group.name}
                type="button"
                onClick={() => selectGroup(group.name)}
                className={`flex w-full items-center gap-3 border-l-2 px-4 py-3 text-left text-sm ${groupFilter === group.name ? "border-sky-600 bg-sky-50 font-semibold text-sky-950" : "border-transparent text-slate-700 hover:bg-white"}`}
              >
                <span className={`size-2.5 rounded-full ${avatarStyles[index % avatarStyles.length].split(" ")[0]}`} />
                <span className="min-w-0 flex-1 truncate">{group.name}</span>
                <span className="rounded-full bg-white px-2 py-0.5 text-xs font-semibold text-slate-600">{group.count}</span>
              </button>
            ))}
          </div>

          <div className="space-y-2 border-t border-slate-200 p-3">
            {groupFilter !== "all" ? (
              <div className="grid grid-cols-[1fr_auto_auto] gap-1.5">
                <Input value={renamedGroup} onChange={(event) => setRenamedGroup(event.target.value)} disabled={managedGroup === "默认"} className="h-8 rounded-lg bg-white text-xs" />
                <Button variant="outline" className="size-8 rounded-lg p-0" onClick={() => void renameGroup()} disabled={saving || managedGroup === "默认" || !renamedGroup.trim() || renamedGroup.trim() === managedGroup} title="重命名分组" aria-label="重命名分组"><Pencil className="size-3.5" /></Button>
                <Button variant="outline" className="size-8 rounded-lg border-rose-200 bg-rose-50 p-0 text-rose-700" onClick={() => void removeGroup()} disabled={saving || managedGroup === "默认"} title="删除分组" aria-label="删除分组"><Trash2 className="size-3.5" /></Button>
              </div>
            ) : null}
            <div className="grid grid-cols-2 gap-1.5">
              <Button variant="outline" className="h-8 rounded-lg px-2 text-xs" onClick={() => void resetStates("failed")} disabled={saving}><RotateCcw className="size-3.5" />释放失败</Button>
              <Button variant="outline" className="h-8 rounded-lg px-2 text-xs" onClick={() => void resetStates("all")} disabled={saving}><RefreshCw className="size-3.5" />重置状态</Button>
            </div>
          </div>
        </aside>

        <section className="flex h-[560px] min-h-0 min-w-0 flex-col border-b border-slate-200 xl:h-auto xl:border-b-0 xl:border-r">
          <div className="border-b border-slate-200 px-4 py-3">
            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0">
                <h3 className="truncate text-sm font-bold text-slate-950">{activeGroupLabel}</h3>
                <p className="mt-0.5 text-xs text-slate-500">{filtered.length} 个邮箱</p>
              </div>
              <div className="flex items-center gap-1">
                <button type="button" onClick={() => setSortMode("updated")} className={`flex h-8 items-center gap-1 rounded-md px-2 text-xs font-semibold ${sortMode === "updated" ? "bg-sky-50 text-sky-700" : "text-slate-500 hover:bg-slate-50"}`}><Clock3 className="size-3.5" />刷新时间</button>
                <button type="button" onClick={() => setSortMode("email")} className={`flex h-8 items-center gap-1 rounded-md px-2 text-xs font-semibold ${sortMode === "email" ? "bg-sky-50 text-sky-700" : "text-slate-500 hover:bg-slate-50"}`}><ArrowDownAZ className="size-3.5" />邮箱名</button>
              </div>
            </div>
            <div className="mt-3 grid grid-cols-[minmax(0,1fr)_118px] gap-2">
              <div className="relative">
                <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-slate-400" />
                <Input value={query} onChange={(event) => { setQuery(event.target.value); setPage(1); }} placeholder="搜索邮箱" className="h-9 rounded-lg pl-9" />
              </div>
              <select value={stateFilter} onChange={(event) => { setStateFilter(event.target.value); setPage(1); }} className="h-9 rounded-lg border border-slate-200 bg-white px-2 text-xs">
                <option value="all">全部状态</option>
                {Object.entries(stateText).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
            </div>
          </div>

          <div className="flex items-center justify-between border-b border-slate-200 bg-slate-50 px-4 py-2 text-xs text-slate-600">
            <label className="flex items-center gap-2 font-semibold"><Checkbox checked={allPageSelected} onCheckedChange={togglePage} />全选本页</label>
            <span>第 {currentPage}/{totalPages} 页</span>
          </div>

          <div className="min-h-0 flex-1 overflow-auto overscroll-contain [scrollbar-gutter:stable]" data-testid="mailbox-list-scroll">
            {loading ? <div className="flex min-h-48 items-center justify-center text-sm text-slate-500"><LoaderCircle className="mr-2 size-5 animate-spin" />正在加载邮箱库存</div> : null}
            {!loading && !pageItems.length ? <div className="flex min-h-48 flex-col items-center justify-center gap-2 text-sm text-slate-500"><Mail className="size-7 text-slate-300" />当前分组没有匹配邮箱</div> : null}
            {pageItems.map((item) => {
              const active = selectedEmail === item.email;
              const tokenOk = item.token_refresh_status === "success";
              const tokenFailed = item.token_refresh_status === "failed";
              return (
                <div key={item.email} className={`border-b border-slate-100 ${active ? "bg-sky-50" : "bg-white hover:bg-slate-50/70"}`}>
                  <div className={`flex gap-3 px-3 ${viewMode === "compact" ? "py-2" : "py-3"}`}>
                    <div className="pt-1"><Checkbox checked={selectedEmails.includes(item.email)} onCheckedChange={() => toggleEmail(item.email)} /></div>
                    <button type="button" className="flex min-w-0 flex-1 gap-3 text-left" onClick={() => selectMailbox(item.email)}>
                      <span className={`flex shrink-0 items-center justify-center rounded-full font-bold ${avatarStyle(item.email)} ${viewMode === "compact" ? "size-8 text-xs" : "size-10 text-sm"}`}>{item.email.charAt(0).toUpperCase()}</span>
                      <span className="min-w-0 flex-1">
                        <span className="flex min-w-0 items-center justify-between gap-2">
                          <span className="truncate text-sm font-semibold text-slate-900">{item.email}</span>
                          <Badge variant={stateVariant(item.state)} className="shrink-0 rounded-md">{stateText[item.state] || item.state}</Badge>
                        </span>
                        <span className="mt-1 flex flex-wrap items-center gap-2 text-xs text-slate-500">
                          <span className="rounded bg-slate-100 px-1.5 py-0.5 font-semibold text-slate-600">Outlook</span>
                          <span className={`rounded px-1.5 py-0.5 font-semibold ${tokenOk ? "bg-emerald-50 text-emerald-700" : tokenFailed ? "bg-rose-50 text-rose-700" : "bg-slate-100 text-slate-500"}`}>{tokenOk ? item.token_refresh_mode || "Token 有效" : tokenFailed ? "Token 失败" : "Token 未检查"}</span>
                          <span className="rounded bg-violet-50 px-1.5 py-0.5 font-semibold text-violet-700">分裂 {item.split_alias_generated || 0} · 成功 {item.split_alias_used || 0}</span>
                          <span>{formatTime(item.token_refreshed_at || item.updated_at)}</span>
                        </span>
                        {viewMode === "standard" && (item.state_reason || item.token_refresh_error) ? <span className="mt-2 block line-clamp-2 text-xs leading-5 text-slate-500">{item.state_reason || item.token_refresh_error}</span> : null}
                      </span>
                    </button>
                    {viewMode === "standard" ? (
                      <div className="flex shrink-0 flex-col gap-1">
                        <Button variant="outline" className="size-8 rounded-lg p-0" onClick={() => void fetchMessages(item.email)} disabled={mailLoading} title="获取邮件" aria-label="获取邮件"><MailOpen className="size-3.5" /></Button>
                        <Button variant="outline" className="size-8 rounded-lg p-0" onClick={() => void refreshTokens([item.email])} disabled={refreshingTokens} title="刷新令牌" aria-label="刷新令牌"><KeyRound className="size-3.5" /></Button>
                      </div>
                    ) : null}
                  </div>
                </div>
              );
            })}
          </div>

          <div className="border-t border-slate-200 bg-white p-3">
            {selectedEmails.length ? (
              <div className="mb-3 flex flex-wrap items-center gap-2 rounded-lg bg-sky-50 p-2">
                <span className="px-1 text-xs font-bold text-sky-900">已选 {selectedEmails.length}</span>
                <select value={batchGroup} onChange={(event) => setBatchGroup(event.target.value)} className="h-8 min-w-28 flex-1 rounded-lg border border-sky-200 bg-white px-2 text-xs">{groups.map((group) => <option key={group} value={group}>{group}</option>)}</select>
                <Button variant="outline" className="h-8 rounded-lg border-sky-200 bg-white px-2 text-xs" onClick={() => void moveSelectedGroup()} disabled={saving}><Folder className="size-3.5" />移动</Button>
                <Button variant="outline" className="size-8 rounded-lg border-sky-200 bg-white p-0" onClick={() => void refreshTokens(selectedEmails)} disabled={refreshingTokens} title="刷新令牌" aria-label="刷新令牌"><KeyRound className="size-3.5" /></Button>
                <Button variant="outline" className="size-8 rounded-lg border-rose-200 bg-rose-50 p-0 text-rose-700" onClick={() => void deleteSelected()} disabled={saving} title="删除邮箱" aria-label="删除邮箱"><Trash2 className="size-3.5" /></Button>
              </div>
            ) : null}
            <div className="flex items-center justify-between gap-2">
              <select value={String(pageSize)} onChange={(event) => { setPageSize(Number(event.target.value)); setPage(1); }} className="h-8 rounded-lg border border-slate-200 bg-white px-2 text-xs"><option value="25">25/页</option><option value="50">50/页</option><option value="100">100/页</option><option value="0">全部</option></select>
              <div className="flex gap-1.5">
                <Button variant="outline" className="h-8 rounded-lg px-3 text-xs" onClick={() => setPage((value) => Math.max(1, value - 1))} disabled={currentPage <= 1}>上一页</Button>
                <Button variant="outline" className="h-8 rounded-lg px-3 text-xs" onClick={() => setPage((value) => Math.min(totalPages, value + 1))} disabled={currentPage >= totalPages}>下一页</Button>
              </div>
            </div>
          </div>
        </section>

        <section className="flex h-[620px] min-h-0 min-w-0 flex-col xl:h-auto" data-testid="mailbox-inbox-pane">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 px-4 py-3">
            <div className="flex items-center gap-1">
              <button type="button" className="flex h-9 items-center gap-2 border-b-2 border-sky-600 px-3 text-sm font-bold text-sky-700"><Inbox className="size-4" />收件箱</button>
              <Badge variant="secondary" className="rounded-md">{messages.length}</Badge>
            </div>
            <div className="flex items-center gap-2">
              <select value={mailMode} onChange={(event) => setMailMode(event.target.value as "auto" | "graph" | "imap")} className="h-9 rounded-lg border border-slate-200 bg-white px-2 text-xs"><option value="auto">自动 Graph → IMAP</option><option value="graph">仅 Graph</option><option value="imap">仅 IMAP</option></select>
              <Button className="h-9 rounded-lg bg-sky-600 px-3 text-white hover:bg-sky-700" onClick={() => void fetchMessages()} disabled={mailLoading || !selected}>{mailLoading ? <LoaderCircle className="size-4 animate-spin" /> : <MailOpen className="size-4" />}获取邮件</Button>
            </div>
          </div>

          <div className="border-b border-slate-200 bg-slate-50 px-4 py-2.5">
            {selected ? (
              <div className="flex min-w-0 items-center justify-between gap-3">
                <div className="min-w-0">
                  <div className="truncate text-sm font-semibold text-slate-900">当前邮箱：{selected.email}</div>
                  <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-slate-500"><span>{groupName(selected)}</span><span>·</span><span>{stateText[selected.state] || selected.state}</span><span>·</span><span>已生成分裂邮箱 {selected.split_alias_generated || 0} 个</span>{messageEmail === selected.email ? <><span>·</span><span>{messages.length} 封邮件</span></> : null}</div>
                </div>
                <Badge variant={stateVariant(selected.state)} className="shrink-0 rounded-md">{stateText[selected.state] || selected.state}</Badge>
              </div>
            ) : <div className="text-sm text-slate-500">未选择邮箱</div>}
            {showSecret && selected ? (
              <div className="mt-2 grid gap-2 text-xs md:grid-cols-2">
                <div className="min-w-0 rounded-md border border-slate-200 bg-white px-2 py-1.5"><span className="text-slate-400">Client ID </span><span className="break-all font-mono text-slate-700">{selected.client_id || "-"}</span></div>
                <div className="min-w-0 rounded-md border border-slate-200 bg-white px-2 py-1.5"><span className="text-slate-400">Refresh Token </span><span className="break-all font-mono text-slate-700">{selected.refresh_token || selected.refresh_token_preview || "-"}</span></div>
              </div>
            ) : null}
          </div>

          {refreshResults.length ? (
            <div className="flex items-center justify-between gap-3 border-b border-slate-200 bg-emerald-50 px-4 py-2 text-xs">
              <span className="font-semibold text-emerald-900">最近令牌刷新：成功 {refreshSummary.success}，失败 {refreshSummary.failed}</span>
              {refreshSummary.failed ? <span className="max-w-[55%] truncate text-rose-700">{refreshResults.find((result) => !result.ok)?.error || "部分邮箱刷新失败"}</span> : <span className="text-emerald-700">刷新完成</span>}
            </div>
          ) : null}

          <div className="flex min-h-0 flex-1 flex-col">
            <div className="min-h-[260px] flex-1 overflow-auto overscroll-contain [scrollbar-gutter:stable] xl:min-h-0" data-testid="message-list-scroll">
              {mailLoading ? <div className="flex h-full min-h-64 items-center justify-center text-sm text-slate-500"><LoaderCircle className="mr-2 size-5 animate-spin" />正在获取最近邮件</div> : null}
              {!mailLoading && !messageEmail ? <div className="flex h-full min-h-64 flex-col items-center justify-center gap-3 text-sm text-slate-500"><MailOpen className="size-8 text-slate-300" />选择邮箱后获取邮件</div> : null}
              {!mailLoading && messageEmail && !messages.length ? <div className="flex h-full min-h-64 flex-col items-center justify-center gap-3 text-sm text-slate-500"><Inbox className="size-8 text-slate-300" />没有读取到最近邮件</div> : null}
              {!mailLoading && messages.map((message, index) => {
                const key = messageKey(message, index);
                const active = selectedMessage ? messageKey(selectedMessage, messages.indexOf(selectedMessage)) === key : false;
                return (
                  <div key={key} className={`flex w-full items-center gap-3 border-b border-slate-100 px-4 py-3 last:border-b-0 ${active ? "bg-sky-50" : "bg-white hover:bg-slate-50"}`}>
                    <button type="button" onClick={() => { setSelectedMessageKey(key); setMessageDetailOpen(true); }} className="flex min-w-0 flex-1 gap-3 text-left">
                      <span className={`flex size-9 shrink-0 items-center justify-center rounded-full text-sm font-bold ${avatarStyle(message.sender || message.subject || key)}`}>{(message.sender || "M").charAt(0).toUpperCase()}</span>
                      <span className="min-w-0 flex-1">
                        <span className="flex items-start justify-between gap-3">
                          <span className="truncate text-sm font-semibold text-slate-900">{message.sender || "未知发件人"}</span>
                          <span className="shrink-0 text-xs text-slate-400">{formatTime(message.received_at)}</span>
                        </span>
                        <span className="mt-1 block truncate text-sm text-slate-700">{message.subject || "无主题"}</span>
                        <span className="mt-1 block truncate text-xs text-slate-500">{message.body_preview || "-"}</span>
                      </span>
                    </button>
                    {message.code ? (
                      <button
                        type="button"
                        onClick={() => void copyVerificationCode(message.code)}
                        className="flex h-12 min-w-24 shrink-0 items-center justify-center gap-1.5 rounded-md border border-emerald-200 bg-emerald-50 px-3 font-mono text-xs font-semibold text-emerald-700 transition-colors hover:border-emerald-300 hover:bg-emerald-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
                        title="点击复制验证码"
                        aria-label={`复制验证码 ${message.code}`}
                        data-testid="mail-code-copy"
                      >
                        {copiedCode === message.code ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
                        {message.code}
                      </button>
                    ) : null}
                  </div>
                );
              })}
            </div>

            <div className="shrink-0 border-t border-slate-200 bg-slate-50/70" data-testid="mail-detail-panel">
              {!selectedMessage ? (
                <div className="flex h-12 items-center gap-2 px-4 text-sm text-slate-500"><Mail className="size-4 text-slate-400" />选择一封邮件查看详情</div>
              ) : (
                <>
                  <div className="flex min-h-14 items-center gap-2 px-4 py-2">
                    <button type="button" onClick={() => setMessageDetailOpen((value) => !value)} className="flex min-w-0 flex-1 items-center gap-3 text-left" aria-expanded={messageDetailOpen}>
                      <span className="flex size-8 shrink-0 items-center justify-center rounded-md bg-white text-slate-500 shadow-sm"><Mail className="size-4" /></span>
                      <span className="min-w-0 flex-1">
                        <span className="block text-[11px] font-semibold text-slate-500">邮件详情</span>
                        <span className="block truncate text-sm font-bold text-slate-900">{selectedMessage.subject || "无主题"}</span>
                      </span>
                    </button>
                    <Badge variant="secondary" className="hidden shrink-0 rounded-md sm:inline-flex">{selectedMessage.provider || mailMode}</Badge>
                    {selectedMessage.code ? (
                      <button
                        type="button"
                        onClick={() => void copyVerificationCode(selectedMessage.code)}
                        className="flex h-9 shrink-0 items-center gap-1.5 rounded-md border border-emerald-200 bg-emerald-50 px-2.5 font-mono text-xs font-semibold text-emerald-700 hover:bg-emerald-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
                        title="点击复制验证码"
                      >
                        {copiedCode === selectedMessage.code ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
                        {selectedMessage.code}
                      </button>
                    ) : null}
                    <button type="button" onClick={() => setMessageDetailOpen((value) => !value)} className="flex size-8 shrink-0 items-center justify-center rounded-md text-slate-500 hover:bg-white hover:text-slate-900" title={messageDetailOpen ? "收起邮件详情" : "展开邮件详情"} aria-label={messageDetailOpen ? "收起邮件详情" : "展开邮件详情"}>
                      {messageDetailOpen ? <ChevronDown className="size-4" /> : <ChevronUp className="size-4" />}
                    </button>
                  </div>
                  {messageDetailOpen ? (
                    <article className="max-h-[240px] overflow-auto overscroll-contain border-t border-slate-200 bg-white px-4 py-4 [scrollbar-gutter:stable]">
                      <div className="mx-auto max-w-4xl">
                        <div className="border-b border-slate-200 pb-4 text-xs">
                          <dl className="grid min-w-0 grid-cols-[52px_minmax(0,1fr)] gap-x-2 gap-y-2">
                            <dt className="font-semibold text-slate-400">发件人</dt>
                            <dd className="break-all font-medium text-slate-800">{selectedMessage.sender || "未知发件人"}</dd>
                            <dt className="font-semibold text-slate-400">收件人</dt>
                            <dd className="break-all text-slate-700">{messageRecipients(selectedMessage, messageEmail || selected?.email)}</dd>
                          </dl>
                          <div className="mt-3 grid min-w-0 grid-cols-[52px_minmax(0,1fr)] gap-x-2 gap-y-2 text-slate-500">
                            <span className="font-semibold text-slate-400">时间</span>
                            <span>{formatTime(selectedMessage.received_at)}</span>
                            <span className="font-semibold text-slate-400">消息 ID</span>
                            <span className="break-all font-mono text-[11px] text-slate-400">{selectedMessage.message_id || "-"}</span>
                          </div>
                        </div>
                        <div className="whitespace-pre-wrap break-words pt-4 text-sm leading-7 text-slate-700">{selectedMessage.body_text || selectedMessage.body_preview || "无正文内容"}</div>
                      </div>
                    </article>
                  ) : null}
                </>
              )}
            </div>
          </div>
        </section>
      </div>
    </section>
  );
}
