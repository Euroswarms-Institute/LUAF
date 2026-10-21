// node --test keeper/venture-regime.test.mjs
//
// The classifier is pure, so it is tested directly: a wrong reading here would
// either dump the treasury's holdings in a healthy market or hold them through
// a crash.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { appendObservation, classify, DEFAULT_POLICY, policyFor, spendable, valueChangeBps } from "./venture-regime.mjs";

const eth = (n) => BigInt(Math.round(n * 1e6)) * 10n ** 12n;

const A = "0x2Add000000000000000000000000000000000001";
const B = "0x2Add000000000000000000000000000000000002";

/** A steady series: same volume, same value for each held coin. */
const steady = (n = 5, volume = 10, value = 1) =>
  Array.from({ length: n }, (_, i) => ({
    at: 1_700_000_000 + i * 86_400,
    volumeWei: eth(volume).toString(),
    coins: { [A]: eth(value).toString(), [B]: eth(value).toString() },
  }));

const obs = (volume, value, coins) => ({
  at: 1_800_000_000,
  volumeWei: eth(volume).toString(),
  coins: coins ?? { [A]: eth(value).toString(), [B]: eth(value).toString() },
});

describe("classify", () => {
  it("holds the middle course until there is enough history to judge", () => {
    for (const n of [0, 1, 2]) {
      const { regime, reasons } = classify(steady(n), obs(1, 1));
      assert.equal(regime, "normal");
      assert.match(reasons[0], /prior observations/);
    }
    assert.equal(classify(undefined, obs(1, 1)).regime, "normal");
  });

  it("reads a steady market as normal", () => {
    assert.equal(classify(steady(), obs(10, 1)).regime, "normal");
  });

  it("calls a volume collapse a contraction", () => {
    const { regime, reasons } = classify(steady(), obs(4, 1)); // -60%
    assert.equal(regime, "contraction");
    assert.match(reasons.join(" "), /routed volume/);
  });

  it("calls a volume surge an expansion", () => {
    const { regime } = classify(steady(), obs(20, 1)); // +100%
    assert.equal(regime, "expansion");
  });

  it("leaves a move inside the threshold alone", () => {
    assert.equal(classify(steady(), obs(8, 1)).regime, "normal"); // -20%
    assert.equal(classify(steady(), obs(12, 1)).regime, "normal"); // +20%
  });

  it("calls falling realisable value a contraction even on healthy volume", () => {
    // Volume is up, but what the holdings would fetch per unit has halved.
    const { regime, reasons } = classify(steady(), obs(20, 0.5));
    assert.equal(regime, "contraction");
    assert.match(reasons.join(" "), /realisable value/);
  });

  it("is not moved by the basket changing", () => {
    // A new coin arrives at a different price and an old one is gone. Every
    // surviving coin held its value, so this is not a contraction — totalling
    // the treasury's quotes would have called it one.
    const latest = obs(10, 1, { [A]: eth(1).toString(), "0x2Add000000000000000000000000000000000009": eth(50).toString() });
    assert.equal(classify(steady(), latest).regime, "normal");
  });

  it("ignores the value signal when nothing is held", () => {
    assert.equal(classify(steady(), obs(10, 0, {})).regime, "normal");
  });

  it("ignores a coin with no history of its own", () => {
    const latest = obs(10, 1, { "0x2Add00000000000000000000000000000000000f": eth(0.01).toString() });
    assert.equal(classify(steady(), latest).regime, "normal");
    assert.equal(classify(steady(), latest).valueChangeBps, null);
  });

  it("survives a zero-volume median without dividing by it", () => {
    const dead = steady(5, 0, 1);
    const { regime } = classify(dead, obs(5, 1));
    assert.ok(["normal", "expansion"].includes(regime));
  });

  it("takes custom thresholds", () => {
    const strict = { thresholds: { volumeDropBps: 1000, volumeRiseBps: 1000, valueDropBps: 1000 } };
    assert.equal(classify(steady(), obs(8, 1), strict).regime, "contraction"); // -20%
  });
});

