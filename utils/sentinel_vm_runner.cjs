"use strict";

const crypto = require("crypto");
const vm = require("vm");

function readStdin() {
  return new Promise((resolve, reject) => {
    const chunks = [];
    process.stdin.on("data", (chunk) => chunks.push(chunk));
    process.stdin.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    process.stdin.on("error", reject);
  });
}

function makeSandbox(input) {
  const sdkUrl = String(input.sdkUrl || "https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js");
  const location = new URL(input.pageUrl || "https://auth.openai.com/about-you");
  const script = {
    src: sdkUrl,
    getAttribute(name) {
      return name === "data-build" ? "" : null;
    },
  };
  const navigatorPrototype = {
    userAgent: String(input.userAgent || "Mozilla/5.0"),
    language: "en-US",
    languages: ["en-US", "en"],
    platform: "Win32",
    vendor: "",
    hardwareConcurrency: 8,
    maxTouchPoints: 0,
    cookieEnabled: true,
    onLine: true,
    pdfViewerEnabled: true,
  };
  const navigator = Object.create(navigatorPrototype);
  Object.assign(navigator, navigatorPrototype);
  const startedAt = Date.now();
  const document = {
    cookie: "",
    scripts: [script],
    currentScript: script,
    documentElement: { getAttribute: () => "" },
    body: { appendChild: () => undefined },
    createElement: () => ({ style: {}, addEventListener: () => undefined, contentWindow: null }),
    addEventListener: () => undefined,
  };
  const sandbox = {
    Array,
    Boolean,
    Buffer,
    Date,
    Error,
    JSON,
    Map,
    Math,
    Number,
    Object,
    Promise,
    Set,
    String,
    TextDecoder,
    TextEncoder,
    URL,
    URLSearchParams,
    Uint8Array,
    WeakMap,
    atob: (value) => Buffer.from(String(value), "base64").toString("binary"),
    btoa: (value) => Buffer.from(String(value), "binary").toString("base64"),
    clearInterval,
    clearTimeout,
    console: { debug() {}, error() {}, info() {}, log() {}, warn() {} },
    crypto: crypto.webcrypto,
    document,
    encodeURIComponent,
    decodeURIComponent,
    escape,
    fetch: async () => { throw new Error("network disabled inside sentinel VM"); },
    location,
    navigator,
    performance: {
      memory: { jsHeapSizeLimit: 4294705152, totalJSHeapSize: 35_000_000, usedJSHeapSize: 25_000_000 },
      now: () => Date.now() - startedAt,
      timeOrigin: startedAt,
    },
    queueMicrotask,
    requestIdleCallback: (callback) => setTimeout(() => callback({ didTimeout: false, timeRemaining: () => 10 }), 0),
    screen: { height: 1080, width: 1920 },
    setInterval,
    setTimeout,
    unescape,
  };
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.top = sandbox;
  sandbox.parent = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.addEventListener = () => undefined;
  sandbox.postMessage = () => undefined;
  return sandbox;
}

async function main() {
  const input = JSON.parse(await readStdin());
  const marker = "t.token=ye,t}({});";
  const replacement = "t.__turnstile=_n,t.__sessionObserver=Nt,t.__setProof=D,t.token=ye,t}({});";
  const sdkSource = String(input.sdkSource || "");
  if (!sdkSource.includes(marker)) throw new Error("sentinel SDK hook marker not found");
  const sandbox = makeSandbox(input);
  vm.createContext(sandbox);
  vm.runInContext(sdkSource.replace(marker, replacement), sandbox, { timeout: 10_000 });
  const sdk = sandbox.SentinelSDK;
  if (!sdk || typeof sdk.__setProof !== "function") throw new Error("sentinel SDK internals unavailable");
  const chatReq = input.chatReq || {};
  sdk.__setProof(chatReq, String(input.cachedProof || ""));
  const turnstile = chatReq.turnstile || {};
  const observer = chatReq.so || {};
  const t = turnstile.dx && typeof sdk.__turnstile === "function"
    ? await sdk.__turnstile(chatReq, turnstile.dx)
    : null;
  const so = observer.collector_dx && typeof sdk.__sessionObserver === "function"
    ? await sdk.__sessionObserver(observer.collector_dx)
    : null;
  process.stdout.write(JSON.stringify({ t: t || null, so: so || null }));
}

main().catch((error) => {
  process.stderr.write(String(error && error.stack || error));
  process.exitCode = 1;
});
