import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  chainIdForNetwork,
  endpointConfigFromEnv,
  EndpointConfigError,
  resolveEndpointConfig,
  writeBundle,
  type EndpointConfig,
} from "./endpoint.js";
import type { Product } from "../payload.js";

const product: Product = {
  name: "Arbitrage Finder",
  ticker: "ARBF",
  description: "Finds price gaps. Reports them.",
  code: "import os\nprint('ok')\n",
  language: "python",
  useCases: [{ title: "Scan", description: "Scan venues" }],
  requirements: [{ package: "requests", installation: "pip install requests" }],
  tags: "defi, arbitrage",
  raw: {},
};

const endpoint: EndpointConfig = {
  payTo: "0x1111111111111111111111111111111111111111",
  priceAtomic: "25000",
  network: "base",
  asset: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
  facilitator: "https://x402.org/facilitator",
};

function bundle(overrides: Partial<Parameters<typeof writeBundle>[0]> = {}) {
  const dir = mkdtempSync(join(tmpdir(), "luaf-bundle-"));
  const result = writeBundle({ product, dir, endpoint, ...overrides });
  const read = (name: string) => readFileSync(join(dir, name), "utf8");
  return { dir, result, read };
}

describe("the paid endpoint bundle", () => {
  it("writes everything needed to deploy it", () => {
    const { result } = bundle();
    expect(result.scriptName).toBe("agent.py");
    expect(result.files.sort()).toEqual(
      ["Dockerfile", "README.md", "agent.py", "fly.toml", "openapi.json", "requirements.txt", "server.mjs"].sort(),
    );
  });

  it("ships the agent's own source unchanged", () => {
    const { read } = bundle();
    expect(read("agent.py")).toBe(product.code);
    expect(read("requirements.txt").trim()).toBe("requests");
  });

  it("bakes the payment terms in, and leaves each one overridable", () => {
    const { read } = bundle();
    const server = read("server.mjs");
    expect(server).toContain(endpoint.payTo);
    expect(server).toContain('"25000"');
    expect(server).toContain(endpoint.asset);
    expect(server).toContain("https://x402.org/facilitator");
    for (const name of ["X402_PAY_TO", "X402_PRICE_ATOMIC", "X402_ASSET", "X402_NETWORK", "X402_FACILITATOR"]) {
      expect(server).toContain(`process.env.${name}`);
    }
    // No placeholder survives into the generated server.
    expect(server).not.toMatch(/__[A-Z_]+__/);
  });

  it("answers an unpaid call with 402 and verifies before it settles", () => {
    const server = bundle().read("server.mjs");
    expect(server).toContain("return json(res, 402, requirements(resource))");
    const verifyAt = server.indexOf('facilitatorCall("/verify"');
    const settleAt = server.indexOf('facilitatorCall("/settle"');
    const runAt = server.indexOf("await runAgent(");
    expect(verifyAt).toBeGreaterThan(-1);
    expect(settleAt).toBeGreaterThan(verifyAt);
    // The agent only runs once payment has been settled.
    expect(runAt).toBeGreaterThan(settleAt);
  });

  it("refuses to serve the agent when payment cannot be checked", () => {
    const server = bundle().read("server.mjs");
    expect(server).toContain('error: "payment cannot be verified"');
    expect(server).toContain('error: "payment cannot be settled"');
  });

  it("escapes the description into the server's string literal", () => {
    const quoted: Product = { ...product, name: 'Arb "Pro" \\ Finder' };
    const { read } = bundle({ product: quoted });
    const server = read("server.mjs");
    expect(server).toContain(JSON.stringify(`${quoted.name}: one run`));
    // The generated file must still parse as JavaScript.
    expect(() => new Function(server.replace(/^import .*$/gm, ""))).not.toThrow();
  });

  it("documents the price and the free-mode warning", () => {
    const readme = bundle().read("README.md");
    expect(readme).toContain("25000");
    expect(readme).toContain("X402_DISABLE");
    expect(readme).toMatch(/gives the agent away/);
  });

  it("describes /run as charged in the OpenAPI document", () => {
    const spec = JSON.parse(bundle().read("openapi.json")) as Record<string, any>;
    expect(spec.openapi).toBe("3.0.3");
    expect(spec.paths["/run"].post.responses["402"].description).toContain("25000");
    expect(spec.paths["/run"].post.parameters[0].name).toBe("X-PAYMENT");
    expect(spec.servers[0].url).toMatch(/^https:\/\//);
  });

  it("records the coin it was launched as, when there is one", () => {
    const { read } = bundle({ token: "0x2add000000000000000000000000000000000001", specId: "0xabc" });
    const readme = read("README.md");
    expect(readme).toContain("0x2add000000000000000000000000000000000001");
    expect(readme).toContain("0xabc");
  });

  it("runs a node agent with node", () => {
    const { result, read } = bundle({ product: { ...product, language: "node", code: "console.log(1)\n" } });
    expect(result.scriptName).toBe("agent.mjs");
    expect(read("server.mjs")).toContain('AGENT_RUNTIME || "node"');
  });

  it("does not run the container as root", () => {
    expect(bundle().read("Dockerfile")).toContain("USER node");
  });

  it("defaults to USDC on Base, paid where it is told", () => {
    const cfg = endpointConfigFromEnv("0x2222222222222222222222222222222222222222");
    expect(cfg.payTo).toBe("0x2222222222222222222222222222222222222222");
    expect(cfg.network).toBe("base");
    expect(cfg.priceAtomic).toBe("10000");
    expect(cfg.asset.toLowerCase()).toBe("0x833589fcd6edb6e08f4c7c32d4f71b54bda02913");
  });
});

describe("who the endpoint pays", () => {
  // Endpoint revenue is the only income here that is not a transfer between
  // traders, so addressing it wrongly is the costliest mistake available.
  const SPLITTER = "0x3333333333333333333333333333333333333333";
  const MINE = "0x4444444444444444444444444444444444444444";
  const LAUNCH_CHAIN = 4663;

  const clean = () => {
    for (const k of Object.keys(process.env)) if (k.startsWith("X402_")) delete process.env[k];
  };

  beforeEach(clean);
  afterEach(clean);

  it("maps the networks it knows and admits the ones it does not", () => {
    expect(chainIdForNetwork("base")).toBe(8453);
    expect(chainIdForNetwork("BASE")).toBe(8453);
    expect(chainIdForNetwork("base-sepolia")).toBe(84532);
    // Unknown must be null, not a guess: a wrong chain id here burns money.
    expect(chainIdForNetwork("robinhood")).toBeNull();
    expect(chainIdForNetwork("")).toBeNull();
  });

  it("refuses to pay the splitter on a chain it does not exist on", () => {
    // The default network is Base; the splitter is a contract on Robinhood.
    expect(() => resolveEndpointConfig({ splitter: SPLITTER, launchChainId: LAUNCH_CHAIN })).toThrow(
      EndpointConfigError,
    );
    expect(() => resolveEndpointConfig({ splitter: SPLITTER, launchChainId: LAUNCH_CHAIN })).toThrow(
      /has no private key, so paying it there would burn every payment/,
    );
  });

  it("names both the fix and the cause in the refusal", () => {
    try {
      resolveEndpointConfig({ splitter: SPLITTER, launchChainId: LAUNCH_CHAIN });
      expect.unreachable();
    } catch (e) {
      const msg = (e as Error).message;
      expect(msg).toContain(SPLITTER);
      expect(msg).toContain("chain 4663");
      expect(msg).toContain("chain 8453");
      expect(msg).toMatch(/X402_PAY_TO/);
    }
  });

  it("refuses an unrecognised network just as firmly", () => {
    process.env.X402_NETWORK = "robinhood";
    expect(() => resolveEndpointConfig({ splitter: SPLITTER, launchChainId: LAUNCH_CHAIN })).toThrow(
      /unrecognised network/,
    );
  });

  it("allows the splitter when the payment network is its own chain", () => {
    process.env.X402_NETWORK = "base";
    const cfg = resolveEndpointConfig({ splitter: SPLITTER, launchChainId: 8453 });
    expect(cfg.payTo).toBe(SPLITTER);
    expect(cfg.chainId).toBe(8453);
  });

  it("takes an explicit recipient on the payment network", () => {
    process.env.X402_PAY_TO = MINE;
    const cfg = resolveEndpointConfig({ splitter: SPLITTER, launchChainId: LAUNCH_CHAIN });
    expect(cfg.payTo).toBe(MINE);
    expect(cfg.network).toBe("base");
  });

  it("rejects a recipient that is not an address, or is the zero address", () => {
    process.env.X402_PAY_TO = "not-an-address";
    expect(() => resolveEndpointConfig({ splitter: SPLITTER, launchChainId: LAUNCH_CHAIN })).toThrow(/must be an address/);
    process.env.X402_PAY_TO = `0x${"0".repeat(40)}`;
    expect(() => resolveEndpointConfig({ splitter: SPLITTER, launchChainId: LAUNCH_CHAIN })).toThrow(/zero address/);
  });

  it("validates an override too, rather than trusting the caller", () => {
    const bad = { ...endpoint, payTo: SPLITTER, chainId: 8453 };
    expect(() =>
      resolveEndpointConfig({ splitter: SPLITTER, launchChainId: LAUNCH_CHAIN, override: bad }),
    ).toThrow(EndpointConfigError);
    const good = { ...endpoint, payTo: MINE, chainId: 8453 };
    expect(resolveEndpointConfig({ splitter: SPLITTER, launchChainId: LAUNCH_CHAIN, override: good }).payTo).toBe(MINE);
  });
});
