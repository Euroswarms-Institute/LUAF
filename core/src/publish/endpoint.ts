/** The paid endpoint bundle: the agent as a service someone pays per call.
 *
 *  Ported from the Python pipeline's RapidAPI bundle, with the marketplace
 *  removed. That bundle listed on RapidAPI, which set the price, held the
 *  billing relationship and took its cut — the pipeline rented a storefront.
 *  Here the endpoint charges callers directly over x402: a request without
 *  payment gets 402 and the terms, a request with it is verified and settled
 *  through a facilitator, and the money goes to our own address.
 *
 *  This is the one part of the business that earns from outside the trader
 *  pool, so the bundle is written to be deployable on its own: a server, a
 *  container, a Fly config and an OpenAPI document. */

import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { slug, type Product } from "../payload.js";

export interface EndpointConfig {
  /** Address paid for each call.
   *
   *  It must be an address somebody controls **on the payment network**. That
   *  is not a pedantic point: a FeeSplitter is a contract, its address is
   *  derived from a deploy nonce on the chain it was deployed to, and no
   *  private key exists for it anywhere. Name it as the recipient on a
   *  different chain and every payment lands in an account with no code and no
   *  owner, which is a burn. `resolveEndpointConfig` enforces this. */
  payTo: string;
  /** Price per call, in the asset's smallest unit (USDC has 6 decimals). */
  priceAtomic: string;
  /** x402 network id, e.g. "base" or "base-sepolia". */
  network: string;
  /** The chain that network settles on, when it is one we can name. Null for
   *  a network this code does not recognise — unknown, not assumed equal. */
  chainId?: number | null;
  /** ERC-20 the price is denominated in. */
  asset: string;
  assetName?: string;
  assetVersion?: string;
  /** Facilitator that verifies and settles payments. */
  facilitator: string;
  /** Seconds a caller has to settle. */
  timeoutSeconds?: number;
}

export interface BundleRequest {
  product: Product;
  /** Directory the bundle is written into. */
  dir: string;
  endpoint: EndpointConfig;
  /** Public URL once deployed, for the OpenAPI server block. */
  baseUrl?: string;
  /** The coin this agent was launched as, recorded for provenance. */
  token?: string;
  specId?: string;
}

export interface BundleResult {
  dir: string;
  files: string[];
  /** The entrypoint of the agent inside the container. */
  scriptName: string;
}

