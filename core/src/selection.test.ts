import { describe, expect, it } from "vitest";

import {
  Rng,
  MutationModel,
  applyIncome,
  breed,
  copiesOfFormat,
  crossover,
  defaultSelectionConfig,
  fitness,
  mutate,
  nextSpec,
  parsePopulation,
  paysItsWay,
  reap,
  repair,
  seedPopulation,
  serializePopulation,
  defaultGenePool,
  type Member,
  type Population,
} from "./selection.js";
import { baseSpec, formatKey, specId, validateSpec, type LaunchSpec } from "./spec.js";

const cfg = { ...defaultSelectionConfig, graceLaunches: 0 };

function member(spec: LaunchSpec, copies: number, feesEthWei: bigint, born = 0): Member {
  return { spec, id: specId(spec), copies, feesEthWei, graduated: 0, born, lastLaunch: 0 };
}

function pop(members: Member[], rivals: Record<string, number> = {}): Population {
  return { epoch: 0, members, rivals };
}

describe("fitness divides income by live copies", () => {
  it("prices saturation: more copies of the same format is worth less per copy", () => {
    const spec = baseSpec({ tenant: "t" });
    const thin = pop([member(spec, 1, 1_000n)]);
    const crowded = pop([member(spec, 10, 1_000n)]);
    expect(fitness(thin, thin.members[0]!)).toBe(1_000n);
    expect(fitness(crowded, crowded.members[0]!)).toBe(100n);
  });

  it("counts rival copies from the market, not only our own", () => {
    const spec = baseSpec({ tenant: "t" });
    const key = formatKey(spec);
    const mine = pop([member(spec, 2, 1_000n)]);
    const contested = pop([member(spec, 2, 1_000n)], { [key]: 8 });
    expect(copiesOfFormat(mine, spec)).toBe(2);
    expect(copiesOfFormat(contested, spec)).toBe(10);
    expect(fitness(contested, contested.members[0]!)).toBe(100n);
  });

  it("pools copies of the same format held by different specs", () => {
    // Two specs with different briefs are the same format, so they compete.
    const a = baseSpec({ tenant: "t", topic: "one" });
    const b = baseSpec({ tenant: "t", topic: "two" });
    expect(formatKey(a)).toBe(formatKey(b));
    const p = pop([member(a, 3, 900n), member(b, 6, 0n)]);
    expect(copiesOfFormat(p, a)).toBe(9);
    expect(fitness(p, p.members[0]!)).toBe(100n);
  });

  it("is zero before anything has been launched", () => {
    const p = pop([member(baseSpec({ tenant: "t" }), 0, 0n)]);
    expect(fitness(p, p.members[0]!)).toBe(0n);
  });
});

describe("the reaper", () => {
  it("drops a spec earning less per copy than a launch costs", () => {
    const rich = baseSpec({ tenant: "t", profile: "crypto" });
    const poor = baseSpec({ tenant: "t", profile: "data_products" });
    const p = pop([member(rich, 1, cfg.costPerLaunchWei * 4n), member(poor, 10, cfg.costPerLaunchWei)]);
    expect(paysItsWay(p, p.members[0]!, cfg)).toBe(true);
    expect(paysItsWay(p, p.members[1]!, cfg)).toBe(false);
    expect(reap(p, cfg).map((m) => m.spec.profile)).toEqual(["crypto"]);
  });

  it("spares a spec still inside its grace period", () => {
    const withGrace = { ...defaultSelectionConfig, graceLaunches: 2 };
    const p = pop([member(baseSpec({ tenant: "t" }), 1, 0n)]);
    expect(paysItsWay(p, p.members[0]!, withGrace)).toBe(true);
    expect(reap(p, withGrace)).toHaveLength(1);
  });

  it("never empties the population, even when nothing pays", () => {
    const p = pop([
      member(baseSpec({ tenant: "t", profile: "crypto" }), 9, 1n),
      member(baseSpec({ tenant: "t", profile: "quant_research" }), 9, 2n),
    ]);
    const kept = reap(p, cfg);
    expect(kept.length).toBeGreaterThan(0);
    expect(kept.length).toBeLessThanOrEqual(2);
  });

  it("trims to capacity, richest first and oldest among equals", () => {
    const small = { ...cfg, carryingCapacity: 2 };
    const specs = ["crypto", "quant_research", "data_products"].map((profile, i) =>
      member(baseSpec({ tenant: "t", profile }), 1, BigInt((i + 1) * 10) * small.costPerLaunchWei, 10 - i),
    );
    const kept = reap(pop(specs), small);
    expect(kept).toHaveLength(2);
    expect(kept.map((m) => m.spec.profile)).toEqual(["data_products", "quant_research"]);
  });
});

