"use client";

import { useEffect, useState } from "react";
import { LoaderCircle, RefreshCw, Save, Settings2 } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { fetchMailProviderConfigs, fetchOutlookMailboxPool, updateMailProviderConfigs, type MailProviderConfigs } from "@/lib/api";

import { useSettingsStore } from "../store";

function defaultConfigs(): MailProviderConfigs {
  return {
    registration_source: "outlook_token",
    outlook_token: { mailbox_group: "默认", mode: "auto", imap_host: "outlook.office365.com", message_limit: 20, split_aliases: true, split_count_per_mailbox: 9999 },
    cloudflare_temp_email: { enable: false, api_base: "", admin_password: "", domain: "" },
    gmail_oauth: { enable: true, mode: "imap", imap_host: "imap.gmail.com", message_limit: 20, split_aliases: true, split_count_per_mailbox: 0 },
    ddg_mail: { enable: false, ddg_token: "", api_base: "", cf_inbox_jwt: "", admin_password: "", cf_api_key: "", cf_auth_mode: "none", cf_domain: "", cf_create_path: "/admin/new_address", cf_messages_path: "/api/mails" },
    duckmail: { enable: false, api_key: "", default_domain: "duckmail.sbs" },
    openai_cpa: { enable: false, domains: "", webhook_secret: "", use_original_password_flow: false, suffix_mode: "fixed", suffix_len_min: 8, suffix_len_max: 8, enable_domain_grouping: false, domain_group_count: 2, domain_group_strategy: "round_robin", domain_fail_threshold: 3, domain_fail_cooldown_sec: 600, disabled_domains: "", enable_multi_level_subdomain: false, multi_level_subdomain_levels: 3 },
  };
}

const providerDescriptions: Record<string, string> = {
  outlook_token: "从微软本地库存的指定分组领取 Outlook/Hotmail OAuth 邮箱。",
  gmail_oauth: "配置 Gmail OAuth/IMAP 读取参数；Gmail 凭据仍由运行配置或数据文件提供。",
  cloudflare_temp_email: "配置 Cloudflare 域名邮箱 API、管理密码和可用域名。",
  ddg_mail: "配置 DuckDuckGo 别名生成与固定收件箱读取参数。",
  duckmail: "配置 DuckMail API Key 与默认域名。",
  openai_cpa: "配置 OpenAI-CPA 内存邮箱域名池和 Webhook。",
};

