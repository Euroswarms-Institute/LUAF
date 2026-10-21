import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { adapterFor, genericAdapter, ventureAdapter } from "./adapters.js";
import type { SourceConfig } from "./config.js";
import { makeStore, openDb, type Store } from "./db.js";
import { MAX_TURNOVERS_PER_YEAR, saturation } from "./saturation.js";

function store(): Store {
  return makeStore(openDb(join(mkdtempSync(join(tmpdir(), "agg-")), "test.db")));
}

const coin = (over: Partial<Parameters<Store["upsertCoin"]>[0]> = {}) => ({
  chain_id: 4663,
  address: "0x2ADD000000000000000000000000000000000001",
  source_id: "doubleplus",
  name: "Agent",
  symbol: "AGT",
  creator: "0xAAAA000000000000000000000000000000000001",
  launched_at: 1_000,
  launch_block: 10,
  ...over,
});

const log = (args: Record<string, unknown>, block = 7n) =>
  ({ args, blockNumber: block } as unknown as Parameters<ReturnType<typeof ventureAdapter>["decodeLaunch"]>[0]);

describe("the generic adapter", () => {
  const base: SourceConfig = {
    id: "third-party",
    label: "Someone else",
    kind: "generic",
    chainId: 8453,
    address: "0x1111111111111111111111111111111111111111",
    startBlock: 0,
    launchEvent: "event TokenCreated(address indexed token, address indexed creator, string name, string symbol)",
    map: { token: "token", creator: "creator", name: "name", symbol: "symbol" },
  };

  it("decodes a launch described purely in configuration", () => {
    const a = genericAdapter(base);
    const rec = a.decodeLaunch(
      log({
        token: "0x3333333333333333333333333333333333333333",
        creator: "0x4444444444444444444444444444444444444444",
        name: "Thing",
        symbol: "THG",
      }),
    );
    expect(rec).toEqual({
      address: "0x3333333333333333333333333333333333333333",
      creator: "0x4444444444444444444444444444444444444444",
      name: "Thing",
      symbol: "THG",
      block: 7,
    });
  });

  it("refuses a description that does not match its own event", () => {
    expect(() => genericAdapter({ ...base, map: { token: "nope" } })).toThrow(/map.token names "nope"/);
    expect(() => genericAdapter({ ...base, map: { token: "token", creator: "missing" } })).toThrow(/map.creator/);
    expect(() => genericAdapter({ ...base, launchEvent: undefined })).toThrow(/needs launchEvent/);
    expect(() => genericAdapter({ ...base, launchEvent: "function notAnEvent()" })).toThrow(/not an event/);
  });

  it("requires a mapped token on a graduation event", () => {
    expect(() => genericAdapter({ ...base, graduateEvent: "event Graduated(address indexed token)" })).toThrow(
      /graduateMap.token/,
    );
    const ok = genericAdapter({
      ...base,
      graduateEvent: "event Graduated(address indexed token)",
      graduateMap: { token: "token" },
    });
    expect(ok.decodeGraduation(log({ token: "0x5555555555555555555555555555555555555555" }))?.address).toBe(
      "0x5555555555555555555555555555555555555555",
    );
  });

  it("skips a log whose shape is not what was described, rather than guessing", () => {
    const a = genericAdapter(base);
    expect(a.decodeLaunch(log({ token: 42 }))).toBeUndefined();
    expect(a.decodeLaunch(log({ token: "0xtooshort" }))).toBeUndefined();
    expect(a.decodeLaunch(log({}))).toBeUndefined();
  });

  it("reports no graduation when the source describes none", () => {
    expect(genericAdapter(base).decodeGraduation(log({ token: "0x6666666666666666666666666666666666666666" }))).toBeUndefined();
  });

  it("leaves name and symbol empty when the event omits them, for the indexer to read", () => {
    const a = genericAdapter({ ...base, map: { token: "token" } });
    const rec = a.decodeLaunch(log({ token: "0x7777777777777777777777777777777777777777" }));
    expect(rec?.name).toBe("");
    expect(rec?.creator).toBe("");
  });
});