const SERVER = String.raw`// Paid wrapper around the agent. One HTTP call runs the script once.
//
// GET  /health  free, for the platform's health check
// GET  /terms   the x402 payment requirements, so a caller can quote itself
// POST /run     charged. Body: {"task": "..."} or {"argv": ["--task", "..."]}
//
// Payment follows x402: an unpaid request is answered 402 with the terms, and
// a request carrying X-PAYMENT is verified and settled through a facilitator
// before the agent runs. The agent itself knows nothing about any of this.
import { createServer } from "node:http";
import { spawn } from "node:child_process";

const PORT = Number(process.env.PORT || 8080);
const SCRIPT = process.env.AGENT_SCRIPT || "__SCRIPT__";
const RUNTIME = process.env.AGENT_RUNTIME || "__RUNTIME__";
const RUN_TIMEOUT_MS = Number(process.env.RUN_TIMEOUT_MS || 120000);

// Payment terms. Every one of these is an environment variable so the price
// can move without rebuilding the image.
const PAY_TO = process.env.X402_PAY_TO || "__PAY_TO__";
const PRICE = process.env.X402_PRICE_ATOMIC || "__PRICE__";
const NETWORK = process.env.X402_NETWORK || "__NETWORK__";
const ASSET = process.env.X402_ASSET || "__ASSET__";
const ASSET_NAME = process.env.X402_ASSET_NAME || "__ASSET_NAME__";
const ASSET_VERSION = process.env.X402_ASSET_VERSION || "__ASSET_VERSION__";
const FACILITATOR = (process.env.X402_FACILITATOR || "__FACILITATOR__").replace(/\/+$/, "");
const TIMEOUT_SECONDS = Number(process.env.X402_TIMEOUT_SECONDS || __TIMEOUT__);
// Opt-out for local testing only. Never set this on a deployed instance: it
// serves the agent to anyone, for free.
const FREE = /^(1|true|yes)$/i.test(process.env.X402_DISABLE || "");

function requirements(resource) {
  return {
    x402Version: 1,
    accepts: [
      {
        scheme: "exact",
        network: NETWORK,
        maxAmountRequired: PRICE,
        resource,
        description: "__DESCRIPTION__",
        mimeType: "application/json",
        payTo: PAY_TO,
        maxTimeoutSeconds: TIMEOUT_SECONDS,
        asset: ASSET,
        extra: { name: ASSET_NAME, version: ASSET_VERSION },
      },
    ],
  };
}

function json(res, status, body, headers = {}) {
  const text = JSON.stringify(body);
  res.writeHead(status, {
    "content-type": "application/json",
    "content-length": Buffer.byteLength(text),
    ...headers,
  });
  res.end(text);
}

async function facilitatorCall(path, payload) {
  const res = await fetch(FACILITATOR + path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(20000),
  });
  if (!res.ok) {
    throw new Error("facilitator " + path + " returned " + res.status);
  }
  return res.json();
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data", (c) => {
      data += c;
      // A caller cannot make the server hold an unbounded body.
      if (data.length > 256 * 1024) {
        reject(new Error("body too large"));
        req.destroy();
      }
    });
    req.on("end", () => resolve(data));
    req.on("error", reject);
  });
}

// The agent takes its task through argv, as the runtime contract requires.
function argvFrom(body) {
  if (Array.isArray(body?.argv)) {
    return body.argv.filter((a) => typeof a === "string").slice(0, 32);
  }
  if (typeof body?.task === "string" && body.task.trim()) {
    return ["--task", body.task.slice(0, 8000)];
  }
  return [];
}

function runAgent(argv) {
  return new Promise((resolve) => {
    const args = RUNTIME === "python" ? ["-I", SCRIPT, ...argv] : [SCRIPT, ...argv];
    const cmd = RUNTIME === "python" ? "python3" : "node";
    const child = spawn(cmd, args, { stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    let timedOut = false;
    const cap = 1024 * 1024;
    child.stdout.on("data", (d) => {
      if (stdout.length < cap) stdout += d.toString();
    });
    child.stderr.on("data", (d) => {
      if (stderr.length < cap) stderr += d.toString();
    });
    const timer = setTimeout(() => {
      timedOut = true;
      child.kill("SIGKILL");
    }, RUN_TIMEOUT_MS);
    child.on("error", (e) => {
      clearTimeout(timer);
      resolve({ code: null, stdout, stderr: String(e.message), timedOut });
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve({ code, stdout, stderr, timedOut });
    });
  });
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url || "/", "http://localhost");
  const resource = "http://" + (req.headers.host || "localhost") + url.pathname;

  if (req.method === "GET" && url.pathname === "/health") {
    return json(res, 200, { ok: true });
  }
  if (req.method === "GET" && url.pathname === "/terms") {
    return json(res, 200, requirements(resource));
  }
  if (req.method !== "POST" || url.pathname !== "/run") {
    return json(res, 404, { error: "not found" });
  }

  const payment = req.headers["x-payment"];
  if (!FREE && !payment) {
    // The caller now knows the price, the asset and where to pay.
    return json(res, 402, requirements(resource));
  }

  let body = {};
  try {
    const text = await readBody(req);
    body = text ? JSON.parse(text) : {};
  } catch (e) {
    return json(res, 400, { error: String(e.message) });
  }

  let settlement = null;
  if (!FREE) {
    const payload = { x402Version: 1, paymentPayload: payment, paymentRequirements: requirements(resource).accepts[0] };
    try {
      const verdict = await facilitatorCall("/verify", payload);
      if (verdict && verdict.isValid === false) {
        return json(res, 402, { error: "payment invalid", reason: verdict.invalidReason ?? null, ...requirements(resource) });
      }
    } catch (e) {
      // The work has not been done yet, so refuse rather than serve unpaid.
      return json(res, 503, { error: "payment cannot be verified", detail: String(e.message) });
    }
    try {
      settlement = await facilitatorCall("/settle", payload);
    } catch (e) {
      return json(res, 503, { error: "payment cannot be settled", detail: String(e.message) });
    }
  }

  const out = await runAgent(argvFrom(body));
  const headers = settlement && settlement.transaction
    ? { "x-payment-response": Buffer.from(JSON.stringify(settlement)).toString("base64") }
    : {};
  if (out.timedOut) {
    return json(res, 504, { error: "the agent did not finish in time", stdout: out.stdout }, headers);
  }
  if (out.code !== 0) {
    return json(res, 502, { error: "the agent failed", exitCode: out.code, stderr: out.stderr.slice(-4000) }, headers);
  }
  return json(res, 200, { ok: true, output: out.stdout }, headers);
});

server.listen(PORT, () => console.log("listening on " + PORT));
`;