export function MailboxPoolCard() {
  const [configs, setConfigs] = useState<MailProviderConfigs>(defaultConfigs());
  const [providerMode, setProviderMode] = useState("outlook_token");
  const [mailboxGroups, setMailboxGroups] = useState<string[]>(["默认"]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const setRegisterConfig = useSettingsStore((state) => state.setRegisterConfig);

  const load = async () => {
    setLoading(true);
    try {
      const [data, poolData] = await Promise.all([fetchMailProviderConfigs(), fetchOutlookMailboxPool()]);
      const next = { ...defaultConfigs(), ...data.provider_configs };
      setConfigs(next);
      setProviderMode(next.registration_source || "outlook_token");
      setMailboxGroups(Array.from(new Set(["默认", ...(poolData.mailboxes.groups || [])])));
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载邮箱提供商配置失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, []);

  const patchCloudflare = (updates: Partial<MailProviderConfigs["cloudflare_temp_email"]>) => setConfigs((current) => ({ ...current, cloudflare_temp_email: { ...current.cloudflare_temp_email, ...updates } }));
  const patchOutlook = (updates: Partial<MailProviderConfigs["outlook_token"]>) => setConfigs((current) => ({ ...current, outlook_token: { ...current.outlook_token, ...updates } }));
  const patchGmail = (updates: Partial<MailProviderConfigs["gmail_oauth"]>) => setConfigs((current) => ({ ...current, gmail_oauth: { ...current.gmail_oauth, ...updates } }));
  const patchDdg = (updates: Partial<MailProviderConfigs["ddg_mail"]>) => setConfigs((current) => ({ ...current, ddg_mail: { ...current.ddg_mail, ...updates } }));
  const patchDuckmail = (updates: Partial<MailProviderConfigs["duckmail"]>) => setConfigs((current) => ({ ...current, duckmail: { ...current.duckmail, ...updates } }));
  const patchOpenaiCpa = (updates: Partial<MailProviderConfigs["openai_cpa"]>) => setConfigs((current) => ({ ...current, openai_cpa: { ...current.openai_cpa, ...updates } }));
  const selectRegistrationSource = (value: MailProviderConfigs["registration_source"]) => {
    setConfigs((current) => {
      const next = { ...current, registration_source: value };
      if (value === "gmail_oauth") next.gmail_oauth = { ...current.gmail_oauth, enable: true };
      if (value === "cloudflare_temp_email") next.cloudflare_temp_email = { ...current.cloudflare_temp_email, enable: true };
      if (value === "ddg_mail") next.ddg_mail = { ...current.ddg_mail, enable: true };
      if (value === "duckmail") next.duckmail = { ...current.duckmail, enable: true };
      if (value === "openai_cpa") next.openai_cpa = { ...current.openai_cpa, enable: true };
      return next;
    });
    setProviderMode(value);
  };

  const save = async () => {
    setSaving(true);
    try {
      const data = await updateMailProviderConfigs(configs);
      setConfigs({ ...defaultConfigs(), ...data.provider_configs });
      setRegisterConfig(data.register);
      toast.success("邮箱提供商配置已保存");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存邮箱提供商配置失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card className="overflow-hidden">
      <CardHeader className="gap-3 border-b border-slate-200 p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <CardTitle className="flex items-center gap-2 text-xl text-slate-950"><Settings2 className="size-5 text-emerald-600" />邮箱配置</CardTitle>
            <CardDescription className="mt-1 text-slate-500">选择注册邮箱来源并维护对应参数；本地微软账号和分组在“微软邮箱库存”中管理。</CardDescription>
          </div>
          <Button variant="outline" className="h-9 rounded-xl" onClick={() => void load()} disabled={loading || saving}>
            {loading ? <LoaderCircle className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}刷新配置
          </Button>
        </div>
      </CardHeader>
      <CardContent className="p-4">
        <div className="mb-4 grid gap-2 border border-emerald-200 bg-emerald-50 p-3 md:grid-cols-[220px_minmax(0,1fr)] md:items-center">
          <div><div className="text-sm font-semibold text-emerald-950">注册邮箱来源</div><div className="mt-1 text-xs text-emerald-700">所有注册模式统一从这里选择的邮箱模式获取邮箱。</div></div>
          <select value={configs.registration_source} onChange={(event) => selectRegistrationSource(event.target.value as MailProviderConfigs["registration_source"])} className="h-11 w-full border border-emerald-300 bg-white px-3 text-sm font-semibold text-emerald-950">
            <option value="outlook_token">本地微软邮箱库模式</option>
            <option value="gmail_oauth">Gmail OAuth 模式</option>
            <option value="cloudflare_temp_email">Cloudflare 域名邮箱模式</option>
            <option value="ddg_mail">DuckGo 转发邮箱模式</option>
            <option value="duckmail">DuckMail 模式</option>
            <option value="openai_cpa">OpenAI-CPA 内存池模式</option>
          </select>
        </div>
        <div className="grid gap-4 xl:grid-cols-[260px_minmax(0,1fr)]">
          <div className="space-y-2">
            {[
              ["outlook_token", "本地微软邮箱库"],
              ["gmail_oauth", "Gmail OAuth"],
              ["cloudflare_temp_email", "Cloudflare 域名邮箱"],
              ["ddg_mail", "DuckGo 转发邮箱"],
              ["duckmail", "DuckMail 鸭子邮箱"],
              ["openai_cpa", "OpenAI-CPA 内存池"],
            ].map(([key, label]) => (
              <button key={key} type="button" onClick={() => setProviderMode(key)} className={`w-full border px-3 py-3 text-left text-sm transition ${providerMode === key ? "border-emerald-300 bg-emerald-50 text-emerald-950" : "border-slate-200 bg-white text-slate-700 hover:bg-slate-50"}`}>
                <div className="flex items-center justify-between gap-2"><span className="font-semibold">{label}</span>{configs.registration_source === key ? <span className="text-[11px] font-semibold text-emerald-700">当前来源</span> : null}</div>
                <div className="mt-1 text-xs leading-5 text-slate-500">{providerDescriptions[key]}</div>
              </button>
            ))}
          </div>

          <div className="min-w-0 border-l border-slate-200 pl-0 xl:pl-5">
            {loading ? (
              <div className="flex min-h-72 items-center justify-center text-sm text-slate-500"><LoaderCircle className="mr-2 size-5 animate-spin" />正在加载配置</div>
            ) : null}

            {!loading && providerMode === "gmail_oauth" ? (
              <div className="space-y-4">
                <label className="flex items-center gap-2 text-sm font-semibold text-slate-900"><Checkbox checked={configs.gmail_oauth.enable} onCheckedChange={(checked) => patchGmail({ enable: Boolean(checked) })} />启用 Gmail OAuth</label>
                <div className="grid gap-3 md:grid-cols-3">
                  <label className="space-y-1.5 text-xs font-medium text-slate-600">读取方式<select value={configs.gmail_oauth.mode} onChange={(event) => patchGmail({ mode: event.target.value })} className="h-10 w-full border border-slate-200 bg-white px-3 text-sm"><option value="imap">IMAP OAuth2</option></select></label>
                  <label className="space-y-1.5 text-xs font-medium text-slate-600">IMAP Host<Input value={configs.gmail_oauth.imap_host} onChange={(event) => patchGmail({ imap_host: event.target.value })} className="h-10" /></label>
                  <label className="space-y-1.5 text-xs font-medium text-slate-600">读取邮件数<Input type="number" min={1} max={50} value={String(configs.gmail_oauth.message_limit)} onChange={(event) => patchGmail({ message_limit: Number(event.target.value) || 20 })} className="h-10" /></label>
                </div>
                <div className="grid gap-3 md:grid-cols-2">
                  <label className="flex items-center gap-2 text-sm text-slate-700"><Checkbox checked={configs.gmail_oauth.split_aliases} onCheckedChange={(checked) => patchGmail({ split_aliases: Boolean(checked) })} />启用 Gmail plus alias 裂变</label>
                  <label className="space-y-1.5 text-xs font-medium text-slate-600">每个邮箱裂变数<Input type="number" min={0} value={String(configs.gmail_oauth.split_count_per_mailbox)} onChange={(event) => patchGmail({ split_count_per_mailbox: Number(event.target.value) || 0 })} className="h-10" /></label>
                </div>
              </div>
            ) : null}

            {!loading && providerMode === "outlook_token" ? (
              <div className="space-y-4">
                <div className="border border-sky-200 bg-sky-50 p-3 text-sm text-sky-900">注册时只会从所选分组领取未使用微软邮箱。分组的新建、重命名和删除请到“微软邮箱库存”。</div>
                <div className="grid gap-3 md:grid-cols-2">
                  <label className="space-y-1.5 text-xs font-medium text-slate-600">邮箱分组<select value={configs.outlook_token.mailbox_group || "默认"} onChange={(event) => patchOutlook({ mailbox_group: event.target.value })} className="h-10 w-full border border-slate-200 bg-white px-3 text-sm">{mailboxGroups.map((group) => <option key={group} value={group}>{group}</option>)}</select></label>
                  <label className="space-y-1.5 text-xs font-medium text-slate-600">读取方式<select value={configs.outlook_token.mode || "auto"} onChange={(event) => patchOutlook({ mode: event.target.value })} className="h-10 w-full border border-slate-200 bg-white px-3 text-sm"><option value="auto">自动 Graph → IMAP</option><option value="graph">仅 Graph</option><option value="imap">仅 IMAP</option></select></label>
                  <label className="space-y-1.5 text-xs font-medium text-slate-600">IMAP Host<Input value={configs.outlook_token.imap_host || "outlook.office365.com"} onChange={(event) => patchOutlook({ imap_host: event.target.value })} className="h-10" /></label>
                  <label className="space-y-1.5 text-xs font-medium text-slate-600">读取邮件数<Input type="number" min={1} max={50} value={String(configs.outlook_token.message_limit || 20)} onChange={(event) => patchOutlook({ message_limit: Number(event.target.value) || 20 })} className="h-10" /></label>
                  <label className="flex items-center gap-2 text-sm text-slate-700"><Checkbox checked={Boolean(configs.outlook_token.split_aliases)} onCheckedChange={(checked) => patchOutlook({ split_aliases: Boolean(checked) })} />启用 Outlook plus alias 裂变</label>
                  <label className="space-y-1.5 text-xs font-medium text-slate-600">每个邮箱裂变数<Input type="number" min={0} value={String(configs.outlook_token.split_count_per_mailbox || 0)} onChange={(event) => patchOutlook({ split_count_per_mailbox: Number(event.target.value) || 0 })} className="h-10" /></label>
                </div>
              </div>
            ) : null}

            {!loading && providerMode === "cloudflare_temp_email" ? (
              <div className="space-y-4">
                <label className="flex items-center gap-2 text-sm font-semibold text-slate-900"><Checkbox checked={configs.cloudflare_temp_email.enable} onCheckedChange={(checked) => patchCloudflare({ enable: Boolean(checked) })} />启用 Cloudflare 域名邮箱</label>
                <div className="grid gap-3 md:grid-cols-2">
                  <label className="space-y-1.5 text-xs font-medium text-slate-600">API Base<Input value={configs.cloudflare_temp_email.api_base} onChange={(event) => patchCloudflare({ api_base: event.target.value })} placeholder="https://mail.example.com" className="h-10" /></label>
                  <label className="space-y-1.5 text-xs font-medium text-slate-600">域名<Input value={configs.cloudflare_temp_email.domain || ""} onChange={(event) => patchCloudflare({ domain: event.target.value })} placeholder="example.com" className="h-10" /></label>
                </div>
                <label className="block space-y-1.5 text-xs font-medium text-slate-600">管理员密码<Input value={configs.cloudflare_temp_email.admin_password || ""} onChange={(event) => patchCloudflare({ admin_password: event.target.value })} placeholder={configs.cloudflare_temp_email.has_admin_password ? String(configs.cloudflare_temp_email.admin_password_preview) : "admin password"} type="password" className="h-10" /></label>
              </div>
            ) : null}

            {!loading && providerMode === "ddg_mail" ? (
              <div className="space-y-4">
                <label className="flex items-center gap-2 text-sm font-semibold text-slate-900"><Checkbox checked={configs.ddg_mail.enable} onCheckedChange={(checked) => patchDdg({ enable: Boolean(checked) })} />启用 DuckGo 转发邮箱</label>
                <div className="grid gap-3 md:grid-cols-2">
                  <label className="space-y-1.5 text-xs font-medium text-slate-600">DuckGo Token<Input value={configs.ddg_mail.ddg_token || ""} onChange={(event) => patchDdg({ ddg_token: event.target.value })} placeholder={configs.ddg_mail.has_ddg_token ? configs.ddg_mail.ddg_token_preview : "Bearer Token"} type="password" className="h-10" /></label>
                  <label className="space-y-1.5 text-xs font-medium text-slate-600">收件箱 API Base<Input value={configs.ddg_mail.api_base} onChange={(event) => patchDdg({ api_base: event.target.value })} className="h-10" /></label>
                  <label className="space-y-1.5 text-xs font-medium text-slate-600">收件箱 JWT<Input value={configs.ddg_mail.cf_inbox_jwt || ""} onChange={(event) => patchDdg({ cf_inbox_jwt: event.target.value })} placeholder={configs.ddg_mail.has_cf_inbox_jwt ? configs.ddg_mail.cf_inbox_jwt_preview : "JWT"} type="password" className="h-10" /></label>
                  <label className="space-y-1.5 text-xs font-medium text-slate-600">CF 域名池<Input value={configs.ddg_mail.cf_domain || ""} onChange={(event) => patchDdg({ cf_domain: event.target.value })} placeholder="a.com,b.com" className="h-10" /></label>
                  <label className="space-y-1.5 text-xs font-medium text-slate-600">管理员密码<Input value={configs.ddg_mail.admin_password || ""} onChange={(event) => patchDdg({ admin_password: event.target.value })} type="password" className="h-10" /></label>
                  <label className="space-y-1.5 text-xs font-medium text-slate-600">CF API Key<Input value={configs.ddg_mail.cf_api_key || ""} onChange={(event) => patchDdg({ cf_api_key: event.target.value })} type="password" className="h-10" /></label>
                  <label className="space-y-1.5 text-xs font-medium text-slate-600">创建路径<Input value={configs.ddg_mail.cf_create_path || ""} onChange={(event) => patchDdg({ cf_create_path: event.target.value })} className="h-10" /></label>
                  <label className="space-y-1.5 text-xs font-medium text-slate-600">邮件路径<Input value={configs.ddg_mail.cf_messages_path || ""} onChange={(event) => patchDdg({ cf_messages_path: event.target.value })} className="h-10" /></label>
                </div>
              </div>
            ) : null}

            {!loading && providerMode === "duckmail" ? (
              <div className="space-y-4">
                <label className="flex items-center gap-2 text-sm font-semibold text-slate-900"><Checkbox checked={configs.duckmail.enable} onCheckedChange={(checked) => patchDuckmail({ enable: Boolean(checked) })} />启用 DuckMail</label>
                <div className="grid gap-3 md:grid-cols-2">
                  <label className="space-y-1.5 text-xs font-medium text-slate-600">API Key<Input value={configs.duckmail.api_key || ""} onChange={(event) => patchDuckmail({ api_key: event.target.value })} placeholder={configs.duckmail.has_api_key ? configs.duckmail.api_key_preview : "API Key"} type="password" className="h-10" /></label>
                  <label className="space-y-1.5 text-xs font-medium text-slate-600">默认域名<Input value={configs.duckmail.default_domain || "duckmail.sbs"} onChange={(event) => patchDuckmail({ default_domain: event.target.value })} className="h-10" /></label>
                </div>
              </div>
            ) : null}

            {!loading && providerMode === "openai_cpa" ? (
              <div className="space-y-4">
                <label className="flex items-center gap-2 text-sm font-semibold text-slate-900"><Checkbox checked={configs.openai_cpa.enable} onCheckedChange={(checked) => patchOpenaiCpa({ enable: Boolean(checked) })} />启用 OpenAI-CPA 内存池</label>
                <div className="grid gap-3 md:grid-cols-2">
                  <label className="space-y-1.5 text-xs font-medium text-slate-600">域名池<Input value={configs.openai_cpa.domains || ""} onChange={(event) => patchOpenaiCpa({ domains: event.target.value })} placeholder="example.com,example.net" className="h-10" /></label>
                  <label className="space-y-1.5 text-xs font-medium text-slate-600">禁用域名<Input value={configs.openai_cpa.disabled_domains || ""} onChange={(event) => patchOpenaiCpa({ disabled_domains: event.target.value })} className="h-10" /></label>
                  <label className="space-y-1.5 text-xs font-medium text-slate-600">Webhook Secret<Input value={configs.openai_cpa.webhook_secret || ""} onChange={(event) => patchOpenaiCpa({ webhook_secret: event.target.value })} placeholder={configs.openai_cpa.has_webhook_secret ? configs.openai_cpa.webhook_secret_preview : "EMAIL_WEBHOOK_SECRET"} type="password" className="h-10" /></label>
                  <label className="space-y-1.5 text-xs font-medium text-slate-600">分组策略<select value={configs.openai_cpa.domain_group_strategy || "round_robin"} onChange={(event) => patchOpenaiCpa({ domain_group_strategy: event.target.value })} className="h-10 w-full border border-slate-200 bg-white px-3 text-sm"><option value="round_robin">轮询分组</option><option value="exhaust_then_next">用完再换</option></select></label>
                  <label className="space-y-1.5 text-xs font-medium text-slate-600">后缀最小长度<Input type="number" value={String(configs.openai_cpa.suffix_len_min || 8)} onChange={(event) => patchOpenaiCpa({ suffix_len_min: Number(event.target.value) || 8 })} className="h-10" /></label>
                  <label className="space-y-1.5 text-xs font-medium text-slate-600">后缀最大长度<Input type="number" value={String(configs.openai_cpa.suffix_len_max || 8)} onChange={(event) => patchOpenaiCpa({ suffix_len_max: Number(event.target.value) || 8 })} className="h-10" /></label>
                  <label className="space-y-1.5 text-xs font-medium text-slate-600">域名失败阈值<Input type="number" value={String(configs.openai_cpa.domain_fail_threshold || 3)} onChange={(event) => patchOpenaiCpa({ domain_fail_threshold: Number(event.target.value) || 3 })} className="h-10" /></label>
                  <label className="space-y-1.5 text-xs font-medium text-slate-600">域名冷却秒数<Input type="number" value={String(configs.openai_cpa.domain_fail_cooldown_sec || 600)} onChange={(event) => patchOpenaiCpa({ domain_fail_cooldown_sec: Number(event.target.value) || 600 })} className="h-10" /></label>
                </div>
                <div className="grid gap-3 md:grid-cols-3">
                  <label className="flex items-center gap-2 text-sm text-slate-700"><Checkbox checked={Boolean(configs.openai_cpa.enable_domain_grouping)} onCheckedChange={(checked) => patchOpenaiCpa({ enable_domain_grouping: Boolean(checked) })} />启用域名分组</label>
                  <label className="flex items-center gap-2 text-sm text-slate-700"><Checkbox checked={Boolean(configs.openai_cpa.enable_multi_level_subdomain)} onCheckedChange={(checked) => patchOpenaiCpa({ enable_multi_level_subdomain: Boolean(checked) })} />启用多级域名</label>
                  <label className="flex items-center gap-2 text-sm text-slate-700"><Checkbox checked={Boolean(configs.openai_cpa.use_original_password_flow)} onCheckedChange={(checked) => patchOpenaiCpa({ use_original_password_flow: Boolean(checked) })} />兼容原始密码流程</label>
                </div>
              </div>
            ) : null}

            {!loading ? <div className="mt-6 flex justify-end border-t border-slate-200 pt-4"><Button className="h-10 rounded-xl bg-emerald-600 px-5 text-white" onClick={() => void save()} disabled={saving}>{saving ? <LoaderCircle className="size-4 animate-spin" /> : <Save className="size-4" />}保存邮箱配置</Button></div> : null}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
