import axios, { AxiosError, type AxiosRequestConfig } from "axios";

import webConfig from "@/constants/common-env";
import { clearStoredAuthSession, getStoredAuthKey } from "@/store/auth";

type RequestConfig = AxiosRequestConfig & {
  redirectOnUnauthorized?: boolean;
};

type ErrorPayload = {
  detail?: string | { error?: string | { message?: string } };
  error?: string | { message?: string };
  message?: string;
};

function errorMessageFromValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (!value || typeof value !== "object") return "";
  const item = value as { error?: unknown; message?: unknown };
  if (typeof item.message === "string") return item.message;
  return errorMessageFromValue(item.error);
}

export const request = axios.create({
  baseURL: webConfig.apiUrl.replace(/\/$/, ""),
});

request.interceptors.request.use(async (config) => {
  const nextConfig = { ...config };
  const authKey = await getStoredAuthKey();
  const headers = { ...(nextConfig.headers || {}) } as Record<string, string>;
  if (authKey && !headers.Authorization) {
    headers.Authorization = `Bearer ${authKey}`;
  }
  // eslint-disable-next-line @typescript-eslint/ban-ts-comment
  // @ts-expect-error axios header type is intentionally narrowed here
  nextConfig.headers = headers;
  return nextConfig;
});

request.interceptors.response.use(
  (response) => response,
  async (error: AxiosError<ErrorPayload>) => {
    const status = error.response?.status;
    const shouldRedirect = (error.config as RequestConfig | undefined)?.redirectOnUnauthorized !== false;
    if (status === 401 && shouldRedirect && typeof window !== "undefined") {
      if (!window.location.pathname.startsWith("/login")) {
        await clearStoredAuthSession();
        window.location.replace("/login");
        return new Promise(() => {});
      }
    }

    const payload = error.response?.data;
    const message =
      errorMessageFromValue(payload?.detail) ||
      errorMessageFromValue(payload?.error) ||
      payload?.message ||
      error.message ||
      `请求失败 (${status || 500})`;
    return Promise.reject(new Error(message));
  },
);

type RequestOptions = {
  method?: string;
  body?: unknown;
  headers?: Record<string, string>;
  redirectOnUnauthorized?: boolean;
};

type CachedRequestOptions = {
  maxAgeMs?: number;
  force?: boolean;
};

type CacheEntry = {
  expiresAt: number;
  value?: unknown;
  pending?: Promise<unknown>;
};

const responseCache = new Map<string, CacheEntry>();
let cacheGeneration = 0;

export function invalidateRequestCache(pathPrefix = "") {
  cacheGeneration += 1;
  for (const key of responseCache.keys()) {
    const path = key.slice(key.indexOf("\n") + 1);
    if (!pathPrefix || path.startsWith(pathPrefix)) responseCache.delete(key);
  }
}

export async function httpRequest<T>(path: string, options: RequestOptions = {}) {
  const { method = "GET", body, headers, redirectOnUnauthorized = true } = options;
  const config: RequestConfig = {
    url: path,
    method,
    data: body,
    headers,
    redirectOnUnauthorized,
  };
  const response = await request.request<T>(config);
  if (!["GET", "HEAD"].includes(String(method).toUpperCase())) invalidateRequestCache();
  return response.data;
}

export async function cachedHttpRequest<T>(path: string, options: CachedRequestOptions = {}) {
  const { maxAgeMs = 10_000, force = false } = options;
  const authKey = await getStoredAuthKey();
  const cacheKey = `${authKey || ""}\n${path}`;
  const current = responseCache.get(cacheKey);
  if (current?.pending) return current.pending as Promise<T>;
  if (!force && current?.value !== undefined && current.expiresAt > Date.now()) return current.value as T;

  const generation = cacheGeneration;
  const pending = httpRequest<T>(path);
  responseCache.set(cacheKey, { expiresAt: 0, pending });
  try {
    const value = await pending;
    if (generation === cacheGeneration) {
      responseCache.set(cacheKey, { expiresAt: Date.now() + Math.max(0, maxAgeMs), value });
    }
    return value;
  } catch (error) {
    if (generation === cacheGeneration) responseCache.delete(cacheKey);
    throw error;
  }
}