describe("the venture adapter", () => {
  it("reads our own launches and graduations", () => {
    const a = ventureAdapter();
    const rec = a.decodeLaunch(
      log({ token: "0x2add000000000000000000000000000000000001", creator: "0x9999999999999999999999999999999999999999" }),
    );
    expect(rec?.address).toBe("0x2add000000000000000000000000000000000001");
    expect(rec?.creator).toBe("0x9999999999999999999999999999999999999999");
    const grad = a.decodeGraduation(log({ token: "0x2add000000000000000000000000000000000001", poolId: "0xabc" }));
    expect(grad).toEqual({ address: "0x2add000000000000000000000000000000000001", poolId: "0xabc", block: 7 });
  });

  it("is chosen by kind", () => {
    const s: SourceConfig = {
      id: "dp",
      label: "doubleplus",
      kind: "venture",
      chainId: 4663,
      address: "0x1111111111111111111111111111111111111111",
      startBlock: 0,
    };
    expect(adapterFor(s).graduateEvent?.name).toBe("Graduated");
  });
});

describe("the store", () => {
  it("keys a coin by chain and address, lowercased", () => {
    const s = store();
    s.upsertCoin(coin());
    const rows = s.coins({ limit: 10, offset: 0 });
    expect(rows).toHaveLength(1);
    expect(rows[0]!.address).toBe("0x2add000000000000000000000000000000000001");
    expect(rows[0]!.creator).toBe("0xaaaa000000000000000000000000000000000001");
  });

  it("lets the same address exist on two chains", () => {
    const s = store();
    s.upsertCoin(coin());
    s.upsertCoin(coin({ chain_id: 8453, source_id: "other" }));
    expect(s.coins({ limit: 10, offset: 0 })).toHaveLength(2);
    expect(s.coins({ chainId: 8453, limit: 10, offset: 0 })).toHaveLength(1);
  });

  it("does not let a re-scan overwrite what is known", () => {
    const s = store();
    s.upsertCoin(coin());
    s.markGraduated(4663, coin().address, 2_000, "0xpool");
    s.upsertCoin(coin({ name: "Rescanned" }));
    const row = s.coins({ limit: 1, offset: 0 })[0]!;
    expect(row.name).toBe("Agent");
    expect(row.graduated).toBe(1);
    expect(row.graduated_at).toBe(2_000);
  });

  it("marks graduation case-insensitively", () => {
    const s = store();
    s.upsertCoin(coin());
    s.markGraduated(4663, coin().address.toLowerCase(), 3_000, null);
    expect(s.coins({ limit: 1, offset: 0 })[0]!.graduated).toBe(1);
  });

  it("keeps a cursor per source", () => {
    const s = store();
    expect(s.cursor("a", 99)).toBe(99);
    s.setCursor("a", 500);
    expect(s.cursor("a", 99)).toBe(500);
    expect(s.cursor("b", 7)).toBe(7);
  });

  it("orders newest first and pages", () => {
    const s = store();
    for (let i = 0; i < 5; i++) {
      s.upsertCoin(coin({ address: `0x${(i + 1).toString(16).padStart(40, "0")}`, launched_at: 1_000 + i }));
    }
    const page = s.coins({ limit: 2, offset: 0 });
    expect(page).toHaveLength(2);
    expect(page[0]!.launched_at).toBe(1_004);
    expect(s.coins({ limit: 2, offset: 4 })).toHaveLength(1);
  });

  it("filters by source", () => {
    const s = store();
    s.upsertCoin(coin());
    s.upsertCoin(coin({ address: "0x00000000000000000000000000000000000000ff", source_id: "rival" }));
    expect(s.coins({ sourceId: "rival", limit: 10, offset: 0 })).toHaveLength(1);
  });
});