const DOCKERFILE = String.raw`# The agent and its paid wrapper in one image.
FROM node:22-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pip ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
# --break-system-packages: this image has no other Python application to break,
# and a venv would only add a layer of indirection to a single script.
RUN if [ -s requirements.txt ]; then pip3 install --no-cache-dir --break-system-packages -r requirements.txt; fi
COPY . ./

ENV PORT=8080
EXPOSE 8080
# Run as a non-root user: the wrapper executes generated code.
USER node
CMD ["node", "server.mjs"]
`;

function flyToml(app: string): string {
  return `# fly.toml — one machine is enough; each call is a short subprocess.
app = "${app}"
primary_region = "iad"

[build]

[http_service]
  internal_port = 8080
  force_https = true
  auto_stop_machines = "stop"
  auto_start_machines = true
  min_machines_running = 0

[[http_service.checks]]
  interval = "30s"
  timeout = "5s"
  grace_period = "10s"
  method = "GET"
  path = "/health"

[[vm]]
  cpu_kind = "shared"
  cpus = 1
  memory_mb = 512
`;
}

function openapi(product: Product, baseUrl: string, cfg: EndpointConfig): unknown {
  return {
    openapi: "3.0.3",
    info: {
      title: product.name,
      version: "1.0.0",
      description: `${product.description}\n\nPOST /run is charged per call over x402: an unpaid request is answered 402 with the payment terms.`,
    },
    servers: [{ url: baseUrl }],
    paths: {
      "/health": {
        get: {
          summary: "Liveness",
          responses: { "200": { description: "ok" } },
        },
      },
      "/terms": {
        get: {
          summary: "Payment terms for /run",
          responses: { "200": { description: "x402 payment requirements" } },
        },
      },
      "/run": {
        post: {
          summary: "Run the agent once",
          parameters: [
            {
              name: "X-PAYMENT",
              in: "header",
              required: true,
              schema: { type: "string" },
              description: "x402 payment payload. Omit it to receive the terms as a 402.",
            },
          ],
          requestBody: {
            required: false,
            content: {
              "application/json": {
                schema: {
                  type: "object",
                  properties: {
                    task: { type: "string", description: "What the agent should do on this call." },
                    argv: { type: "array", items: { type: "string" }, description: "Raw arguments, instead of task." },
                  },
                },
              },
            },
          },
          responses: {
            "200": {
              description: "The agent's output",
              content: {
                "application/json": {
                  schema: {
                    type: "object",
                    properties: { ok: { type: "boolean" }, output: { type: "string" } },
                  },
                },
              },
            },
            "402": { description: `Payment required: ${cfg.priceAtomic} of ${cfg.asset} on ${cfg.network}` },
            "502": { description: "The agent failed" },
            "504": { description: "The agent did not finish in time" },
          },
        },
      },
    },
  };
}

