/** Storage. One row per coin, whoever launched it.
 *
 *  The schema follows `backend/src/db.ts` — SQLite with WAL, amounts as TEXT so
 *  no bigint is ever rounded through a float — but is keyed by (chain, address)
 *  rather than by address alone, because the same address can exist on two
 *  chains and the terminal spans both. */

import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";

import Database from "better-sqlite3";

export interface CoinRow {
  chain_id: number;
  address: string;
  source_id: string;
  name: string;
  symbol: string;
  creator: string;
  launched_at: number;
  launch_block: number;
  graduated: number;
  graduated_at: number | null;
  pool_id: string | null;
  /** Wei as decimal strings: a wei amount fits neither a JS number nor a
   *  SQLite integer. Null where the launchpad's event does not carry it. */
  target_raise_wei: string | null;
  deadline: number | null;
  raised_wei: string | null;
  pair_seeded_wei: string | null;
}

export function openDb(path: string): Database.Database {
  mkdirSync(dirname(resolve(path)), { recursive: true });
  const db = new Database(path);
  db.pragma("journal_mode = WAL");
  db.pragma("synchronous = NORMAL");
  db.exec(`
    CREATE TABLE IF NOT EXISTS cursors (
      source_id TEXT PRIMARY KEY,
      last_block INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS coins (
      chain_id INTEGER NOT NULL,
      address TEXT NOT NULL,
      source_id TEXT NOT NULL,
      name TEXT NOT NULL DEFAULT '',
      symbol TEXT NOT NULL DEFAULT '',
      creator TEXT NOT NULL DEFAULT '',
      launched_at INTEGER NOT NULL,
      launch_block INTEGER NOT NULL,
      graduated INTEGER NOT NULL DEFAULT 0,
      graduated_at INTEGER,
      pool_id TEXT,
      -- The launch and graduation events carry the money; wei is kept as TEXT
      -- because it does not fit a JS number and SQLite has no 256-bit integer.
      target_raise_wei TEXT,
      deadline INTEGER,
      raised_wei TEXT,
      pair_seeded_wei TEXT,
      PRIMARY KEY (chain_id, address)
    );
    CREATE INDEX IF NOT EXISTS coins_source_launched ON coins (source_id, launched_at DESC);
    CREATE INDEX IF NOT EXISTS coins_launched ON coins (launched_at DESC);
    CREATE INDEX IF NOT EXISTS coins_chain_launched ON coins (chain_id, launched_at DESC);
  `);

  // Additive migration for databases written before these columns existed:
  // an index built up over weeks is not worth discarding to add a column.
  const existing = new Set(
    (db.prepare("PRAGMA table_info(coins)").all() as { name: string }[]).map((c) => c.name),
  );
  for (const [col, type] of [
    ["target_raise_wei", "TEXT"],
    ["deadline", "INTEGER"],
    ["raised_wei", "TEXT"],
    ["pair_seeded_wei", "TEXT"],
  ] as const) {
    if (!existing.has(col)) db.exec(`ALTER TABLE coins ADD COLUMN ${col} ${type}`);
  }
  return db;
}

export interface Store {
  cursor(sourceId: string, fallback: number): number;
  setCursor(sourceId: string, block: number): void;
  /** The amounts are optional: a launchpad whose launch event does not publish
   *  a target has no target, and a caller should not have to say so. */
  upsertCoin(
    row: Omit<
      CoinRow,
      "graduated" | "graduated_at" | "pool_id" | "raised_wei" | "pair_seeded_wei" | "target_raise_wei" | "deadline"
    > &
      Partial<Pick<CoinRow, "target_raise_wei" | "deadline">>,
  ): void;
  markGraduated(
    chainId: number,
    address: string,
    at: number,
    poolId: string | null,
    amounts?: { raisedWei: string | null; pairSeededWei: string | null },
  ): void;
  coins(opts: { chainId?: number; sourceId?: string; limit: number; offset: number }): CoinRow[];
  countsSince(since: number): { chain_id: number; source_id: string; launches: number; graduations: number }[];
  /** Completed turnovers in the window: how long each coin took from launch to
   *  an open market, per launchpad. The competitive measure that launch counts
   *  cannot give — two launchpads graduating the same share of coins are not
   *  equally good if one takes a week and the other a quarter. */
  turnoversSince(since: number): { chain_id: number; source_id: string; secs: number }[];
  totals(): { chain_id: number; source_id: string; coins: number; graduated: number }[];
}