describe("saturation", () => {
  const now = 1_000_000;

  it("counts launches and graduations per source and per chain", () => {
    const s = store();
    s.upsertCoin(coin({ address: "0x01".padEnd(42, "0"), launched_at: now - 100 }));
    s.upsertCoin(coin({ address: "0x02".padEnd(42, "0"), launched_at: now - 200 }));
    s.markGraduated(4663, "0x02".padEnd(42, "0"), now - 150, null);
    s.upsertCoin(coin({ address: "0x03".padEnd(42, "0"), chain_id: 8453, source_id: "rival", launched_at: now - 300 }));

    const r = saturation(s, 7, now);
    expect(r.chains).toHaveLength(2);
    const rh = r.chains.find((c) => c.chainId === 4663)!;
    expect(rh.launches).toBe(2);
    expect(rh.graduations).toBe(1);
    expect(rh.graduationRate).toBe(0.5);
    expect(rh.sources[0]!.sourceId).toBe("doubleplus");
  });

  it("counts every launchpad on a chain as a rival, which is the point", () => {
    const s = store();
    s.upsertCoin(coin({ address: "0x01".padEnd(42, "0"), launched_at: now - 10 }));
    s.upsertCoin(coin({ address: "0x02".padEnd(42, "0"), source_id: "rival-pad", launched_at: now - 20 }));
    const r = saturation(s, 7, now);
    // Ours and theirs, pooled: competition is overstated rather than missed.
    expect(r.rivalsByChain["4663"]).toBe(2);
  });

  it("ignores launches older than the window", () => {
    const s = store();
    s.upsertCoin(coin({ address: "0x01".padEnd(42, "0"), launched_at: now - 86_400 * 30 }));
    s.upsertCoin(coin({ address: "0x02".padEnd(42, "0"), launched_at: now - 86_400 }));
    expect(saturation(s, 7, now).rivalsByChain["4663"]).toBe(1);
    expect(saturation(s, 60, now).rivalsByChain["4663"]).toBe(2);
  });

  it("reports no rate rather than a zero when nothing launched", () => {
    const r = saturation(store(), 7, now);
    expect(r.chains).toEqual([]);
    expect(r.rivalsByChain).toEqual({});
  });

  it("reports lifetime totals alongside the window", () => {
    const s = store();
    s.upsertCoin(coin({ address: "0x01".padEnd(42, "0"), launched_at: now - 86_400 * 90 }));
    const r = saturation(s, 7, now);
    expect(r.rivalsByChain).toEqual({});
    expect(r.totals[0]).toMatchObject({ chain_id: 4663, source_id: "doubleplus", coins: 1, graduated: 0 });
  });
});

