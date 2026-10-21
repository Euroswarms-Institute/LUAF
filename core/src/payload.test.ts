import { describe, expect, it } from "vitest";

import { codeComplaints, parseProduct, PayloadError, slug, substantiveLines } from "./payload.js";
import { baseSpec, formatKey, specId, validateSpec, SpecError } from "./spec.js";

const good = {
  name: "Arbitrage Finder",
  ticker: "arbf",
  description: "Finds price gaps across venues and reports them.",
  agent: "import os\n\ndef main():\n    print('ok')\n\nmain()\n",
  language: "python",
  requirements: [{ package: "requests", installation: "pip install requests" }],
  useCases: [
    { title: "Scan", description: "Scan venues" },
    { title: "Report", description: "Report gaps" },
    { title: "Alert", description: "Alert on a threshold" },
  ],
  tags: "defi, arbitrage",
};

describe("parsing the designer's JSON", () => {
  it("accepts a clean object and uppercases the ticker", () => {
    const p = parseProduct(JSON.stringify(good));
    expect(p.name).toBe("Arbitrage Finder");
    expect(p.ticker).toBe("ARBF");
    expect(p.useCases).toHaveLength(3);
    expect(p.requirements[0]!.package).toBe("requests");
  });

  it("recovers the object from a code fence and from surrounding prose", () => {
    const body = JSON.stringify(good);
    expect(parseProduct("```json\n" + body + "\n```").ticker).toBe("ARBF");
    expect(parseProduct("Here is the agent:\n" + body + "\nDone.").ticker).toBe("ARBF");
  });

  it("tolerates a trailing comma", () => {
    const body = JSON.stringify(good).replace(/}$/, ',}');
    expect(parseProduct(body).ticker).toBe("ARBF");
  });

  it("is not fooled by braces inside strings", () => {
    const withBraces = { ...good, description: "uses {curly} braces and a \" quote" };
    const p = parseProduct("prose " + JSON.stringify(withBraces));
    expect(p.description).toContain("{curly}");
  });

  it("rejects empty output, non-objects and missing keys", () => {
    expect(() => parseProduct("")).toThrow(PayloadError);
    expect(() => parseProduct("no json here")).toThrow(/no JSON object/);
    expect(() => parseProduct("[1,2]")).toThrow(/no JSON object/);
    const { agent, ...noAgent } = good;
    void agent;
    expect(() => parseProduct(JSON.stringify(noAgent))).toThrow(/missing keys: agent/);
  });

  it("rejects a ticker a coin cannot carry, and empty code", () => {
    expect(() => parseProduct(JSON.stringify({ ...good, ticker: "this-is-too-long" }))).toThrow(/ticker/);
    expect(() => parseProduct(JSON.stringify({ ...good, ticker: "" }))).toThrow(/ticker/);
    expect(() => parseProduct(JSON.stringify({ ...good, agent: "   " }))).toThrow(/agent code is empty/);
    expect(() => parseProduct(JSON.stringify({ ...good, name: "" }))).toThrow(/name is empty/);
  });

  it("survives malformed use cases and requirements", () => {
    const p = parseProduct(JSON.stringify({ ...good, useCases: ["nope", { title: "T" }], requirements: "none" }));
    expect(p.useCases).toEqual([{ title: "T", description: "" }]);
    expect(p.requirements).toEqual([]);
  });
});

describe("counting and vetting code", () => {
  it("ignores blank lines and comments", () => {
    const code = "# a comment\n\nimport os\n\n# another\ndef f():\n    return 1\n";
    expect(substantiveLines(code)).toBe(3);
  });

  it("counts // comments for node code", () => {
    expect(substantiveLines("// note\nconst a = 1;\n", "node")).toBe(1);
  });

  it("complains about short code, placeholders and no structure", () => {
    const short = parseProduct(JSON.stringify(good));
    expect(codeComplaints(short, 400)[0]).toMatch(/substantive lines/);

    const stub = parseProduct(JSON.stringify({ ...good, agent: "def f():\n    raise NotImplementedError\n" }));
    expect(codeComplaints(stub, 1).some((c) => c.includes("NotImplementedError"))).toBe(true);

    const flat = parseProduct(JSON.stringify({ ...good, agent: "x = 1\ny = 2\n" }));
    expect(codeComplaints(flat, 1).some((c) => c.includes("no function or class"))).toBe(true);
  });

  it("passes code that is long enough and has structure", () => {
    const body = "def f():\n" + "    print('work')\n".repeat(20);
    const p = parseProduct(JSON.stringify({ ...good, agent: body }));
    expect(codeComplaints(p, 10)).toEqual([]);
  });
});

describe("slug", () => {
  it("is filesystem-safe and bounded", () => {
    expect(slug("Arbitrage Finder!")).toBe("arbitrage-finder");
    expect(slug("---")).toBe("agent");
    expect(slug("")).toBe("agent");
    expect(slug("a".repeat(100)).length).toBe(48);
  });
});

describe("the launch spec", () => {
  it("accepts the base spec", () => {
    expect(() => validateSpec(baseSpec({ tenant: "t" }))).not.toThrow();
  });

  it("rejects what the factory would reject", () => {
    const bad = baseSpec({ tenant: "t" });
    bad.tax = { ...bad.tax, devBps: 1 };
    expect(() => validateSpec(bad)).toThrow(/sum to 10000/);

    const taxed = baseSpec({ tenant: "t" });
    taxed.tax = { ...taxed.tax, buyTaxBps: 500 };
    expect(() => validateSpec(taxed)).toThrow(/buyTaxBps/);

    expect(() => validateSpec(baseSpec({ tenant: "t", mode: 0 }))).toThrow(/Open mode/);
    expect(() => validateSpec(baseSpec({ tenant: "" }))).toThrow(/tenant/);
    expect(() => validateSpec(baseSpec({ tenant: "t", profile: "  " }))).toThrow(/profile/);
    expect(() => validateSpec(baseSpec({ tenant: "t", minHoldForDividends: -1 }))).toThrow(SpecError);
    expect(() => validateSpec(baseSpec({ tenant: "t", dividendMode: 1, minHoldForDividends: 0 }))).toThrow(/ladder/);
  });

  it("hashes to a stable 32-byte id that covers every field", () => {
    const a = baseSpec({ tenant: "t", topic: "one" });
    expect(specId(a)).toMatch(/^0x[0-9a-f]{64}$/);
    expect(specId(a)).toBe(specId({ ...a }));
    expect(specId(baseSpec({ tenant: "t", topic: "two" }))).not.toBe(specId(a));
    expect(specId(baseSpec({ tenant: "t", topic: "one", firstBuyWei: 1n }))).not.toBe(specId(a));
  });

  it("treats the brief and the tenant as outside the format", () => {
    const a = baseSpec({ tenant: "one", topic: "a" });
    const b = baseSpec({ tenant: "two", topic: "b" });
    expect(formatKey(a)).toBe(formatKey(b));
    // The terms are inside it.
    const c = baseSpec({ tenant: "one", topic: "a", dividendMode: 1, minHoldForDividends: 10 });
    expect(formatKey(c)).not.toBe(formatKey(a));
  });
});
