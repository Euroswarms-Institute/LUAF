import { describe, expect, it } from "vitest";

import { absorptionFrom, defaultAbsorptionLimits, mayLaunch, type AbsorptionLimits } from "./absorption.js";
import type { TokenIncome } from "./income.js";

const DAY = 86_400;
const NOW = 1_700_000_000;

function coin(over: Partial<TokenIncome> = {}): TokenIncome {
  return {
    token: "0x2add000000000000000000000000000000000001",
    specId: "0xabc",
    graduated: false,
    launchedAt: NOW - DAY,
    graduatedAt: null,
    turnoverSecs: DAY,
    turnoverComplete: false,
    curveCreatorWei: 0n,
    curvePlatformWei: 0n,
    poolPlatformPairWei: 0n,
    poolDevPairWei: 0n,
    poolPlatformCoin: 0n,
    poolDevCoin: 0n,
    totalEthWei: 0n,
    swaps: 0,
    ...over,
  };
}

const open = (n: number, ageDays = 1) =>
  Array.from({ length: n }, (_, i) => coin({ token: `0x${(i + 1).toString(16).padStart(40, "0")}`, launchedAt: NOW - ageDays * DAY }));
const done = (n: number) =>
  Array.from({ length: n }, (_, i) =>
    coin({ token: `0xf${(i + 1).toString(16).padStart(39, "0")}`, graduated: true, graduatedAt: NOW, turnoverComplete: true }),
  );

describe("measuring absorption", () => {
  it("separates unsold stock from what graduated", () => {
    const s = absorptionFrom([...open(3), ...done(2)], NOW);
    expect(s.launched).toBe(5);
    expect(s.open).toBe(3);
    expect(s.graduated).toBe(2);
    expect(s.graduationRate).toBeCloseTo(0.4, 6);
  });

  it("reports the oldest unsold coin, and ignores graduated ones for it", () => {
    const s = absorptionFrom([...open(1, 30), ...open(1, 2), ...done(1)], NOW);
    expect(s.oldestOpenSecs).toBe(30 * DAY);
  });

  it("has no rate before anything launched, rather than a zero", () => {
    const s = absorptionFrom([], NOW);
    expect(s.launched).toBe(0);
    expect(s.graduationRate).toBeNull();
    expect(s.oldestOpenSecs).toBe(0);
  });

  it("does not report a negative age for a coin launched in a later block", () => {
    const s = absorptionFrom(open(1, -5), NOW);
    expect(s.oldestOpenSecs).toBe(0);
  });

  it("skips an age it cannot read", () => {
    const s = absorptionFrom([coin({ launchedAt: 0 })], NOW);
    expect(s.open).toBe(1);
    expect(s.oldestOpenSecs).toBe(0);
  });
});

describe("whether to add another coin to the market", () => {
  const limits: AbsorptionLimits = { maxOpen: 4, minGraduationRate: 0.25, graceLaunches: 4 };

  it("allows a launch while the stock is moving", () => {
    const v = mayLaunch(absorptionFrom([...open(1), ...done(3)], NOW), limits);
    expect(v.ok).toBe(true);
  });

  it("refuses once the unsold stock hits the ceiling", () => {
    const v = mayLaunch(absorptionFrom(open(4), NOW), limits);
    expect(v.ok).toBe(false);
    expect(v.reason).toMatch(/still on the curve/);
    expect(v.reason).toMatch(/glut/);
  });

  it("names the oldest straggler, so a wall of fresh coins reads differently", () => {
    const v = mayLaunch(absorptionFrom(open(4, 45), NOW), limits);
    expect(v.reason).toMatch(/oldest 45\.0 days/);
  });

  it("refuses when too little of what launched ever graduates", () => {
    // The stock ceiling is off here, so only the rate is under test.
    const rateOnly = { ...limits, maxOpen: 0 };
    // 1 of 10 graduated: 10%, under the 25% floor.
    const glutted = absorptionFrom([...done(1), ...open(9)], NOW);
    expect(glutted.graduationRate).toBeCloseTo(0.1, 6);
    const refused = mayLaunch(glutted, rateOnly);
    expect(refused.ok).toBe(false);
    expect(refused.reason).toMatch(/not absorbing/);
    // 5 of 10 graduated clears it comfortably.
    expect(mayLaunch(absorptionFrom([...done(5), ...open(5)], NOW), rateOnly).ok).toBe(true);
  });

  it("will not judge the rate on too small a sample", () => {
    // 1 of 3 graduated is 33%, but under the grace count it is not acted on
    // either way; make it fail the floor to prove the grace is what spares it.
    const thin = absorptionFrom([...done(0), ...open(3)], NOW);
    expect(thin.graduationRate).toBe(0);
    expect(mayLaunch(thin, { maxOpen: 0, minGraduationRate: 0.25, graceLaunches: 4 }).ok).toBe(true);
    expect(mayLaunch(thin, { maxOpen: 0, minGraduationRate: 0.25, graceLaunches: 3 }).ok).toBe(false);
  });

  it("can have either limit switched off", () => {
    const glutted = absorptionFrom(open(50), NOW);
    expect(mayLaunch(glutted, { maxOpen: 0, minGraduationRate: 0, graceLaunches: 0 }).ok).toBe(true);
    expect(mayLaunch(glutted, { maxOpen: 0, minGraduationRate: 0.1, graceLaunches: 1 }).ok).toBe(false);
  });

  it("allows the first launch, when there is nothing to be glutted by", () => {
    expect(mayLaunch(absorptionFrom([], NOW), defaultAbsorptionLimits).ok).toBe(true);
  });

  it("ships limits that bind before the stock is absurd", () => {
    expect(defaultAbsorptionLimits.maxOpen).toBeGreaterThan(0);
    expect(defaultAbsorptionLimits.maxOpen).toBeLessThan(50);
    expect(mayLaunch(absorptionFrom(open(defaultAbsorptionLimits.maxOpen), NOW), defaultAbsorptionLimits).ok).toBe(false);
  });
});
