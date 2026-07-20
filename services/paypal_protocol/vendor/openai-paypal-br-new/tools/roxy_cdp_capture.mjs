import fs from 'node:fs/promises';
import fssync from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { chromium } from 'file:///home/nonewhite/.npm/_npx/666793a7876f3860/node_modules/js-reverse-mcp/build/src/third_party/index.js';

const args = process.argv.slice(2);
function argValue(name, fallback = undefined) {
  const idx = args.indexOf(name);
  return idx >= 0 ? args[idx + 1] : fallback;
}
const explicitEndpoint = argValue('--endpoint') || argValue('-e') || args.find(a => /^https?:\/\//.test(a));
const root = path.resolve(argValue('--out') || argValue('-o') || path.join(process.cwd(), 'captures', `roxy-paypal-${new Date().toISOString().replace(/[:.]/g, '-')}`));
const pollHtmlMs = Number(argValue('--html-interval-ms', '3000'));

const dirs = {
  root,
  html: path.join(root, 'html'),
  js: path.join(root, 'js'),
  css: path.join(root, 'css'),
  bodies: path.join(root, 'network', 'bodies'),
  requests: path.join(root, 'network', 'requests'),
  ws: path.join(root, 'network', 'websocket'),
  storage: path.join(root, 'storage'),
  screenshots: path.join(root, 'screenshots'),
};
const files = {
  events: path.join(root, 'network', 'events.jsonl'),
  requestsCsv: path.join(root, 'network', 'requests.tsv'),
  websocket: path.join(root, 'network', 'websocket.jsonl'),
  pages: path.join(root, 'pages.jsonl'),
  htmlIndex: path.join(root, 'html', 'snapshots.jsonl'),
  resourceIndex: path.join(root, 'resources.jsonl'),
  console: path.join(root, 'console.jsonl'),
  summary: path.join(root, 'summary.json'),
  meta: path.join(root, 'metadata.json'),
  pid: path.join(root, 'capture.pid'),
};

for (const d of Object.values(dirs)) await fs.mkdir(d, { recursive: true });
await fs.writeFile(files.requestsCsv, 'id\ttime\tmethod\tstatus\tresourceType\turl\trequestBody\tresponseBody\tcontentType\n', { flag: 'a' });

function now() { return new Date().toISOString(); }
function sha256(buf) { return crypto.createHash('sha256').update(buf).digest('hex'); }
function safeName(s, max = 120) {
  return String(s || '')
    .replace(/^https?:\/\//, '')
    .replace(/[^a-zA-Z0-9._-]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, max) || 'item';
}
function mimeToExt(mime = '', url = '') {
  const m = String(mime || '').split(';')[0].trim().toLowerCase();
  if (m.includes('javascript') || m.includes('ecmascript')) return '.js';
  if (m === 'text/html' || m === 'application/xhtml+xml') return '.html';
  if (m === 'text/css') return '.css';
  if (m.includes('json')) return '.json';
  if (m.startsWith('text/')) return '.txt';
  if (m === 'application/xml' || m.endsWith('+xml')) return '.xml';
  if (m === 'image/png') return '.png';
  if (m === 'image/jpeg') return '.jpg';
  if (m === 'image/webp') return '.webp';
  if (m === 'font/woff2') return '.woff2';
  try {
    const ext = path.extname(new URL(url).pathname);
    if (ext && ext.length <= 10) return ext;
  } catch {}
  return '.bin';
}
function isTextMime(mime = '', url = '') {
  const m = String(mime || '').toLowerCase();
  return m.startsWith('text/')
    || m.includes('json')
    || m.includes('javascript')
    || m.includes('ecmascript')
    || m.includes('xml')
    || m.includes('graphql')
    || /\.(js|mjs|json|html|htm|css|txt|xml|graphql)(\?|$)/i.test(url);
}
function chooseBodyDir(resourceType, contentType, url) {
  const ext = mimeToExt(contentType, url);
  if (resourceType === 'script' || ext === '.js' || /\.m?js(\?|$)/i.test(url)) return dirs.js;
  if (ext === '.html') return dirs.html;
  if (ext === '.css') return dirs.css;
  return dirs.bodies;
}
async function appendJsonl(file, obj) {
  await fs.appendFile(file, JSON.stringify(obj) + '\n').catch(() => {});
}
async function appendTsv(fields) {
  const line = fields.map(v => String(v ?? '').replace(/\t/g, ' ').replace(/\r?\n/g, '\\n')).join('\t') + '\n';
  await fs.appendFile(files.requestsCsv, line).catch(() => {});
}
async function writeJson(file, obj) {
  await fs.writeFile(file, JSON.stringify(obj, null, 2));
}
function withTimeout(p, ms, label) {
  return Promise.race([
    p,
    new Promise((_, reject) => setTimeout(() => reject(new Error(`timeout:${label}`)), ms)),
  ]);
}
async function probeEndpoint(endpoint) {
  try {
    const res = await fetch(new URL('/json/version', endpoint));
    if (!res.ok) return null;
    const json = await res.json();
    if (!json.webSocketDebuggerUrl || !String(json.Browser || '').includes('Chrome/')) return null;
    return { endpoint: endpoint.replace(/\/+$/, ''), version: json };
  } catch {
    return null;
  }
}
async function discoverEndpoint() {
  if (explicitEndpoint) {
    const probed = await probeEndpoint(explicitEndpoint);
    if (!probed) throw new Error(`无法连接 CDP endpoint: ${explicitEndpoint}`);
    return probed;
  }
  const out = execFileSync('ss', ['-ltnp'], { encoding: 'utf8' });
  const ports = Array.from(new Set([...out.matchAll(/127\.0\.0\.1:(\d+)/g)].map(m => Number(m[1])))).sort((a, b) => b - a);
  for (const p of ports) {
    const probed = await probeEndpoint(`http://127.0.0.1:${p}`);
    if (probed) return probed;
  }
  throw new Error('没有发现可用的 Roxy Chrome CDP endpoint');
}

const endpointInfo = await discoverEndpoint();
const endpoint = endpointInfo.endpoint;
await writeJson(files.meta, {
  startedAt: now(),
  endpoint,
  browser: endpointInfo.version,
  root,
  pollHtmlMs,
  recorder: 'tools/roxy_cdp_capture.mjs',
  note: 'Attach to an already-open Roxy/Chrome CDP endpoint and continuously record requests, responses, JS, HTML, CSS, WebSocket frames, console, cookies and screenshots.',
});
await fs.writeFile(files.pid, String(process.pid));

console.log(`[capture] endpoint: ${endpoint}`);
console.log(`[capture] output:   ${root}`);

const browser = await chromium.connectOverCDP(endpoint);
const context = browser.contexts()[0];
if (!context) throw new Error('CDP 已连接，但没有找到 browser context');

const requestIds = new WeakMap();
const responseMeta = new Map();
const requestBodyPaths = new Map();
const pageLastHtmlHash = new Map();
const resourceHashes = new Set();
let reqSeq = 0;
let pageSeq = 0;
let wsSeq = 0;
let resourceSeq = 0;
let stopping = false;

async function saveBuffer(kind, url, resourceType, contentType, buf, id = ++resourceSeq) {
  if (!buf || !buf.length) return null;
  const hash = sha256(buf);
  const ext = mimeToExt(contentType, url);
  const outDir = chooseBodyDir(resourceType, contentType, url);
  const out = path.join(outDir, `${String(id).padStart(5, '0')}_${kind}_${safeName(url)}_${hash.slice(0, 10)}${ext}`);
  await fs.writeFile(out, buf);
  const rec = { time: now(), kind, id, url, resourceType, contentType, path: out, bytes: buf.length, sha256: hash, text: isTextMime(contentType, url) };
  await appendJsonl(files.resourceIndex, rec);
  return rec;
}

async function saveHtmlSnapshot(page, reason) {
  if (stopping && reason !== 'shutdown') return;
  try {
    const url = page.url();
    if (!url || url === 'about:blank') return;
    const html = await withTimeout(page.content(), 6000, 'page.content');
    const buf = Buffer.from(html, 'utf8');
    const hash = sha256(buf);
    const pageId = page.__capturePageId || 0;
    const key = `${pageId}:${url}`;
    if (pageLastHtmlHash.get(key) === hash && reason !== 'shutdown') return;
    pageLastHtmlHash.set(key, hash);
    const name = `${Date.now()}_p${pageId}_${safeName(url)}_${hash.slice(0, 10)}.html`;
    const out = path.join(dirs.html, name);
    await fs.writeFile(out, buf);
    await appendJsonl(files.htmlIndex, { time: now(), reason, pageId, url, path: out, bytes: buf.length, sha256: hash });
    console.log(`[html] ${reason} p${pageId} ${url}`);
  } catch (e) {
    await appendJsonl(files.htmlIndex, { time: now(), reason, error: String(e?.message || e) });
  }
}

async function dumpExistingResources(page) {
  try {
    const cdp = await context.newCDPSession(page);
    await cdp.send('Page.enable');
    const tree = await cdp.send('Page.getResourceTree');
    const frames = [];
    function walk(frameTree) {
      if (!frameTree) return;
      frames.push({ frameId: frameTree.frame?.id, resources: frameTree.resources || [] });
      for (const child of frameTree.childFrames || []) walk(child);
    }
    walk(tree.frameTree);
    for (const frame of frames) {
      for (const res of frame.resources) {
        if (!frame.frameId || !res.url) continue;
        if (!['Document', 'Script', 'Stylesheet', 'XHR', 'Fetch'].includes(res.type)) continue;
        try {
          const content = await withTimeout(cdp.send('Page.getResourceContent', { frameId: frame.frameId, url: res.url }), 3500, 'Page.getResourceContent');
          const buf = content.base64Encoded ? Buffer.from(content.content || '', 'base64') : Buffer.from(content.content || '', 'utf8');
          if (!buf.length) continue;
          const hash = sha256(buf);
          const dedupe = `${res.url}:${hash}`;
          if (resourceHashes.has(dedupe)) continue;
          resourceHashes.add(dedupe);
          await saveBuffer(`existing_${res.type.toLowerCase()}`, res.url, res.type.toLowerCase(), res.mimeType || '', buf);
        } catch (e) {
          await appendJsonl(files.resourceIndex, { time: now(), kind: 'existing_error', url: res.url, type: res.type, error: String(e?.message || e) });
        }
      }
    }
    await cdp.detach().catch(() => {});
  } catch (e) {
    await appendJsonl(files.resourceIndex, { time: now(), kind: 'dump_existing_error', pageUrl: page.url(), error: String(e?.message || e) });
  }
}

async function attachPage(page) {
  if (page.__captureAttached) return;
  page.__captureAttached = true;
  page.__capturePageId = ++pageSeq;
  const pageId = page.__capturePageId;
  console.log(`[page] attach p${pageId}: ${page.url()}`);
  await appendJsonl(files.pages, { time: now(), event: 'attach', pageId, url: page.url() });

  page.on('domcontentloaded', () => setTimeout(() => saveHtmlSnapshot(page, 'domcontentloaded'), 300));
  page.on('load', () => setTimeout(() => saveHtmlSnapshot(page, 'load'), 600));
  page.on('framenavigated', frame => {
    if (frame === page.mainFrame()) {
      appendJsonl(files.pages, { time: now(), event: 'framenavigated', pageId, url: page.url() });
      setTimeout(() => saveHtmlSnapshot(page, 'framenavigated'), 700);
      setTimeout(() => dumpExistingResources(page), 1200);
    }
  });
  page.on('console', async msg => {
    await appendJsonl(files.console, { time: now(), pageId, type: msg.type(), text: msg.text(), location: msg.location(), url: page.url() });
  });
  page.on('pageerror', async err => {
    await appendJsonl(files.console, { time: now(), pageId, type: 'pageerror', text: String(err?.stack || err), url: page.url() });
  });

  setTimeout(() => saveHtmlSnapshot(page, 'attach'), 500);
  setTimeout(() => dumpExistingResources(page), 900);
}

context.on('page', attachPage);
for (const p of context.pages()) await attachPage(p);

context.on('request', async request => {
  const id = ++reqSeq;
  requestIds.set(request, id);
  let pageId = null;
  try { pageId = request.frame()?.page()?.__capturePageId || null; } catch {}
  const headers = await request.allHeaders().catch(() => request.headers());
  const rec = {
    id, time: now(), type: 'request', pageId,
    method: request.method(), url: request.url(), resourceType: request.resourceType(),
    isNavigationRequest: request.isNavigationRequest(), headers,
  };
  const postBuffer = request.postDataBuffer?.() || (request.postData?.() ? Buffer.from(request.postData(), 'utf8') : null);
  if (postBuffer?.length) {
    const ct = headers['content-type'] || headers['Content-Type'] || '';
    const ext = isTextMime(ct, request.url()) ? '.txt' : '.bin';
    const bodyPath = path.join(dirs.requests, `${String(id).padStart(5, '0')}_${request.method()}_${safeName(request.url())}${ext}`);
    await fs.writeFile(bodyPath, postBuffer);
    requestBodyPaths.set(id, bodyPath);
    rec.postData = {
      path: bodyPath,
      bytes: postBuffer.length,
      sha256: sha256(postBuffer),
      textPreview: isTextMime(ct, request.url()) ? postBuffer.toString('utf8').slice(0, 4000) : undefined,
    };
  }
  await appendJsonl(files.events, rec);
  console.log(`[req ${id}] ${request.method()} ${request.resourceType()} ${request.url()}`);
});

context.on('response', async response => {
  const request = response.request();
  const id = requestIds.get(request) || ++reqSeq;
  if (!requestIds.has(request)) requestIds.set(request, id);
  let pageId = null;
  try { pageId = request.frame()?.page()?.__capturePageId || null; } catch {}
  const headers = await response.allHeaders().catch(() => response.headers());
  const rec = {
    id, time: now(), type: 'response', pageId,
    method: request.method(), url: response.url(), resourceType: request.resourceType(),
    status: response.status(), statusText: response.statusText(), headers,
  };
  responseMeta.set(id, rec);
  await appendJsonl(files.events, rec);
});

context.on('requestfinished', async request => {
  const id = requestIds.get(request) || ++reqSeq;
  const response = await request.response().catch(() => null);
  const rec = { id, time: now(), type: 'requestfinished', url: request.url(), method: request.method(), resourceType: request.resourceType() };
  let responseBodyPath = '';
  let contentType = '';
  let status = '';
  if (response) {
    status = response.status();
    try {
      const headers = await response.allHeaders().catch(() => response.headers());
      contentType = headers['content-type'] || headers['Content-Type'] || '';
      const body = await withTimeout(response.body(), 15000, 'response.body');
      if (body?.length) {
        const hash = sha256(body);
        const dedupe = `${response.url()}:${hash}`;
        const saved = await saveBuffer(`resp_${response.status()}_${request.resourceType()}`, response.url(), request.resourceType(), contentType, body, id);
        responseBodyPath = saved?.path || '';
        rec.responseBody = saved ? { path: saved.path, bytes: saved.bytes, sha256: saved.sha256, contentType, text: saved.text } : undefined;
        resourceHashes.add(dedupe);
      }
    } catch (e) {
      rec.responseBodyError = String(e?.message || e);
    }
  }
  const requestBody = requestBodyPaths.get(id) || '';
  await appendJsonl(files.events, rec);
  await appendTsv([id, now(), request.method(), status, request.resourceType(), request.url(), requestBody, responseBodyPath, contentType]);
});

context.on('requestfailed', async request => {
  const id = requestIds.get(request) || ++reqSeq;
  await appendJsonl(files.events, { id, time: now(), type: 'requestfailed', url: request.url(), method: request.method(), resourceType: request.resourceType(), failure: request.failure() });
});

context.on('websocket', ws => {
  const wsid = ++wsSeq;
  appendJsonl(files.websocket, { time: now(), wsid, event: 'open', url: ws.url() });
  console.log(`[ws ${wsid}] ${ws.url()}`);
  ws.on('framesent', async payload => {
    await appendJsonl(files.websocket, { time: now(), wsid, event: 'framesent', url: ws.url(), opcode: payload.opcode, data: payload.payload });
  });
  ws.on('framereceived', async payload => {
    await appendJsonl(files.websocket, { time: now(), wsid, event: 'framereceived', url: ws.url(), opcode: payload.opcode, data: payload.payload });
  });
  ws.on('close', async () => {
    await appendJsonl(files.websocket, { time: now(), wsid, event: 'close', url: ws.url() });
  });
});

const htmlInterval = setInterval(async () => {
  for (const p of context.pages()) await saveHtmlSnapshot(p, 'interval');
}, Math.max(1000, pollHtmlMs));

async function finalScreenshot() {
  for (const p of context.pages()) {
    try {
      const pageId = p.__capturePageId || 0;
      const out = path.join(dirs.screenshots, `final_p${pageId}_${Date.now()}.png`);
      await p.screenshot({ path: out, fullPage: true, timeout: 10000 });
    } catch {}
  }
}

async function shutdown(reason = 'manual') {
  if (stopping) return;
  stopping = true;
  clearInterval(htmlInterval);
  console.log(`[capture] stopping (${reason})...`);
  for (const p of context.pages()) await saveHtmlSnapshot(p, 'shutdown');
  await finalScreenshot();
  try { await writeJson(path.join(dirs.storage, 'cookies.json'), await context.cookies()); } catch {}
  try {
    const pages = context.pages().map(p => ({ pageId: p.__capturePageId || 0, url: p.url() }));
    await writeJson(files.summary, { finishedAt: now(), reason, endpoint, root, pages, requests: reqSeq, pagesSeen: pageSeq, websockets: wsSeq });
  } catch {}
  try { await browser.close({ reason: `capture shutdown: ${reason}` }); } catch {}
  console.log(`[capture] saved to ${root}`);
  process.exit(0);
}

process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('uncaughtException', async err => {
  console.error('[capture] uncaught', err);
  await appendJsonl(files.console, { time: now(), type: 'recorder-uncaught', text: String(err?.stack || err) });
});
process.on('unhandledRejection', async err => {
  console.error('[capture] unhandledRejection', err);
  await appendJsonl(files.console, { time: now(), type: 'recorder-unhandledRejection', text: String(err?.stack || err) });
});

console.log('[capture] 已开始持续抓包。请在当前 Roxy 指纹浏览器窗口里手动完成完整支付流程。完成后回到 Codex 告诉我“完成/好了”，我会停止并整理、分析捕获记录。');
await new Promise(() => {});
