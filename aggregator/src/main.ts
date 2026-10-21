import * as http from "node:http";

import { WebSocketServer, type WebSocket } from "ws";

import { createApi } from "./api.js";
import { loadConfig } from "./config.js";
import { makeStore, openDb } from "./db.js";
import { Hub } from "./hub.js";
import { Indexer } from "./indexer.js";

const CHANNELS = new Set(["coin:launched", "coin:graduated"]);

async function main() {
  const cfg = loadConfig();
  const db = openDb(cfg.dbPath);
  const store = makeStore(db);
  const hub = new Hub();
  const indexer = new Indexer(cfg, store, hub);

  const app = createApi(cfg, store, indexer);
  const server = http.createServer(app);
  const wss = new WebSocketServer({ server, path: "/ws" });

  wss.on("connection", (ws: WebSocket) => {
    (ws as WebSocket & { isAlive?: boolean }).isAlive = true;
    ws.on("pong", () => ((ws as WebSocket & { isAlive?: boolean }).isAlive = true));
    ws.on("message", (raw) => {
      let msg: { op?: string; channel?: string; token?: string } | undefined;
      try {
        msg = JSON.parse(String(raw));
      } catch {
        ws.send(JSON.stringify({ channel: "error", message: "invalid json" }));
        return;
      }
      if (!msg?.channel || !CHANNELS.has(msg.channel)) {
        ws.send(JSON.stringify({ channel: "error", message: "unknown channel" }));
        return;
      }
      if (msg.op === "subscribe") {
        hub.subscribe(ws, msg.channel, msg.token);
        ws.send(JSON.stringify({ channel: "subscribed", topic: msg.channel, token: msg.token }));
      } else if (msg.op === "unsubscribe") {
        hub.unsubscribe(ws, msg.channel, msg.token);
        ws.send(JSON.stringify({ channel: "unsubscribed", topic: msg.channel, token: msg.token }));
      }
    });
    ws.on("close", () => hub.drop(ws));
    ws.on("error", () => hub.drop(ws));
  });

  const heartbeat = setInterval(() => {
    for (const ws of wss.clients) {
      const w = ws as WebSocket & { isAlive?: boolean };
      if (w.isAlive === false) {
        hub.drop(ws);
        ws.terminate();
        continue;
      }
      w.isAlive = false;
      ws.ping();
    }
  }, 30_000);

  server.listen(cfg.port, () => {
    console.log(`aggregator listening on :${cfg.port}`);
    console.log(`  sources: ${cfg.sources.map((s) => s.id).join(", ")}`);
    console.log(`  REST http://localhost:${cfg.port}/api/coins   WS ws://localhost:${cfg.port}/ws`);
  });

  // Indexing starts after the server is up, so /health answers during a long
  // first backfill instead of looking dead to the platform's health check.
  void indexer.start();

  const shutdown = () => {
    clearInterval(heartbeat);
    indexer.stop();
    wss.close();
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 3_000).unref();
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
}

main().catch((e) => {
  console.error(e instanceof Error ? e.message : e);
  process.exit(1);
});
