import { describe, expect, it } from "vitest";

import {
  applyIncome,
  defaultSelectionConfig,
  depreciation,
  fitness,
  incomePerCopy,
  nextSpec,
  parsePopulation,
  paysItsWay,
  reap,
  serializePopulation,
  turnoverRate,
  type Member,
  type Population,
} from "./selection.js";
import { baseSpec, specId, type LaunchSpec } from "./spec.js";

const DAY = 86_400;
const cfg = { ...defaultSelectionConfig, graceLaunches: 0 };
const NOW = 1_700_000_000;

function member(spec: LaunchSpec, over: Partial<Member> = {}): Member {
  return {
    spec,
    id: specId(spec),
    copies: 1,
    feesEthWei: 0n,
    graduated: 0,
    born: 0,
    lastLaunch: 0,
    ...over,
  };
}

const pop = (members: Member[], rivals: Record<string, number> = {}): Population => ({ epoch: 0, members, rivals });

describe("turnovers per year", () => {
  it("is one when the turnover has never been measured", () => {
    // Neither rewarded nor punished for missing data.
    expect(turnoverRate(undefined, cfg)).toBe(1_000_000n);
    expect(turnoverRate(0, cfg)).toBe(1_000_000n);
  });

  it("counts a yearly turnover as one and a monthly one as about twelve", () => {
    expect(turnoverRate(31_557_600, cfg)).toBe(1_000_000n);
    const monthly = Number(turnoverRate(31_557_600 / 12, cfg)) / 1e6;
    expect(monthly).toBeCloseTo(12, 5);
  });

  it("is capped, so one lucky fast coin cannot dominate on noise", () => {
    const capped = turnoverRate(1, cfg); // a one-second turnover
    expect(capped).toBe(BigInt(cfg.maxTurnoversPerYear) * 1_000_000n);
    expect(turnoverRate(60, cfg)).toBe(capped);
  });

  it("never falls to zero, however slow", () => {
    expect(turnoverRate(100 * 31_557_600, cfg)).toBeGreaterThan(0n);
  });
});

describe("Capital II's arithmetic: equal income, unequal turnover", () => {
  // "there is a difference of 900% in the annual rate of surplus-value of the
  // two capitals A and B" — Capital II ch. 16. Two formats take the same fees
  // per copy; one turns over ten times as fast.
  const fast = baseSpec({ tenant: "t", profile: "crypto" });
  const slow = baseSpec({ tenant: "t", profile: "quant_research" });

  it("scores the faster one ten times higher at a tenfold turnover gap", () => {
    const income = 10n ** 18n;
    const a = member(fast, { feesEthWei: income, meanTurnoverSecs: 3 * DAY, lastLaunchAt: NOW });
    const b = member(slow, { feesEthWei: income, meanTurnoverSecs: 30 * DAY, lastLaunchAt: NOW });
    const p = pop([a, b]);
    const fa = fitness(p, a, cfg, NOW);
    const fb = fitness(p, b, cfg, NOW);
    expect(fa).toBeGreaterThan(fb);
    expect(Number(fa) / Number(fb)).toBeCloseTo(10, 3);
  });

  it("ranks the faster one first, where the old measure called them equal", () => {
    const income = 10n ** 18n;
    const a = member(fast, { feesEthWei: income, meanTurnoverSecs: 90 * DAY, lastLaunchAt: NOW });
    const b = member(slow, { feesEthWei: income, meanTurnoverSecs: 3 * DAY, lastLaunchAt: NOW });
    const p = pop([a, b]);
    // Income per copy, the whole of the old measure, cannot tell them apart.
    expect(incomePerCopy(p, a)).toBe(incomePerCopy(p, b));
    expect(nextSpec(p, cfg, NOW)?.id).toBe(b.id);
  });

  it("still prices saturation: copies and turnover both count", () => {
    const income = 10n ** 18n;
    // Same turnover, but one format has ten times the live copies.
    const a = member(fast, { feesEthWei: income, copies: 1, meanTurnoverSecs: 7 * DAY, lastLaunchAt: NOW });
    const b = member(slow, { feesEthWei: income, copies: 10, meanTurnoverSecs: 7 * DAY, lastLaunchAt: NOW });
    const p = pop([a, b]);
    expect(Number(fitness(p, a, cfg, NOW)) / Number(fitness(p, b, cfg, NOW))).toBeCloseTo(10, 3);
  });
});

describe("an open turnover counts against the format", () => {
  // Capital II ch. 5: capital in circulation "produces neither commodities nor
  // surplus-value". A coin sitting on the curve is exactly that.
  it("scores a coin stuck on the curve below one that graduated quickly", () => {
    const quick = baseSpec({ tenant: "t", profile: "crypto" });
    const stuck = baseSpec({ tenant: "t", profile: "data_products" });
    const income = 10n ** 18n;
    const a = member(quick, { feesEthWei: income, meanTurnoverSecs: 2 * DAY, lastLaunchAt: NOW });
    // Launched 120 days ago and never graduated: the turnover is still running.
    const b = member(stuck, { feesEthWei: income, meanTurnoverSecs: 120 * DAY, lastLaunchAt: NOW });
    const p = pop([a, b]);
    expect(fitness(p, a, cfg, NOW)).toBeGreaterThan(fitness(p, b, cfg, NOW));
  });
});

