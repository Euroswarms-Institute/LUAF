import { describe, expect, it } from "vitest";

import { concentration, formatAtomic, type EndpointIncome } from "./endpointIncome.js";

const base: EndpointIncome = {
  payTo: "0x4444444444444444444444444444444444444444",
  asset: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
  chainId: 8453,
  totalAtomic: 0n,
  payments: 0,
  uniquePayers: 0,
  largestPayerAtomic: 0n,
  fromBlock: 0n,
  toBlock: 100n,
};

const income = (over: Partial<EndpointIncome>): EndpointIncome => ({ ...base, ...over });

describe("is the endpoint revenue a market or an echo", () => {
  it("says nothing has come in yet, rather than reporting a share of zero", () => {
    const v = concentration(base);
    expect(v.topPayerShare).toBeNull();
    expect(v.isMarket).toBe(false);
    expect(v.note).toMatch(/no endpoint revenue yet/);
  });

  it("refuses to call a handful of payers demand", () => {
    // Plenty of money, four addresses. Capital I ch. 5: the class as a whole
    // cannot over-reach itself, and neither can four wallets.
    const v = concentration(income({ totalAtomic: 1_000_000n, payments: 40, uniquePayers: 4, largestPayerAtomic: 260_000n }));
    expect(v.isMarket).toBe(false);
    expect(v.note).toMatch(/too few to call this demand/);
  });

  it("refuses when one payer is most of the revenue", () => {
    const v = concentration(income({ totalAtomic: 1_000_000n, payments: 50, uniquePayers: 20, largestPayerAtomic: 900_000n }));
    expect(v.isMarket).toBe(false);
    expect(v.topPayerShare).toBeCloseTo(0.9, 6);
    expect(v.note).toMatch(/a customer, not a market/);
  });

  it("accepts spread revenue from enough payers", () => {
    const v = concentration(income({ totalAtomic: 1_000_000n, payments: 120, uniquePayers: 37, largestPayerAtomic: 150_000n }));
    expect(v.isMarket).toBe(true);
    expect(v.topPayerShare).toBeCloseTo(0.15, 6);
    expect(v.note).toMatch(/37 payers/);
  });

  it("takes stricter thresholds", () => {
    const row = income({ totalAtomic: 1_000_000n, payments: 60, uniquePayers: 6, largestPayerAtomic: 200_000n });
    expect(concentration(row).isMarket).toBe(true);
    expect(concentration(row, { minPayers: 20 }).isMarket).toBe(false);
    expect(concentration(row, { maxTopShare: 0.1 }).isMarket).toBe(false);
  });

  it("reports the share even when it rejects, so the number is visible", () => {
    const v = concentration(income({ totalAtomic: 100n, payments: 1, uniquePayers: 1, largestPayerAtomic: 100n }));
    expect(v.topPayerShare).toBe(1);
  });
});

describe("formatting an atomic amount", () => {
  it("renders USDC's six decimals", () => {
    expect(formatAtomic(10_000n)).toBe("0.01");
    expect(formatAtomic(1_000_000n)).toBe("1");
    expect(formatAtomic(1_500_000n)).toBe("1.5");
    expect(formatAtomic(0n)).toBe("0");
  });

  it("takes another asset's decimals", () => {
    expect(formatAtomic(10n ** 18n, 18)).toBe("1");
    expect(formatAtomic(10n ** 17n, 18)).toBe("0.1");
  });

  it("does not round a large amount through a float", () => {
    expect(formatAtomic(123_456_789_012_345_678_901n, 18)).toBe("123.456789012345678901");
  });
});