describe("repair keeps a spec launchable", () => {
  it("renormalises the tax buckets to exactly 10000", () => {
    const broken = baseSpec({ tenant: "t" });
    broken.tax = { ...broken.tax, devBps: 3333, dividendBps: 3333, liquidityBps: 3333, mmBps: 3333 };
    const fixed = repair(broken);
    const t = fixed.tax;
    expect(t.devBps + t.dividendBps + t.liquidityBps + t.mmBps).toBe(10_000);
    expect(() => validateSpec(fixed)).not.toThrow();
  });

  it("gives everything to dev when every bucket is zero", () => {
    const broken = baseSpec({ tenant: "t" });
    broken.tax = { ...broken.tax, devBps: 0, dividendBps: 0, liquidityBps: 0, mmBps: 0 };
    const fixed = repair(broken);
    expect(fixed.tax.devBps).toBe(10_000);
    expect(() => validateSpec(fixed)).not.toThrow();
  });

  it("clears a dividend floor and ladder when no fee reaches holders", () => {
    const broken = baseSpec({ tenant: "t", minHoldForDividends: 1_000, dividendMode: 1 });
    broken.tax = { ...broken.tax, devBps: 10_000, dividendBps: 0, liquidityBps: 0, mmBps: 0 };
    const fixed = repair(broken);
    expect(fixed.minHoldForDividends).toBe(0);
    expect(fixed.dividendMode).toBe(0);
    expect(() => validateSpec(fixed)).not.toThrow();
  });

  it("clamps a side tax to the factory's 400 bps ceiling", () => {
    const broken = baseSpec({ tenant: "t" });
    broken.tax = { ...broken.tax, buyTaxBps: 9_000, sellTaxBps: 500 };
    const fixed = repair(broken);
    expect(fixed.tax.buyTaxBps).toBe(400);
    expect(fixed.tax.sellTaxBps).toBe(400);
  });

  it("drops a tiered ladder with no floor to pin it to", () => {
    const fixed = repair(baseSpec({ tenant: "t", dividendMode: 1, minHoldForDividends: 0 }));
    expect(fixed.dividendMode).toBe(0);
  });
});

describe("crossover and mutation", () => {
  it("is deterministic for a given seed", () => {
    const a = baseSpec({ tenant: "t", profile: "crypto" });
    const b = baseSpec({ tenant: "t", profile: "quant_research", dividendMode: 0, minHoldForDividends: 0 });
    b.tax = { buyTaxBps: 400, sellTaxBps: 400, devBps: 6000, dividendBps: 2000, liquidityBps: 1000, mmBps: 1000 };
    const one = crossover(a, b, new Rng(42n));
    const two = crossover(a, b, new Rng(42n));
    expect(one).toEqual(two);
  });

  it("takes something from each parent and stays launchable", () => {
    const a = baseSpec({ tenant: "t", profile: "crypto" });
    const b = baseSpec({ tenant: "t", profile: "quant_research" });
    b.tax = { buyTaxBps: 400, sellTaxBps: 400, devBps: 6000, dividendBps: 2000, liquidityBps: 1000, mmBps: 1000 };
    let sawB = false;
    for (let seed = 1; seed < 40; seed++) {
      const child = crossover(a, b, new Rng(BigInt(seed)));
      expect(() => validateSpec(child)).not.toThrow();
      if (child.tax.buyTaxBps === 400 || child.tax.devBps === 6000) sawB = true;
    }
    expect(sawB).toBe(true);
  });

  it("always produces a launchable spec, over many seeds", () => {
    const start = baseSpec({ tenant: "t" });
    for (let seed = 1; seed < 300; seed++) {
      const child = mutate(start, new Rng(BigInt(seed)), defaultSelectionConfig, defaultGenePool);
      expect(() => validateSpec(child)).not.toThrow();
    }
  });

  it("does change something", () => {
    const start = baseSpec({ tenant: "t" });
    const before = specId(start);
    const changed = [...Array(40).keys()].some(
      (i) => specId(mutate(start, new Rng(BigInt(i + 1)), defaultSelectionConfig, defaultGenePool)) !== before,
    );
    expect(changed).toBe(true);
  });

  it("draws from the order-2 model where it has seen the context", () => {
    const model = new MutationModel();
    const trained = baseSpec({ tenant: "t", profile: "x402_ai_payments" });
    trained.tax = { ...trained.tax, buyTaxBps: 400 };
    model.train([trained, trained, trained]);
    // Gene 0 is the profile, whose context is the start of the vector.
    const drawn = model.draw(0, trained, new Rng(7n));
    expect(drawn).toBe("x402_ai_payments");
    // An unseen context yields nothing, so the caller mutates uniformly.
    const other = baseSpec({ tenant: "t", profile: "crypto" });
    expect(new MutationModel().draw(3, other, new Rng(7n))).toBeUndefined();
  });
});

