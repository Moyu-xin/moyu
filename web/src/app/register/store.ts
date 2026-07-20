"use client";

import { create } from "zustand";
import { toast } from "sonner";

import {
  clearRegisterLogs as clearRegisterLogsApi,
  fetchRegisterConfig,
  resetOutlookTokenPool,
  resetRegister as resetRegisterApi,
  startRegister,
  stopRegister,
  updateRegisterConfig,
  type GrokRegisterConfig,
  type RegisterConfig,
} from "@/lib/api";

type RegisterStore = {
  registerConfig: RegisterConfig | null;
  isLoadingRegister: boolean;
  isSavingRegister: boolean;
  loadRegister: (silent?: boolean) => Promise<void>;
  setRegisterConfig: (config: RegisterConfig) => void;
  setRegisterTotal: (value: string) => void;
  setRegisterThreads: (value: string) => void;
  setRegisterMode: (value: "total" | "quota" | "available") => void;
  setRegisterFlowMode: (value: "free" | "k12" | "grok") => void;
  setRegisterProductMode: (value: "partial" | "finished") => void;
  setGrokConfig: (updates: Partial<GrokRegisterConfig>) => void;
  setRegisterTargetQuota: (value: string) => void;
  setRegisterTargetAvailable: (value: string) => void;
  setRegisterCheckInterval: (value: string) => void;
  saveRegister: () => Promise<void>;
  toggleRegister: () => Promise<void>;
  clearRegisterLogs: () => Promise<void>;
  resetRegister: () => Promise<void>;
  resetOutlookPool: (scope?: "all" | "failed" | "unused") => Promise<void>;
};

function patchConfig(config: RegisterConfig, patch: Partial<RegisterConfig>): RegisterConfig {
  return { ...config, ...patch };
}

