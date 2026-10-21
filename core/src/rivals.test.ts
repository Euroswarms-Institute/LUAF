import { describe, expect, it } from "vitest";

import { rivalsForSpecs, type SaturationResponse } from "./rivals.js";
import { baseSpec, formatKey } from "./spec.js";
import { copiesOfFormat, fitness, type Member, type Population } from "./selection.js";
import { specId } from "./spec.js";

const report: SaturationResponse = {
  windowDays: 7,
  since: 0,
  rivalsByChain: { "4663": 100, "8453": 4 },
};

describe("turning per-chain saturation into per-format rivals", () => {
  it("subtracts our own copies, so a launch is not counted twice", () => {
    const spec = baseSpec({ tenant: "t", chainId: 4663 });
    const rivals = rivalsForSpecs(report, [{ spec, copies: 10 }]);
    // 100 launched on the chain, 10 of them ours.
    expect(rivals[formatKey(spec)]).toBe(90);
  });

  it("pools our copies across every spec on the same chain", () => {
    const a = baseSpec({ tenant: "t", chainId: 8453, profile: "crypto" });
    const b = baseSpec({ tenant: "t", chainId: 8453, profile: "quant_research" });
    const rivals = rivalsForSpecs(report, [
      { spec: a, copies: 1 },
      { spec: b, copies: 2 },
    ]);
    // 4 on the chain, 3 of them ours, so 1 rival for each of our formats.
    expect(rivals[formatKey(a)]).toBe(1);
    expect(rivals[formatKey(b)]).toBe(1);
  });

  it("never goes negative when we launched more than the window saw", () => {
    const spec = baseSpec({ tenant: "t", chainId: 8453 });
    expect(rivalsForSpecs(report, [{ spec, copies: 999 }])[formatKey(spec)]).toBe(0);
  });

  it("reports no rivals for a chain the aggregator does not cover", () => {
    const spec = baseSpec({ tenant: "t", chainId: 1 });
    expect(rivalsForSpecs(report, [{ spec, copies: 0 }])[formatKey(spec)]).toBe(0);
  });

  it("feeds straight into fitness, and crowds it down", () => {
    const spec = baseSpec({ tenant: "t", chainId: 4663 });
    const m: Member = { spec, id: specId(spec), copies: 10, feesEthWei: 1_000_000n, graduated: 0, born: 0, lastLaunch: 0 };
    const alone: Population = { epoch: 0, members: [m] };
    const contested: Population = { epoch: 0, members: [m], rivals: rivalsForSpecs(report, [{ spec, copies: 10 }]) };
    expect(copiesOfFormat(alone, spec)).toBe(10);
    expect(copiesOfFormat(contested, spec)).toBe(100);
    expect(fitness(alone, m)).toBe(100_000n);
    // The same income, priced against the whole chain's launches.
    expect(fitness(contested, m)).toBe(10_000n);
  });
});