function readme(product: Product, req: BundleRequest, scriptName: string): string {
  const c = req.endpoint;
  return `# ${product.name} (${product.ticker})

${product.description}

Generated by the foundry. The agent is \`${scriptName}\`; \`server.mjs\` wraps it as a paid
endpoint and runs it once per call.

## Charging

\`POST /run\` is charged over x402. A request without \`X-PAYMENT\` is answered **402** with the
terms, which the caller reads and pays:

| Term | Value |
| --- | --- |
| Price | \`${c.priceAtomic}\` (smallest unit of the asset) |
| Asset | \`${c.asset}\` |
| Network | \`${c.network}\` |
| Paid to | \`${c.payTo}\` |
| Facilitator | ${c.facilitator} |

Each is an environment variable (\`X402_PRICE_ATOMIC\`, \`X402_ASSET\`, \`X402_NETWORK\`,
\`X402_PAY_TO\`, \`X402_FACILITATOR\`), so the price moves without a rebuild.

\`X402_DISABLE=1\` serves the agent for free. It is for local testing; setting it on a deployed
instance gives the agent away.

## Run it

\`\`\`sh
docker build -t ${slug(product.name)} .
docker run -p 8080:8080 -e X402_DISABLE=1 ${slug(product.name)}
curl -s localhost:8080/terms
curl -s -X POST localhost:8080/run -H 'content-type: application/json' -d '{"task":"hello"}'
\`\`\`

## Deploy

\`\`\`sh
fly launch --copy-config --no-deploy
fly secrets set X402_PAY_TO=${c.payTo} X402_FACILITATOR=${c.facilitator}
fly deploy
\`\`\`

## Credentials

The agent reads every credential from the environment. \`requirements.txt\` lists what it imports.
${product.requirements.length ? `\n${product.requirements.map((r) => `- \`${r.package}\` — ${r.installation}`).join("\n")}\n` : ""}
${req.token ? `\n## Provenance\n\nCoin: \`${req.token}\`${req.specId ? `\nSpec: \`${req.specId}\`` : ""}\n` : ""}`;
}

/** Write a deployable bundle for one designed agent. */
export function writeBundle(req: BundleRequest): BundleResult {
  const { product, dir, endpoint } = req;
  mkdirSync(dir, { recursive: true });
  const isPython = product.language !== "node" && product.language !== "javascript" && product.language !== "typescript";
  const scriptName = isPython ? "agent.py" : "agent.mjs";
  const app = slug(`${product.ticker}-${product.name}`);
  const baseUrl = req.baseUrl ?? `https://${app}.fly.dev`;

  const server = SERVER.replaceAll("__SCRIPT__", scriptName)
    .replaceAll("__RUNTIME__", isPython ? "python" : "node")
    .replaceAll("__PAY_TO__", endpoint.payTo)
    .replaceAll("__PRICE__", endpoint.priceAtomic)
    .replaceAll("__NETWORK__", endpoint.network)
    .replaceAll("__ASSET__", endpoint.asset)
    .replaceAll("__ASSET_NAME__", endpoint.assetName ?? "USDC")
    .replaceAll("__ASSET_VERSION__", endpoint.assetVersion ?? "2")
    .replaceAll("__FACILITATOR__", endpoint.facilitator)
    .replaceAll("__TIMEOUT__", String(endpoint.timeoutSeconds ?? 60))
    // JSON-escape the description: it lands inside a string literal.
    .replaceAll('"__DESCRIPTION__"', JSON.stringify(`${product.name}: one run`));

  const files: Record<string, string> = {
    [scriptName]: product.code,
    "server.mjs": server,
    Dockerfile: DOCKERFILE,
    "fly.toml": flyToml(app),
    "requirements.txt": product.requirements.map((r) => r.package).filter(Boolean).join("\n") + "\n",
    "openapi.json": JSON.stringify(openapi(product, baseUrl, endpoint), null, 2),
    "README.md": readme(product, req, scriptName),
  };
  for (const [name, body] of Object.entries(files)) {
    writeFileSync(join(dir, name), body, "utf8");
  }
  return { dir, files: Object.keys(files), scriptName };
}

/** x402 network names mapped to the chain they settle on.
 *
 *  Only networks whose chain id is not in doubt. A name that is not here
 *  resolves to null, meaning unknown — which is treated as "cannot prove the
 *  recipient exists there", not as "probably fine". */