describe("turnover: how fast each launchpad reaches an open market", () => {
  // A realistic clock: with a small one, a launch a year ago lands before the
  // epoch and the store rejects it as an impossible timestamp.
  const now = 1_800_000_000;
  const DAY = 86_400;

  /** A graduated coin that took `days` from launch to an open market. */
  const graduated = (addr: string, days: number, source = "doubleplus", chain = 4663) => ({
    ...coin({ address: addr, source_id: source, chain_id: chain, launched_at: now - days * DAY - 1000 }),
  });

  function seed(s: Store, rows: { addr: string; days: number; source?: string; chain?: number }[]) {
    for (const r of rows) {
      const g = graduated(r.addr, r.days, r.source, r.chain);
      s.upsertCoin(g);
      s.markGraduated(g.chain_id, g.address, g.launched_at + r.days * DAY, null);
    }
  }

  it("reports the median time to graduation per launchpad", () => {
    const s = store();
    seed(s, [
      { addr: "0x01".padEnd(42, "0"), days: 1 },
      { addr: "0x02".padEnd(42, "0"), days: 3 },
      { addr: "0x03".padEnd(42, "0"), days: 11 },
    ]);
    const src = saturation(s, 30, now).chains[0]!.sources[0]!;
    expect(src.medianTurnoverSecs).toBe(3 * DAY);
    expect(src.turnoversPerYear).toBeCloseTo(31_557_600 / (3 * DAY), 3);
  });

  it("uses the median so a few stragglers cannot hide a fast launchpad", () => {
    const s = store();
    seed(s, [
      { addr: "0x01".padEnd(42, "0"), days: 1 },
      { addr: "0x02".padEnd(42, "0"), days: 1 },
      { addr: "0x03".padEnd(42, "0"), days: 2 },
      { addr: "0x04".padEnd(42, "0"), days: 300 },
      { addr: "0x05".padEnd(42, "0"), days: 300 },
    ]);
    const src = saturation(s, 400, now).chains[0]!.sources[0]!;
    expect(src.medianTurnoverSecs).toBe(2 * DAY);
    // The mean would be about 121 days and call this a slow launchpad.
  });

  it("compares two launchpads on the same chain", () => {
    const s = store();
    seed(s, [
      { addr: "0x01".padEnd(42, "0"), days: 2, source: "doubleplus" },
      { addr: "0x02".padEnd(42, "0"), days: 2, source: "doubleplus" },
      { addr: "0x03".padEnd(42, "0"), days: 40, source: "rival-pad" },
      { addr: "0x04".padEnd(42, "0"), days: 40, source: "rival-pad" },
    ]);
    const chain = saturation(s, 90, now).chains[0]!;
    const ours = chain.sources.find((x) => x.sourceId === "doubleplus")!;
    const theirs = chain.sources.find((x) => x.sourceId === "rival-pad")!;
    expect(ours.medianTurnoverSecs).toBe(2 * DAY);
    expect(theirs.medianTurnoverSecs).toBe(40 * DAY);
    expect(ours.turnoversPerYear! / theirs.turnoversPerYear!).toBeCloseTo(20, 3);
  });

  it("takes the chain median from the coins, not from the sources' medians", () => {
    const s = store();
    // One fast source with one coin, one slow source with three. The median of
    // all four is 30 days; the median of the two source medians would be 20.
    seed(s, [
      { addr: "0x01".padEnd(42, "0"), days: 10, source: "a" },
      { addr: "0x02".padEnd(42, "0"), days: 30, source: "b" },
      { addr: "0x03".padEnd(42, "0"), days: 30, source: "b" },
      { addr: "0x04".padEnd(42, "0"), days: 30, source: "b" },
    ]);
    expect(saturation(s, 90, now).chains[0]!.medianTurnoverSecs).toBe(30 * DAY);
  });

  it("caps the implied rate, so one self-dealt graduation cannot dominate", () => {
    const s = store();
    // Measured against the real testnet stack this came out at 181,365
    // turnovers a year, from a coin whose operator filled his own curve in
    // 174 seconds. The median seconds stays as the fact; the rate is capped.
    seed(s, [{ addr: "0x01".padEnd(42, "0"), days: 174 / 86_400 }]);
    const src = saturation(s, 30, now).chains[0]!.sources[0]!;
    expect(src.medianTurnoverSecs).toBe(174);
    expect(src.turnoversPerYear).toBe(MAX_TURNOVERS_PER_YEAR);
  });

  it("counts a graduation that completed in minutes as self-dealt", () => {
    const s = store();
    seed(s, [
      { addr: "0x01".padEnd(42, "0"), days: 120 / 86_400 }, // 2 minutes
      { addr: "0x02".padEnd(42, "0"), days: 5 },
      { addr: "0x03".padEnd(42, "0"), days: 9 },
    ]);
    const src = saturation(s, 30, now).chains[0]!.sources[0]!;
    expect(src.fastGraduations).toBe(1);
    // Labelled, not dropped: it still counts as a graduation.
    expect(src.graduations).toBe(3);
    expect(src.medianTurnoverSecs).toBe(5 * DAY);
  });

  it("counts fast graduations at the chain level too", () => {
    const s = store();
    seed(s, [
      { addr: "0x01".padEnd(42, "0"), days: 60 / 86_400, source: "a" },
      { addr: "0x02".padEnd(42, "0"), days: 60 / 86_400, source: "b" },
      { addr: "0x03".padEnd(42, "0"), days: 7, source: "b" },
    ]);
    expect(saturation(s, 30, now).chains[0]!.fastGraduations).toBe(2);
  });

  it("flags nothing when every graduation took a plausible time", () => {
    const s = store();
    seed(s, [{ addr: "0x01".padEnd(42, "0"), days: 3 }, { addr: "0x02".padEnd(42, "0"), days: 8 }]);
    expect(saturation(s, 30, now).chains[0]!.sources[0]!.fastGraduations).toBe(0);
  });

  it("reports nothing rather than zero when no coin graduated", () => {
    const s = store();
    s.upsertCoin(coin({ address: "0x01".padEnd(42, "0"), launched_at: now - DAY }));
    const src = saturation(s, 30, now).chains[0]!.sources[0]!;
    expect(src.graduations).toBe(0);
    expect(src.medianTurnoverSecs).toBeNull();
    expect(src.turnoversPerYear).toBeNull();
  });

  it("ignores a coin whose timestamps are impossible", () => {
    const s = store();
    const c = coin({ address: "0x01".padEnd(42, "0"), launched_at: now });
    s.upsertCoin(c);
    // Graduated before it launched: a reorg or a bad block read, not speed.
    s.markGraduated(c.chain_id, c.address, now - 5 * DAY, null);
    expect(saturation(s, 30, now).chains[0]!.sources[0]!.medianTurnoverSecs).toBeNull();
  });
});
