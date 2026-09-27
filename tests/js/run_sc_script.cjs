// Прогоняет soundcloud_playlists.js в Node с поддельными soundcloud.com и api-v2 и печатает JSON.
// Использование: node run_sc_script.cjs <script.js> <scenario> <runs>
const fs = require("fs");
const vm = require("vm");

const [scriptPath, scenario = "ok", runsArg = "1"] = process.argv.slice(2);
const code = fs.readFileSync(scriptPath, "utf8").replace(/^﻿/, "");
const API = "https://api-v2.soundcloud.com";
const server = { playlists: [], nextId: 1000 };
if (scenario === "existing_mark") {
  server.playlists.push({ id: 555, title: "Из VK", description: "Перенесено из VK (vk2sc)", tracks: [{ id: 9 }], track_count: 1 });
  server.playlists.push({ id: 556, title: "Из VK (2)", description: "личный", tracks: [], track_count: 0 });
}
const requests = [];
const logs = [];
const storage = {};

function reply(status, obj, raw) {
  const text = raw !== undefined ? raw : obj === undefined ? "" : JSON.stringify(obj);
  return { status, ok: status >= 200 && status < 300, text: async () => text };
}

async function fakeFetch(url, opts = {}) {
  const u = new URL(url);
  const body = opts.body ? JSON.parse(opts.body) : null;
  requests.push({ method: opts.method, path: u.pathname, clientId: u.searchParams.get("client_id"),
                  headers: opts.headers, credentials: opts.credentials, body });
  if (u.origin !== API) return reply(404, {});
  if (opts.method === "GET" && u.pathname === "/me") return reply(200, { id: 42, username: "tester" });
  if (opts.method === "GET" && u.pathname === "/users/42/playlists_without_albums")
    return reply(200, { collection: server.playlists, next_href: null });
  if (opts.method === "POST" && u.pathname === "/playlists") {
    if (scenario === "captcha") return reply(403, null, '{"url":"https://geo.captcha-delivery.com/captcha/?initialCid=x"}');
    if (scenario === "reject_description" && body.playlist.description) return reply(422, { error: "description" });
    const p = { id: server.nextId++, ...body.playlist, tracks: body.playlist.tracks.map((id) => ({ id })),
                track_count: body.playlist.tracks.length, permalink_url: "https://soundcloud.com/tester/sets/p" + server.nextId };
    server.playlists.push(p);
    return reply(201, p);
  }
  const m = /^\/playlists\/(\d+)$/.exec(u.pathname);
  if (opts.method === "PUT" && m) {
    const p = server.playlists.find((x) => x.id === Number(m[1]));
    if (!p) return reply(404, {});
    p.tracks = body.playlist.tracks.map((id) => ({ id }));
    p.track_count = body.playlist.tracks.length;
    return reply(200, p);
  }
  return reply(404, {});
}

const sandbox = {
  document: { cookie: "sc_anonymous_id=x; oauth_token=2-111-222-TokenFromCookie; datadome=DDcookieValue" },
  location: { hostname: "soundcloud.com" },
  performance: { getEntriesByType: () => [{ name: API + "/me?client_id=" + "P".repeat(32) + "&app_version=1" }] },
  localStorage: { getItem: (k) => (k in storage ? storage[k] : null), setItem: (k, v) => { storage[k] = String(v); } },
  prompt: () => null,
  fetch: fakeFetch,
  setTimeout,
  URL,
  console: {
    log: (...a) => logs.push(a.filter((x) => typeof x === "string" && !x.startsWith("%c") && !x.startsWith("color")).join(" ")),
    error: (...a) => logs.push("ERROR " + a.join(" ")),
    table: () => logs.push("TABLE"),
  },
};
sandbox.window = sandbox;
const ctx = vm.createContext(sandbox);

(async () => {
  const results = [];
  for (let i = 0; i < Number(runsArg); i++) {
    vm.runInContext(code, ctx);
    results.push(await ctx.vk2scRun);
  }
  console.log(JSON.stringify({ results, requests, logs, storage, playlists: server.playlists }));
})().catch((e) => { console.error(e); process.exit(1); });
