/** Validation: prove the designed agent actually runs before anything is
 *  launched on its behalf.
 *
 *  A port of `run_agent_code_validation`. Two deliberate changes from the
 *  Python pipeline:
 *
 *  1. The agent runs with a stripped environment. The old pipeline handed the
 *     generated script the operator's whole environment, including the keys and
 *     the wallet. A design that has never been reviewed should not be able to
 *     spend or exfiltrate anything, so only an allowlist is passed through and
 *     the key material is never in it. Code that needs a credential is supposed
 *     to fail clearly, which is exactly what the runtime contract asks for.
 *  2. Dependencies install into one dedicated virtualenv under the run
 *     directory, not the operator's interpreter. */

import { spawn } from "node:child_process";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

export class ValidationError extends Error {}

export interface ValidateRequest {
  /** The agent's source. */
  code: string;
  language: string;
  /** Directory to write the script and the venv into. */
  dir: string;
  timeoutMs: number;
  /** Attempts, each one after an automatic dependency install. */
  maxInstalls?: number;
  /** Extra env names to pass through to the agent, beyond the safe defaults. */
  passEnv?: string[];
}

export interface ValidateResult {
  ok: boolean;
  /** What to tell the designer when it failed. */
  complaints: string[];
  stdout: string;
  stderr: string;
  installed: string[];
  exitCode: number | null;
  scriptPath: string;
  timedOut: boolean;
}

/** Variables a generated agent may legitimately need and that carry no
 *  authority. Everything else, above all the operator's keys, is withheld. */
const SAFE_ENV = ["PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"];

/** Names that must never reach generated code, even if a caller allowlists
 *  them: a leaked operator key costs real money. */
const NEVER_PASS = /KEY|SECRET|TOKEN|MNEMONIC|SEED|PASSWORD|CREDENTIAL|PRIVATE/i;

function childEnv(passEnv: string[] = []): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {};
  for (const name of [...SAFE_ENV, ...passEnv]) {
    if (NEVER_PASS.test(name)) continue;
    const v = process.env[name];
    if (v !== undefined) env[name] = v;
  }
  // Unbuffered output, so a timeout still yields the logs written so far.
  env.PYTHONUNBUFFERED = "1";
  return env;
}

interface RunOutcome {
  code: number | null;
  stdout: string;
  stderr: string;
  timedOut: boolean;
}