export const X402_CHAIN_IDS: Record<string, number> = {
  base: 8453,
  "base-sepolia": 84532,
  ethereum: 1,
  "ethereum-sepolia": 11155111,
  polygon: 137,
  arbitrum: 42161,
  optimism: 10,
  avalanche: 43114,
};

export function chainIdForNetwork(network: string): number | null {
  return X402_CHAIN_IDS[network.trim().toLowerCase()] ?? null;
}

/** Endpoint settings from the environment. USDC on Base by default, through
 *  the public facilitator, because that is where x402 traffic actually is.
 *
 *  `payTo` is not defaulted. See `resolveEndpointConfig`. */
export function endpointConfigFromEnv(payTo: string): EndpointConfig {
  const network = process.env.X402_NETWORK?.trim() || "base";
  return {
    payTo,
    priceAtomic: process.env.X402_PRICE_ATOMIC?.trim() || "10000", // 0.01 USDC
    network,
    chainId: chainIdForNetwork(network),
    asset: process.env.X402_ASSET?.trim() || "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    assetName: process.env.X402_ASSET_NAME?.trim() || "USDC",
    assetVersion: process.env.X402_ASSET_VERSION?.trim() || "2",
    facilitator: process.env.X402_FACILITATOR?.trim() || "https://x402.org/facilitator",
    timeoutSeconds: Number(process.env.X402_TIMEOUT_SECONDS?.trim() || "60"),
  };
}

export class EndpointConfigError extends Error {}

/**
 * Work out who the endpoint pays, and refuse rather than guess.
 *
 * Endpoint revenue is the only income in this business that is not a transfer
 * between traders — Capital I ch. 5: "Circulation, or the exchange of
 * commodities, begets no value", and Capital III ch. 17 on merchant capital
 * creating "neither value nor surplus-value". That makes it the income worth
 * protecting most carefully, and the earlier default did the opposite: it
 * named the creator FeeSplitter, a contract on the *launch* chain, while the
 * payments settle on Base. No private key exists for a contract address, so
 * every payment would have landed in an ownerless account.
 *
 * The rule: the splitter may be the recipient only when the payment network
 * settles on the same chain it was deployed to — then it really is there and
 * the tenant's 90/10 terms hold on endpoint income too. Otherwise an explicit
 * `X402_PAY_TO` on the payment network is required.
 */
export function resolveEndpointConfig(opts: {
  /** The creator splitter and the chain it lives on. */
  splitter: string;
  launchChainId: number;
  /** Overrides everything below; used when a caller supplies its own. */
  override?: EndpointConfig;
}): EndpointConfig {
  if (opts.override) return validateEndpointConfig(opts.override, opts);
  const explicit = process.env.X402_PAY_TO?.trim();
  const cfg = endpointConfigFromEnv(explicit || opts.splitter);
  return validateEndpointConfig(cfg, opts);
}

function validateEndpointConfig(
  cfg: EndpointConfig,
  opts: { splitter: string; launchChainId: number },
): EndpointConfig {
  if (!/^0x[0-9a-fA-F]{40}$/.test(cfg.payTo)) {
    throw new EndpointConfigError(`X402_PAY_TO must be an address, got ${JSON.stringify(cfg.payTo)}`);
  }
  if (/^0x0{40}$/.test(cfg.payTo)) {
    throw new EndpointConfigError("the endpoint cannot be paid to the zero address");
  }
  const isSplitter = cfg.payTo.toLowerCase() === opts.splitter.toLowerCase();
  if (isSplitter && cfg.chainId !== opts.launchChainId) {
    const where = cfg.chainId === null ? `an unrecognised network (${cfg.network})` : `chain ${cfg.chainId}`;
    throw new EndpointConfigError(
      `the creator splitter ${cfg.payTo} is a contract on chain ${opts.launchChainId}, but payments settle ` +
        `on ${where}. A contract address has no private key, so paying it there would burn every payment. ` +
        `Set X402_PAY_TO to an address you control on ${cfg.network}, or point X402_NETWORK at chain ` +
        `${opts.launchChainId}.`,
    );
  }
  return cfg;
}
