/** The read API, and the terminal it serves.
 *
 *  Inventory for the terminal, saturation for the foundry. The terminal itself
 *  is served from here rather than from somewhere else: it reads only this
 *  API, so anything that can answer the API can hand out the page, and there
 *  is no second thing to deploy or keep in step. */

import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

import cors from "cors";
import express, { type Express } from "express";

import type { AggregatorConfig } from "./config.js";
import type { Store } from "./db.js";
import type { Indexer } from "./indexer.js";
import { saturation } from "./saturation.js";

function intParam(value: unknown, fallback: number, min: number, max: number): number {
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, Math.floor(n)));
}

/** Where the served files live. This package compiles to CommonJS, so
 *  __dirname is the module's directory: dist/ when built, src/ under tsx, and
 *  public/ sits beside both. Resolved once, so a missing directory shows up in
 *  the startup log rather than as a 404 the first time somebody looks. */
function publicDir(): string | undefined {
  for (const candidate of [join(__dirname, "../public"), join(__dirname, "public")]) {
    if (existsSync(join(candidate, "terminal.html"))) return candidate;
  }
  return undefined;
}

/** The terminal page, read once at startup. */
function terminalPage(): string | undefined {
  const dir = publicDir();
  if (!dir) return undefined;
  try {
    return readFileSync(join(dir, "terminal.html"), "utf8");
  } catch {
    return undefined;
  }
}

export function createApi(cfg: AggregatorConfig, store: Store, indexer: Indexer): Express {
  const app = express();
  app.use(cors());
  app.disable("x-powered-by");

  // The legal documents and the stylesheet they share. Served from disk rather
  // than held in memory: nobody reads them often enough for that to matter.
  const dir = publicDir();
  if (dir) {
    app.use(
      express.static(dir, {
        index: false,
        dotfiles: "ignore",
        setHeaders: (res) => res.setHeader("x-content-type-options", "nosniff"),
      }),
    );
  }

  const page = terminalPage();
  app.get(["/", "/terminal"], (_req, res) => {
    if (!page) {
      res.status(404).json({ error: "no terminal page is installed" });
      return;
    }
    res
      .type("html")
      .set({
        "cache-control": "no-store",
        "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer",
        // The page talks to this origin only and embeds nothing.
        "content-security-policy":
          "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline' 'self'; " +
          "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
      })
      .send(page);
  });

  app.get("/health", (_req, res) => {
    res.json({
      ok: true,
      sources: cfg.sources.length,
      chains: cfg.chains.length,
      indexed: indexer.stats,
    });
  });

  /** What is being indexed, so a client can build its own filters. */
  app.get("/api/sources", (_req, res) => {
    res.json({
      chains: cfg.chains.map((c) => ({ chainId: c.chainId, label: c.label })),
      sources: cfg.sources.map((s) => ({
        id: s.id,
        label: s.label,
        kind: s.kind,
        chainId: s.chainId,
        address: s.address,
      })),
    });
  });

  /** Inventory: every coin, whoever launched it. */
  app.get("/api/coins", (req, res) => {
    const limit = intParam(req.query.limit, 100, 1, 500);
    const offset = intParam(req.query.offset, 0, 0, 1_000_000);
    const chainId = req.query.chainId === undefined ? undefined : intParam(req.query.chainId, 0, 0, 2 ** 31);
    const sourceId = typeof req.query.source === "string" ? req.query.source : undefined;
    if (sourceId && !cfg.sources.some((s) => s.id === sourceId)) {
      res.status(400).json({ error: `unknown source ${sourceId}` });
      return;
    }
    const rows = store.coins({ chainId, sourceId, limit, offset });
    res.json({
      coins: rows.map((r) => ({
        chainId: r.chain_id,
        address: r.address,
        source: r.source_id,
        name: r.name,
        symbol: r.symbol,
        creator: r.creator,
        launchedAt: r.launched_at,
        launchBlock: r.launch_block,
        graduated: r.graduated === 1,
        graduatedAt: r.graduated_at,
        poolId: r.pool_id,
        // Wei as decimal strings, all the way to the browser. Rounding money
        // on the way out is not a presentation choice.
        targetRaiseWei: r.target_raise_wei,
        deadline: r.deadline,
        raisedWei: r.raised_wei,
        pairSeededWei: r.pair_seeded_wei,
      })),
      limit,
      offset,
    });
  });

  /** How crowded each chain is. The foundry reads `rivalsByChain`. */
  app.get("/api/saturation", (req, res) => {
    const windowDays = intParam(req.query.windowDays, cfg.windowDays, 1, 365);
    res.json(saturation(store, windowDays));
  });

  app.use((_req, res) => res.status(404).json({ error: "not found" }));
  return app;
}