export const useSettingsStore = create<RegisterStore>((set, get) => ({
  registerConfig: null,
  isLoadingRegister: true,
  isSavingRegister: false,

  loadRegister: async (silent = false) => {
    if (!silent) set({ isLoadingRegister: true });
    try {
      const data = await fetchRegisterConfig();
      set({ registerConfig: data.register });
    } catch (error) {
      if (!silent) toast.error(error instanceof Error ? error.message : "加载注册配置失败");
    } finally {
      if (!silent) set({ isLoadingRegister: false });
    }
  },

  setRegisterConfig: (config) => set({ registerConfig: config, isLoadingRegister: false }),

  setRegisterTotal: (value) => set((state) => state.registerConfig ? { registerConfig: patchConfig(state.registerConfig, { total: Math.max(1, Number(value) || 1) }) } : {}),
  setRegisterThreads: (value) => set((state) => state.registerConfig ? { registerConfig: patchConfig(state.registerConfig, { threads: Math.max(1, Math.min(64, Number(value) || 1)) }) } : {}),
  setRegisterMode: (value) => set((state) => state.registerConfig ? { registerConfig: patchConfig(state.registerConfig, { mode: value }) } : {}),
  setRegisterFlowMode: (value) => {
    const k12Mode = value === "k12";
    set((state) => state.registerConfig ? { registerConfig: patchConfig(state.registerConfig, { register_mode: value, k12_mode: k12Mode, ...(value === "grok" ? { mode: "total", product_mode: "finished" } : {}) }) } : {});
    void (async () => {
      const config = get().registerConfig;
      if (!config || config.enabled) return;
      try {
        const data = await updateRegisterConfig({ register_mode: value, k12_mode: k12Mode });
        const current = get().registerConfig;
        if ((current?.register_mode || (current?.k12_mode ? "k12" : "free")) === value) {
          set({ registerConfig: data.register });
        }
      } catch (error) {
        toast.error(error instanceof Error ? error.message : "切换注册模式失败");
      }
    })();
  },
  setGrokConfig: (updates) => set((state) => {
    if (!state.registerConfig) return {};
    const current = state.registerConfig.grok || {
      captcha_provider: "local",
      local_solver_url: "http://127.0.0.1:5072",
      solver_auto_start: true,
      solver_threads: 1,
      captcha_timeout: 120,
      email_timeout: 120,
      request_timeout: 30,
      oauth_timeout: 45,
      exchange_oauth: true,
      delivery_target: "warehouses",
      auto_import_sub2api: false,
      auto_import_cpa: false,
      auto_import_grok2api: false,
    };
    return { registerConfig: patchConfig(state.registerConfig, { grok: { ...current, ...updates } }) };
  }),
  setRegisterProductMode: (value) => {
    set((state) => state.registerConfig ? { registerConfig: patchConfig(state.registerConfig, { product_mode: value }) } : {});
    void (async () => {
      const config = get().registerConfig;
      if (!config || config.enabled) return;
      try {
        const data = await updateRegisterConfig({ product_mode: value });
        if (get().registerConfig?.product_mode === value) set({ registerConfig: data.register });
      } catch (error) {
        toast.error(error instanceof Error ? error.message : "切换产物模式失败");
      }
    })();
  },
  setRegisterTargetQuota: (value) => set((state) => state.registerConfig ? { registerConfig: patchConfig(state.registerConfig, { target_quota: Math.max(1, Number(value) || 1) }) } : {}),
  setRegisterTargetAvailable: (value) => set((state) => state.registerConfig ? { registerConfig: patchConfig(state.registerConfig, { target_available: Math.max(1, Number(value) || 1) }) } : {}),
  setRegisterCheckInterval: (value) => set((state) => state.registerConfig ? { registerConfig: patchConfig(state.registerConfig, { check_interval: Math.max(1, Number(value) || 5) }) } : {}),
  saveRegister: async () => {
    const config = get().registerConfig;
    if (!config) return;
    set({ isSavingRegister: true });
    try {
      const data = await updateRegisterConfig({
        total: Math.max(1, Number(config.total) || 1),
        threads: Math.max(1, Math.min(64, Number(config.threads) || 1)),
        mode: config.mode || "total",
        register_mode: config.register_mode || (config.k12_mode ? "k12" : "free"),
        product_mode: config.product_mode || "partial",
        target_quota: Math.max(1, Number(config.target_quota) || 1),
        target_available: Math.max(1, Number(config.target_available) || 1),
        check_interval: Math.max(1, Number(config.check_interval) || 5),
        k12_mode: Boolean(config.k12_mode),
        grok: config.grok,
      });
      set({ registerConfig: data.register });
      toast.success("注册配置已保存");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存注册配置失败");
    } finally {
      set({ isSavingRegister: false });
    }
  },

  toggleRegister: async () => {
    const config = get().registerConfig;
    if (!config) return;
    set({ isSavingRegister: true });
    try {
      if (!config.enabled) {
        await updateRegisterConfig({
          total: Math.max(1, Number(config.total) || 1),
          threads: Math.max(1, Math.min(64, Number(config.threads) || 1)),
          mode: config.mode || "total",
          register_mode: config.register_mode || (config.k12_mode ? "k12" : "free"),
          product_mode: config.product_mode || "partial",
          target_quota: Math.max(1, Number(config.target_quota) || 1),
          target_available: Math.max(1, Number(config.target_available) || 1),
          check_interval: Math.max(1, Number(config.check_interval) || 5),
          k12_mode: Boolean(config.k12_mode),
          grok: config.grok,
        });
      }
      const data = config.enabled ? await stopRegister() : await startRegister();
      set({ registerConfig: data.register });
      toast.success(config.enabled ? "注册任务已停止" : "注册任务已启动");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "切换注册状态失败");
    } finally {
      set({ isSavingRegister: false });
    }
  },

  clearRegisterLogs: async () => {
    set({ isSavingRegister: true });
    try {
      const data = await clearRegisterLogsApi();
      set({ registerConfig: data.register });
      toast.success("运行日志已清空");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "清空运行日志失败");
    } finally {
      set({ isSavingRegister: false });
    }
  },

  resetRegister: async () => {
    set({ isSavingRegister: true });
    try {
      const data = await resetRegisterApi();
      set({ registerConfig: data.register });
      toast.success("注册统计已重置");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "重置注册统计失败");
    } finally {
      set({ isSavingRegister: false });
    }
  },

  resetOutlookPool: async (scope = "all") => {
    set({ isSavingRegister: true });
    try {
      const data = await resetOutlookTokenPool(scope);
      set({ registerConfig: data.register });
      toast.success("Outlook 邮箱池状态已更新");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "更新 Outlook 邮箱池失败");
    } finally {
      set({ isSavingRegister: false });
    }
  },
}));