export function makeStore(db: Database.Database): Store {
  const getCursor = db.prepare<[string], { last_block: number }>("SELECT last_block FROM cursors WHERE source_id = ?");
  const putCursor = db.prepare(
    "INSERT INTO cursors (source_id, last_block) VALUES (?, ?) ON CONFLICT(source_id) DO UPDATE SET last_block = excluded.last_block",
  );
  // A re-scan must not overwrite what is already known, so a repeated launch
  // is ignored rather than reset; graduation is applied by its own statement.
  const putCoin = db.prepare(`
    INSERT INTO coins
      (chain_id, address, source_id, name, symbol, creator, launched_at, launch_block,
       target_raise_wei, deadline)
    VALUES
      (@chain_id, @address, @source_id, @name, @symbol, @creator, @launched_at, @launch_block,
       @target_raise_wei, @deadline)
    ON CONFLICT(chain_id, address) DO NOTHING
  `);
  const putGraduated = db.prepare(`
    UPDATE coins
       SET graduated = 1,
           graduated_at = @at,
           pool_id = @pool_id,
           -- COALESCE so a re-scan that decodes no amount does not wipe one
           -- an earlier pass recorded.
           raised_wei = COALESCE(@raised_wei, raised_wei),
           pair_seeded_wei = COALESCE(@pair_seeded_wei, pair_seeded_wei)
     WHERE chain_id = @chain_id AND address = @address
  `);

  return {
    cursor(sourceId, fallback) {
      return getCursor.get(sourceId)?.last_block ?? fallback;
    },
    setCursor(sourceId, block) {
      putCursor.run(sourceId, block);
    },
    upsertCoin(row) {
      putCoin.run({
        target_raise_wei: null,
        deadline: null,
        ...row,
        address: row.address.toLowerCase(),
        creator: row.creator.toLowerCase(),
      });
    },
    markGraduated(chainId, address, at, poolId, amounts) {
      putGraduated.run({
        at,
        pool_id: poolId,
        chain_id: chainId,
        address: address.toLowerCase(),
        raised_wei: amounts?.raisedWei ?? null,
        pair_seeded_wei: amounts?.pairSeededWei ?? null,
      });
    },
    coins({ chainId, sourceId, limit, offset }) {
      const where: string[] = [];
      const args: unknown[] = [];
      if (chainId !== undefined) {
        where.push("chain_id = ?");
        args.push(chainId);
      }
      if (sourceId !== undefined) {
        where.push("source_id = ?");
        args.push(sourceId);
      }
      const sql = `SELECT * FROM coins ${where.length ? `WHERE ${where.join(" AND ")}` : ""} ORDER BY launched_at DESC LIMIT ? OFFSET ?`;
      return db.prepare(sql).all(...args, limit, offset) as CoinRow[];
    },
    countsSince(since) {
      return db
        .prepare(
          `SELECT chain_id, source_id, COUNT(*) AS launches,
                  SUM(CASE WHEN graduated = 1 THEN 1 ELSE 0 END) AS graduations
             FROM coins WHERE launched_at >= ?
            GROUP BY chain_id, source_id ORDER BY launches DESC`,
        )
        .all(since) as { chain_id: number; source_id: string; launches: number; graduations: number }[];
    },
    turnoversSince(since) {
      return db
        .prepare(
          // Only coins whose graduation we actually saw, and only where both
          // timestamps are sane: a negative span would be a reorg or a bad
          // block read, not a fast launchpad.
          `SELECT chain_id, source_id, (graduated_at - launched_at) AS secs
             FROM coins
            WHERE graduated = 1 AND graduated_at IS NOT NULL
              AND launched_at > 0 AND graduated_at >= launched_at
              AND graduated_at >= ?
            ORDER BY secs ASC`,
        )
        .all(since) as { chain_id: number; source_id: string; secs: number }[];
    },
    totals() {
      return db
        .prepare(
          `SELECT chain_id, source_id, COUNT(*) AS coins,
                  SUM(CASE WHEN graduated = 1 THEN 1 ELSE 0 END) AS graduated
             FROM coins GROUP BY chain_id, source_id ORDER BY coins DESC`,
        )
        .all() as { chain_id: number; source_id: string; coins: number; graduated: number }[];
    },
  };
}