describe("policyFor", () => {
  it("always converts, and converts fastest in a contraction", () => {
    assert.ok(policyFor("contraction").sellBps > policyFor("normal").sellBps);
    assert.ok(policyFor("normal").sellBps > policyFor("expansion").sellBps);
    for (const r of ["expansion", "normal", "contraction"]) {
      assert.ok(policyFor(r).sellBps > 0, `${r} must still convert`);
      assert.ok(policyFor(r).sellBps <= 10_000);
    }
  });

  it("forbids the flywheel only in a contraction", () => {
    assert.equal(policyFor("contraction").flywheelAllowed, false);
    assert.equal(policyFor("normal").flywheelAllowed, true);
    assert.equal(policyFor("expansion").flywheelAllowed, true);
  });

  it("falls back to normal for an unknown regime", () => {
    assert.deepEqual(policyFor("nonsense"), DEFAULT_POLICY.normal);
  });
});

describe("spendable", () => {
  const floor = eth(5);

  it("spends nothing in a contraction, whatever the balance", () => {
    const { amount, why } = spendable(eth(100), eth(10), "contraction", floor);
    assert.equal(amount, 0n);
    assert.match(why, /contraction/);
  });

  it("pays what was wanted when it fits above the floor", () => {
    assert.equal(spendable(eth(100), eth(10), "normal", floor).amount, eth(10));
  });

  it("never dips into the reserve", () => {
    const { amount, why } = spendable(eth(6), eth(10), "normal", floor);
    assert.equal(amount, eth(1));
    assert.match(why, /reserve floor/);
  });

  it("spends nothing when the floor is the whole balance", () => {
    assert.equal(spendable(eth(5), eth(10), "normal", floor).amount, 0n);
    assert.equal(spendable(eth(1), eth(10), "normal", floor).amount, 0n);
  });

  it("is the acquisition reserve: a bust leaves the floor intact", () => {
    // Lenin ch. 3: depressions are when holdings are bought "for a mere song",
    // which needs means of payment still in hand.
    let balance = eth(20);
    for (let i = 0; i < 10; i++) {
      balance -= spendable(balance, eth(50), "contraction", floor).amount;
    }
    assert.equal(balance, eth(20));
  });
});

describe("appendObservation", () => {
  it("adds newest last and keeps the window bounded", () => {
    let series = [];
    for (let i = 0; i < 20; i++) series = appendObservation(series, { at: i }, 12);
    assert.equal(series.length, 12);
    assert.equal(series.at(-1).at, 19);
    assert.equal(series[0].at, 8);
  });

  it("tolerates a missing or corrupt series", () => {
    assert.equal(appendObservation(undefined, { at: 1 }).length, 1);
    assert.equal(appendObservation(null, { at: 1 }).length, 1);
  });
});

describe("valueChangeBps", () => {
  const A = "0x2Add000000000000000000000000000000000001";
  const B = "0x2Add000000000000000000000000000000000002";
  const e = (n) => (BigInt(Math.round(n * 1e6)) * 10n ** 12n).toString();
  const hist = (a, b) =>
    Array.from({ length: 4 }, () => ({ volumeWei: e(1), coins: { [A]: e(a), [B]: e(b) } }));

  it("compares each coin against its own history, then pools", () => {
    // A halves, B holds. The median of (-50%, 0%) is -25%.
    const bps = valueChangeBps(hist(1, 1), { coins: { [A]: e(0.5), [B]: e(1) } });
    assert.equal(bps, -2500);
  });

  it("is unmoved when every coin holds its value, at any price level", () => {
    assert.equal(valueChangeBps(hist(1, 100), { coins: { [A]: e(1), [B]: e(100) } }), 0);
  });

  it("reports null when nothing can be compared", () => {
    assert.equal(valueChangeBps(hist(1, 1), { coins: {} }), null);
    assert.equal(valueChangeBps([], { coins: { [A]: e(1) } }), null);
  });

  it("matches addresses case-insensitively", () => {
    const bps = valueChangeBps(hist(1, 1), { coins: { [A.toLowerCase()]: e(0.5), [B.toUpperCase()]: e(0.5) } });
    assert.equal(bps, -5000);
  });
});