describe("depreciation", () => {
  it("leaves fresh earnings alone and halves them at the half-life", () => {
    expect(depreciation(NOW, cfg, NOW)).toBe(1_000_000n);
    const halved = Number(depreciation(NOW - cfg.incomeHalfLifeDays * DAY, cfg, NOW)) / 1e6;
    expect(halved).toBeCloseTo(0.5, 5);
    const quartered = Number(depreciation(NOW - 2 * cfg.incomeHalfLifeDays * DAY, cfg, NOW)) / 1e6;
    expect(quartered).toBeCloseTo(0.25, 5);
  });

  it("applies no discount when the age is unknown, rather than writing it off", () => {
    expect(depreciation(undefined, cfg, NOW)).toBe(1_000_000n);
    expect(depreciation(0, cfg, NOW)).toBe(1_000_000n);
  });

  it("can be switched off", () => {
    const off = { ...cfg, incomeHalfLifeDays: 0 };
    expect(depreciation(NOW - 365 * DAY, off, NOW)).toBe(1_000_000n);
  });

  it("prefers a format earning the same now over one that earned it long ago", () => {
    const recent = baseSpec({ tenant: "t", profile: "crypto" });
    const stale = baseSpec({ tenant: "t", profile: "hedge_fund" });
    const income = 10n ** 18n;
    const a = member(recent, { feesEthWei: income, meanTurnoverSecs: 7 * DAY, lastLaunchAt: NOW - DAY });
    const b = member(stale, { feesEthWei: income, meanTurnoverSecs: 7 * DAY, lastLaunchAt: NOW - 180 * DAY });
    const p = pop([a, b]);
    expect(fitness(p, a, cfg, NOW)).toBeGreaterThan(fitness(p, b, cfg, NOW));
    expect(nextSpec(p, cfg, NOW)?.id).toBe(a.id);
  });
});

describe("the cost test stays a cash comparison", () => {
  // Annualising is right for ranking and wrong for "did this cover its cost":
  // a rate against a cash amount is a category error, and a fast turnover
  // would otherwise make a loss-making format look solvent.
  it("reaps a format that did not cover a launch, however fast it turned over", () => {
    const spec = baseSpec({ tenant: "t", profile: "crypto" });
    const m = member(spec, {
      copies: 4,
      feesEthWei: cfg.costPerLaunchWei, // a quarter of cost per copy
      meanTurnoverSecs: 3600, // very fast, so the annualised rate looks large
      lastLaunchAt: NOW,
    });
    const p = pop([m]);
    expect(fitness(p, m, cfg, NOW)).toBeGreaterThan(cfg.costPerLaunchWei);
    expect(paysItsWay(p, m, cfg)).toBe(false);
    expect(reap(p, cfg).length).toBeLessThanOrEqual(1);
  });

  it("keeps one that did cover it", () => {
    const spec = baseSpec({ tenant: "t", profile: "crypto" });
    const m = member(spec, { copies: 2, feesEthWei: cfg.costPerLaunchWei * 10n, meanTurnoverSecs: 30 * DAY, lastLaunchAt: NOW });
    expect(paysItsWay(pop([m]), m, cfg)).toBe(true);
  });
});

describe("folding measured timing back in", () => {
  it("takes the turnover, completions and last launch from the chain", () => {
    const spec = baseSpec({ tenant: "t" });
    const m = member(spec, { copies: 0 });
    const updated = applyIncome(
      pop([m]),
      new Map([
        [
          m.id.toLowerCase(),
          { totalEthWei: 5n, tokens: [1, 2], graduated: 1, meanTurnoverSecs: 4 * DAY, turnoversComplete: 1, lastLaunchAt: NOW },
        ],
      ]),
    );
    const row = updated.members[0]!;
    expect(row.meanTurnoverSecs).toBe(4 * DAY);
    expect(row.turnoversComplete).toBe(1);
    expect(row.lastLaunchAt).toBe(NOW);
    expect(row.copies).toBe(2);
  });

  it("leaves timing alone when the chain reported none", () => {
    const spec = baseSpec({ tenant: "t" });
    const m = member(spec, { meanTurnoverSecs: 9 * DAY, lastLaunchAt: NOW });
    const updated = applyIncome(pop([m]), new Map([[m.id.toLowerCase(), { totalEthWei: 1n, tokens: [1], graduated: 0 }]]));
    // Zeroing these would read as "unmeasured" and silently drop the term.
    expect(updated.members[0]!.meanTurnoverSecs).toBe(9 * DAY);
    expect(updated.members[0]!.lastLaunchAt).toBe(NOW);
  });

  it("round-trips the timing through the saved population", () => {
    const spec = baseSpec({ tenant: "t" });
    const m = member(spec, { meanTurnoverSecs: 11 * DAY, turnoversComplete: 3, lastLaunchAt: NOW });
    const back = parsePopulation(serializePopulation(pop([m])));
    expect(back.members[0]!.meanTurnoverSecs).toBe(11 * DAY);
    expect(back.members[0]!.turnoversComplete).toBe(3);
    expect(back.members[0]!.lastLaunchAt).toBe(NOW);
  });
});