describe("breeding a generation", () => {
  it("keeps the earners, drops the rest and refills with new specs", () => {
    const rich = member(baseSpec({ tenant: "t", profile: "crypto" }), 1, cfg.costPerLaunchWei * 50n);
    const poor = member(baseSpec({ tenant: "t", profile: "data_products" }), 20, 1n);
    const next = breed(pop([rich, poor]), { cfg, tenant: "t", chainId: 46630, rng: new Rng(9n) });
    expect(next.epoch).toBe(1);
    expect(next.members.some((m) => m.id === rich.id)).toBe(true);
    expect(next.members.some((m) => m.id === poor.id)).toBe(false);
    expect(next.members.length).toBeGreaterThan(1);
    for (const m of next.members) expect(() => validateSpec(m.spec)).not.toThrow();
  });

  it("gives offspring the tenant and chain, and no inherited brief", () => {
    const seed = member(baseSpec({ tenant: "old", topic: "a brief", profile: "crypto" }), 1, cfg.costPerLaunchWei * 9n);
    const next = breed(pop([seed]), { cfg, tenant: "acme", chainId: 4663, rng: new Rng(3n) });
    const children = next.members.filter((m) => m.born === 1);
    expect(children.length).toBeGreaterThan(0);
    for (const c of children) {
      expect(c.spec.tenant).toBe("acme");
      expect(c.spec.chainId).toBe(4663);
      expect(c.spec.topic).toBe("");
      expect(c.parents).toHaveLength(2);
    }
  });

  it("never produces two members with the same id", () => {
    const next = breed(pop([member(baseSpec({ tenant: "t" }), 1, cfg.costPerLaunchWei * 9n)]), {
      cfg,
      tenant: "t",
      chainId: 1,
      rng: new Rng(11n),
    });
    expect(new Set(next.members.map((m) => m.id)).size).toBe(next.members.length);
  });
});

describe("choosing what to launch next", () => {
  it("prefers an untried spec while it is inside its grace period", () => {
    const withGrace = { ...defaultSelectionConfig, graceLaunches: 2 };
    const proven = member(baseSpec({ tenant: "t", profile: "crypto" }), 5, withGrace.costPerLaunchWei * 100n);
    const fresh = member(baseSpec({ tenant: "t", profile: "quant_research" }), 0, 0n);
    expect(nextSpec(pop([proven, fresh]), withGrace)?.id).toBe(fresh.id);
  });

  it("otherwise picks the fittest", () => {
    const a = member(baseSpec({ tenant: "t", profile: "crypto" }), 2, 100n);
    const b = member(baseSpec({ tenant: "t", profile: "quant_research" }), 2, 900n);
    expect(nextSpec(pop([a, b]), cfg)?.id).toBe(b.id);
  });

  it("returns nothing for an empty population", () => {
    expect(nextSpec(pop([]), cfg)).toBeUndefined();
  });
});

