// 실시간 국내 종목 뉴스 — 서버가 새 기사를 접속한 모든 사람에게 밀어주는 방식 (Cloudflare 무료 플랜)
//
// - 장중(평일 08:00~15:30, 휴장일 제외)에 보고 있는 사람이 있으면 20초마다 언론사 RSS를 한 번만 확인하고,
//   새 기사를 WebSocket으로 접속한 모두에게 동시에 보냄 → 보는 사람이 늘어도 서버 사용량은 거의 그대로
// - 보는 사람이 없거나 장이 끝나면 확인을 멈춤 (장 마감 후에는 화면이 GitHub 자동 수집분을 씀)
// - 종목 목록·삭제 목록·휴장일은 GitHub Pages 사이트(SITE_URL)에서 읽어 옴
//
// 주소: /ws (실시간 연결), /live (오늘 기사 목록 JSON, 점검용)

import { DurableObject } from "cloudflare:workers";
import { FEEDS, KST_MS, judge, kstDate, makeLists, marketOpen, parseRss } from "./rules.js";

const TICK_MS = 20 * 1000;          // 장중 확인 간격
const LIST_MS = 60 * 60 * 1000;     // 종목·삭제·휴장 목록 다시 읽는 간격
const UA = { "User-Agent": "Mozilla/5.0 (compatible; RiseBriefBot/1.0)" };
const CORS = { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, OPTIONS" };

export class LiveHub extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.sql = ctx.storage.sql;
    this.sql.exec("CREATE TABLE IF NOT EXISTS items (link TEXT PRIMARY KEY, day TEXT, time TEXT, data TEXT)");
    this.sql.exec("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)");
  }

  meta(k) {
    const rows = this.sql.exec("SELECT v FROM meta WHERE k = ?", k).toArray();
    return rows.length ? rows[0].v : null;
  }
  setMeta(k, v) { this.sql.exec("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", k, String(v)); }

  // 종목·삭제·휴장 목록 (1시간마다 사이트에서 다시 읽고, 저장해 둔 것을 씀)
  async lists() {
    if (Date.now() - Number(this.meta("listsAt") || 0) > LIST_MS) {
      const site = (this.env.SITE_URL || "").replace(/\/$/, "");
      try {
        const [names, block, closed] = await Promise.all([
          fetch(`${site}/stocks.json`).then(r => r.json()),
          fetch(`${site}/blocklist.json`).then(r => r.json()).catch(() => []),
          fetch(`${site}/holidays.json`).then(r => r.json()).catch(() => []),
        ]);
        this.setMeta("lists", JSON.stringify({ names, block, closed }));
        this.setMeta("listsAt", Date.now());
      } catch (e) {  // 사이트를 못 읽으면 저장해 둔 목록을 계속 쓰고 5분 뒤 다시 시도
        this.setMeta("listsAt", Date.now() - LIST_MS + 5 * 60 * 1000);
      }
      this._lists = null;
    }
    if (!this._lists) {  // 서버가 잠들었다 깨면 메모리가 비므로 저장해 둔 목록에서 다시 만듦
      const saved = JSON.parse(this.meta("lists") || '{"names":{},"block":[],"closed":[]}');
      this._lists = { ...makeLists(saved.names, saved.block), closed: saved.closed };
    }
    return this._lists;
  }

  snapshot(type = "snapshot") {
    const day = kstDate(Date.now());
    const items = this.sql.exec("SELECT data FROM items WHERE day = ? ORDER BY time DESC LIMIT 400", day)
      .toArray().map(r => JSON.parse(r.data));
    return { type, date: day, updatedAt: this.meta("lastRun"), items };
  }

  // 언론사 RSS를 읽어 새 기사만 저장하고 돌려줌
  async refresh() {
    const L = await this.lists();
    const now = Date.now();
    const day = kstDate(now);
    const dayStart = Date.parse(`${day}T00:00:00+09:00`);
    const known = new Set(this.sql.exec("SELECT link FROM items WHERE day = ?", day).toArray().map(r => r.link));
    const keys = new Set();
    const results = await Promise.allSettled(FEEDS.map(([source, url]) =>
      fetch(url, { headers: UA }).then(r => (r.ok ? r.text() : "")).then(xml => parseRss(xml, source, dayStart))));
    const added = [];
    for (const r of results) {
      if (r.status !== "fulfilled") continue;
      for (const it of r.value) {
        if (known.has(it.link) || it.t > now + 600000) continue;
        const key = it.title.replace(/[^\p{L}\p{N}]/gu, "").slice(0, 60);
        if (keys.has(key)) continue;
        const item = judge(it, L);
        if (!item) continue;
        keys.add(key);
        known.add(it.link);
        added.push(item);
        this.sql.exec("INSERT OR IGNORE INTO items (link, day, time, data) VALUES (?, ?, ?, ?)",
          item.link, day, item.time, JSON.stringify(item));
      }
    }
    this.sql.exec("DELETE FROM items WHERE day < ?", kstDate(now - 2 * 86400000));  // 이틀 지난 기사는 지움
    this.setMeta("lastRun", new Date(now).toISOString());
    return added.sort((a, b) => b.time.localeCompare(a.time));
  }

  broadcast(msg) {
    const text = JSON.stringify(msg);
    for (const ws of this.ctx.getWebSockets()) {
      try { ws.send(text); } catch (e) { /* 끊긴 연결은 무시 */ }
    }
  }

  async isOpen() { return marketOpen(Date.now(), (await this.lists()).closed); }

  async fetch(request) {
    const open = await this.isOpen();
    const stale = !(Date.now() - Date.parse(this.meta("lastRun") || "") <= TICK_MS);  // 기록이 없어도 '오래됨'
    if (open && stale) await this.refresh();  // 오랜만에 접속하면 먼저 최신으로

    if (request.headers.get("Upgrade") === "websocket") {
      const pair = new WebSocketPair();
      this.ctx.acceptWebSocket(pair[1]);  // 기다리는 동안 서버가 잠들 수 있는 방식(hibernation)으로 연결
      pair[1].send(JSON.stringify({ ...this.snapshot(), open }));
      if (open && (await this.ctx.storage.getAlarm()) == null) await this.ctx.storage.setAlarm(Date.now() + TICK_MS);
      return new Response(null, { status: 101, webSocket: pair[0] });
    }
    return new Response(JSON.stringify({ ...this.snapshot(), open }), {
      headers: { ...CORS, "content-type": "application/json; charset=utf-8" },
    });
  }

  // 장중 20초마다: 새 기사 확인 → 접속한 모두에게 전달. 보는 사람이 없거나 장이 끝나면 멈춤
  async alarm() {
    if (!this.ctx.getWebSockets().length) return;
    if (!(await this.isOpen())) {
      this.broadcast({ type: "closed" });  // 화면이 장 마감 모드(GitHub 수집분)로 바뀜
      return;
    }
    const added = await this.refresh();
    this.broadcast({ type: added.length ? "add" : "tick", date: kstDate(Date.now()), updatedAt: this.meta("lastRun"), items: added });
    await this.ctx.storage.setAlarm(Date.now() + TICK_MS);
  }

  webSocketClose(ws, code) {
    try { ws.close(code === 1005 ? 1000 : code); } catch (e) { /* 이미 닫힘 */ }
  }
  webSocketError(ws) {
    try { ws.close(1011); } catch (e) { /* 이미 닫힘 */ }
  }
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
    const path = new URL(request.url).pathname;
    if (path === "/ws" || path === "/live") {
      return env.HUB.get(env.HUB.idFromName("main")).fetch(request);  // 모든 사람이 같은 서버 하나에 연결
    }
    return new Response("실시간 국내 종목 뉴스: /ws (실시간 연결), /live (오늘 기사 목록)", {
      headers: { ...CORS, "content-type": "text/plain; charset=utf-8" },
    });
  },
};