function run(cmd: string, args: string[], opts: { cwd: string; timeoutMs: number; env: NodeJS.ProcessEnv }): Promise<RunOutcome> {
  return new Promise((resolve) => {
    const child = spawn(cmd, args, { cwd: opts.cwd, env: opts.env, stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    let timedOut = false;
    const cap = 200_000;
    child.stdout.on("data", (d: Buffer) => {
      if (stdout.length < cap) stdout += d.toString();
    });
    child.stderr.on("data", (d: Buffer) => {
      if (stderr.length < cap) stderr += d.toString();
    });
    const timer = setTimeout(() => {
      timedOut = true;
      child.kill("SIGKILL");
    }, opts.timeoutMs);
    child.on("error", (e) => {
      clearTimeout(timer);
      resolve({ code: null, stdout, stderr: `${stderr}\n${e.message}`, timedOut });
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve({ code, stdout, stderr, timedOut });
    });
  });
}

/** The module name from a Python import failure, if that is what went wrong. */
function missingPythonModule(stderr: string): string | undefined {
  const m = /ModuleNotFoundError: No module named ['"]([A-Za-z0-9_.\-]+)['"]/.exec(stderr);
  const name = m?.[1]?.split(".")[0];
  // A relative import failure is a bug in the generated code, not a dependency.
  return name && !name.startsWith("_") ? name : undefined;
}

function missingNodeModule(stderr: string): string | undefined {
  const m = /Cannot find (?:module|package) ['"]([^'"]+)['"]/.exec(stderr);
  const raw = m?.[1];
  if (!raw || raw.startsWith(".") || raw.startsWith("/")) return undefined;
  const parts = raw.split("/");
  return raw.startsWith("@") ? parts.slice(0, 2).join("/") : parts[0];
}

async function pythonInterpreter(dir: string, timeoutMs: number): Promise<string> {
  const venv = join(dir, ".venv");
  const bin = join(venv, "bin", "python");
  if (existsSync(bin)) return bin;
  const made = await run("python3", ["-m", "venv", venv], { cwd: dir, timeoutMs, env: childEnv() });
  if (made.code !== 0 || !existsSync(bin)) {
    // No venv module available: fall back to the interpreter on PATH. The
    // install step then needs --user or an already-satisfied dependency.
    return "python3";
  }
  return bin;
}

/** Design, then prove it runs. Dependencies named by the failure are installed
 *  and the script is retried, as the Python pipeline did. */
export async function validate(req: ValidateRequest): Promise<ValidateResult> {
  const { dir, timeoutMs } = req;
  mkdirSync(dir, { recursive: true });
  const isPython = req.language !== "node" && req.language !== "javascript" && req.language !== "typescript";
  const scriptPath = join(dir, isPython ? "agent.py" : "agent.mjs");
  writeFileSync(scriptPath, req.code, "utf8");

  const env = childEnv(req.passEnv);
  const maxInstalls = req.maxInstalls ?? 4;
  const installed: string[] = [];
  let last: RunOutcome = { code: null, stdout: "", stderr: "", timedOut: false };

  for (let attempt = 0; attempt <= maxInstalls; attempt++) {
    if (isPython) {
      const python = await pythonInterpreter(dir, Math.min(timeoutMs, 120_000));
      // -I isolates the run: no user site-packages, no script-directory
      // imports, so a stray module beside the script cannot shadow the real
      // one. os.environ still carries what childEnv allowed.
      last = await run(python, ["-I", scriptPath], { cwd: dir, timeoutMs, env });
    } else {
      last = await run("node", [scriptPath], { cwd: dir, timeoutMs, env });
    }
    if (last.code === 0) {
      return { ok: true, complaints: [], stdout: last.stdout, stderr: last.stderr, installed, exitCode: 0, scriptPath, timedOut: false };
    }
    if (last.timedOut) break;
    if (attempt === maxInstalls) break;

    const missing = isPython ? missingPythonModule(last.stderr) : missingNodeModule(last.stderr);
    if (!missing || installed.includes(missing)) break;
    const ok = isPython
      ? await (async () => {
          const python = await pythonInterpreter(dir, Math.min(timeoutMs, 120_000));
          const r = await run(python, ["-m", "pip", "install", "--quiet", "--disable-pip-version-check", missing], {
            cwd: dir,
            timeoutMs: Math.min(timeoutMs, 300_000),
            env,
          });
          return r.code === 0;
        })()
      : (await run("npm", ["install", "--no-audit", "--no-fund", "--silent", missing], { cwd: dir, timeoutMs: Math.min(timeoutMs, 300_000), env })).code === 0;
    if (!ok) {
      return {
        ok: false,
        complaints: [`the dependency ${JSON.stringify(missing)} could not be installed; use a package that exists, or avoid it`],
        stdout: last.stdout,
        stderr: last.stderr,
        installed,
        exitCode: last.code,
        scriptPath,
        timedOut: false,
      };
    }
    installed.push(missing);
  }

  return {
    ok: false,
    complaints: complaintsFor(last, timeoutMs),
    stdout: last.stdout,
    stderr: last.stderr,
    installed,
    exitCode: last.code,
    scriptPath,
    timedOut: last.timedOut,
  };
}

/** Turn a failed run into something the designer can act on. */
function complaintsFor(outcome: RunOutcome, timeoutMs: number): string[] {
  if (outcome.timedOut) {
    return [
      `the agent did not exit within ${Math.round(timeoutMs / 1000)}s. It must do one unit of work and ` +
        `exit; remove any default loop or long sleep.`,
    ];
  }
  const tail = outcome.stderr.trim().split("\n").slice(-25).join("\n");
  return [`the agent exited with status ${outcome.code}. Fix the cause:\n${tail}`];
}