describe("persistence and feedback", () => {
  it("round-trips a population, bigints included", () => {
    const seeded = seedPopulation(baseSpec({ tenant: "t", firstBuyWei: 10n ** 16n }), 4);
    const back = parsePopulation(serializePopulation(seeded));
    expect(back.members).toHaveLength(seeded.members.length);
    expect(back.members[0]!.spec.firstBuyWei).toBe(seeded.members[0]!.spec.firstBuyWei);
    expect(typeof back.members[0]!.feesEthWei).toBe("bigint");
    expect(back.members[0]!.id).toBe(seeded.members[0]!.id);
  });

  it("folds measured income into the matching spec only", () => {
    const a = member(baseSpec({ tenant: "t", profile: "crypto" }), 1, 0n);
    const b = member(baseSpec({ tenant: "t", profile: "quant_research" }), 1, 0n);
    const updated = applyIncome(
      pop([a, b]),
      new Map([[a.id.toLowerCase(), { totalEthWei: 777n, tokens: [1, 2, 3], graduated: 2 }]]),
    );
    expect(updated.members[0]!.feesEthWei).toBe(777n);
    expect(updated.members[0]!.copies).toBe(3);
    expect(updated.members[0]!.graduated).toBe(2);
    expect(updated.members[1]!.feesEthWei).toBe(0n);
  });

  it("seeds a varied founder population from one spec", () => {
    const seeded = seedPopulation(baseSpec({ tenant: "t" }), 6);
    expect(seeded.members.length).toBeGreaterThan(1);
    expect(new Set(seeded.members.map((m) => m.id)).size).toBe(seeded.members.length);
    for (const m of seeded.members) expect(() => validateSpec(m.spec)).not.toThrow();
  });
});

describe("Rng", () => {
  it("is reproducible and never returns the same value twice in a row", () => {
    const a = new Rng(5n);
    const b = new Rng(5n);
    const first = [a.next(), a.next(), a.next()];
    expect([b.next(), b.next(), b.next()]).toEqual(first);
    expect(new Set(first).size).toBe(3);
  });

  it("stays inside the requested bound", () => {
    const r = new Rng(1n);
    for (let i = 0; i < 500; i++) {
      const v = r.below(7);
      expect(v).toBeGreaterThanOrEqual(0);
      expect(v).toBeLessThan(7);
    }
    expect(r.below(1)).toBe(0);
    expect(r.below(0)).toBe(0);
  });
});

describe("the untradeable zero-tax format", () => {
  it("repair lifts 0/0 to something the factory's hook can trade", () => {
    const s = repair(baseSpec({ tenant: "t", tax: { buyTaxBps: 0, sellTaxBps: 0, devBps: 2500, dividendBps: 2500, liquidityBps: 2500, mmBps: 2500 } }));
    expect(s.tax.buyTaxBps + s.tax.sellTaxBps).toBeGreaterThan(0);
    expect(() => validateSpec(s)).not.toThrow();
  });

  it("validateSpec refuses 0/0, because the coin cannot be traded for 15s after graduation", () => {
    const s = baseSpec({ tenant: "t", tax: { buyTaxBps: 0, sellTaxBps: 0, devBps: 2500, dividendBps: 2500, liquidityBps: 2500, mmBps: 2500 } });
    expect(() => validateSpec(s)).toThrow(/cannot both be 0/);
  });

  it("one side at zero is fine: the sniper premium is not the only founder fee", () => {
    for (const tax of [
      { buyTaxBps: 0, sellTaxBps: 300, devBps: 2500, dividendBps: 2500, liquidityBps: 2500, mmBps: 2500 },
      { buyTaxBps: 300, sellTaxBps: 0, devBps: 2500, dividendBps: 2500, liquidityBps: 2500, mmBps: 2500 },
    ]) {
      expect(() => validateSpec(baseSpec({ tenant: "t", tax }))).not.toThrow();
    }
  });

  it("no mutation of a valid spec can reach 0/0", () => {
    let spec = baseSpec({ tenant: "t" });
    for (let i = 0; i < 2000; i++) {
      spec = mutate(spec, new Rng(BigInt(i + 1)), defaultSelectionConfig, defaultGenePool);
      expect(spec.tax.buyTaxBps + spec.tax.sellTaxBps).toBeGreaterThan(0);
    }
  });
});
