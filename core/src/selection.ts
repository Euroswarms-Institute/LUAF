/** Selection over launch specs.
 *
 *  The rules are ported from Abiogenesis, which exists to find out which
 *  selection pressures keep producing novelty. Four of its mechanisms carry
 *  over, and each one answers a question about launching coins:
 *
 *  - **Frequency-dependent payoff** (`--streams`, `world.rs` payout): a crowded
 *    stream pays little and an empty one a lot. Here the stream is the launch
 *    *format*, so a format's worth is its fee income divided by the number of
 *    live copies of it — ours and everyone else's. This is the one rule that
 *    makes saturation visible: a format that everyone has copied scores near
 *    zero however much gross revenue it once produced.
 *  - **The reaper** (`--reap-poor`): the poorest dies first. A format whose
 *    income per copy no longer covers what a launch costs is dropped.
 *  - **One-point crossover** (`--sex`): two survivors are cut at one point and
 *    spliced. In Abiogenesis this was the piece of the v5 bundle that mattered.
 *  - **An order-2 mutation model** (`--learned-mut`): mutations are drawn from
 *    what persisted rather than uniformly, conditioned on the two preceding
 *    fields.
 *
 *  Abiogenesis prices its streams per unit of *score* rather than per head.
 *  That is the right model when the resource is a fixed budget being shared;
 *  saturation is the other case — the question is what one more copy earns —
 *  so the divisor here is the copy count. */

import { formatKey, specId, validateSpec, type LaunchSpec, type TaxPolicy } from "./spec.js";

/** xorshift64, as in `src/rng.rs`: tiny, seedable and identical across runs,
 *  so a selection step can be tested. */
export class Rng {
  private s: bigint;
  constructor(seed: bigint = 0x9e3779b97f4a7c15n) {
    this.s = seed === 0n ? 1n : seed & MASK64;
  }
  next(): bigint {
    let x = this.s;
    x ^= (x << 13n) & MASK64;
    x ^= x >> 7n;
    x ^= (x << 17n) & MASK64;
    this.s = x & MASK64;
    return this.s;
  }
  below(n: number): number {
    if (n <= 1) return 0;
    return Number(this.next() % BigInt(n));
  }
  /** True with probability pct/100. */
  chance(pct: number): boolean {
    return this.below(100) < pct;
  }
  pick<T>(xs: readonly T[]): T {
    if (xs.length === 0) throw new Error("pick from an empty list");
    return xs[this.below(xs.length)]!;
  }
}

const MASK64 = (1n << 64n) - 1n;

/** One spec's standing. `copies` counts launches we made from it; `rivals` is
 *  how many copies of the same format exist elsewhere in the market, which the
 *  aggregator supplies once it is running and is 0 until then. */
export interface Member {
  spec: LaunchSpec;
  id: string;
  /** Coins launched from this spec. */
  copies: number;
  /** Fee income attributed to it, in wei of ETH. */
  feesEthWei: bigint;
  /** Launches that graduated, the only income that recurs. */
  graduated: number;
  /** Epoch this spec entered the population. */
  born: number;
  /** Epoch of its last launch. */
  lastLaunch: number;
  /** Mean turnover period of its coins, in seconds: launch to graduation, or
   *  launch to now while a coin is still on the curve. 0 means not measured. */
  meanTurnoverSecs?: number;
  /** Coins of its that completed a turnover. */
  turnoversComplete?: number;
  /** Unix seconds of its most recent launch, for the depreciation term. */
  lastLaunchAt?: number;
  parents?: string[];
}

export interface Population {
  epoch: number;
  members: Member[];
  /** Copies of each format seen elsewhere in the market, by format key. */
  rivals?: Record<string, number>;
}

export interface SelectionConfig {
  /** What one launch costs us: gas plus the keeper's share of running it. */
  costPerLaunchWei: bigint;
  /** Most members kept. The reaper trims to this after breeding. */
  carryingCapacity: number;
  /** Launches a spec gets before its fitness is taken seriously. */
  graceLaunches: number;
  /** Percentage of mutations drawn from the order-2 model rather than uniform. */
  learnedMutPct: number;
  /** Fields mutated per offspring, on average. */
  mutationRate: number;
  /** Ceiling on turnovers per year, so one fast coin cannot dominate on noise.
   *  Marx's arithmetic has no ceiling; a mean measured over one or two coins
   *  does, because a single lucky three-hour graduation would otherwise score
   *  thousands of times an honest one. */
  maxTurnoversPerYear: number;
  /** Half-life of past earnings, in days. Capital I ch. 15 on machinery: it
   *  "loses exchange-value, either by machines of the same sort being produced
   *  cheaper than it, or by better machines entering into competition with
   *  it." A format's takings from months ago say little about what it would
   *  earn today, so they are discounted rather than counted at face value. */
  incomeHalfLifeDays: number;
}

export const defaultSelectionConfig: SelectionConfig = {
  costPerLaunchWei: 2n * 10n ** 15n, // 0.002 ETH: launch gas plus keeper overhead
  carryingCapacity: 24,
  graceLaunches: 2,
  learnedMutPct: 25,
  mutationRate: 2,
  maxTurnoversPerYear: 365, // a turnover a day is as fast as this will credit
  incomeHalfLifeDays: 30,
};

/** Live copies of a spec's format: ours plus the market's. */
export function copiesOfFormat(pop: Population, spec: LaunchSpec): number {
  const key = formatKey(spec);
  let mine = 0;
  for (const m of pop.members) {
    if (formatKey(m.spec) === key) mine += m.copies;
  }
  return mine + (pop.rivals?.[key] ?? 0);
}

/** Seconds in a Julian year, the base the turnover rate is annualised against. */
const SECONDS_PER_YEAR = 31_557_600;
/** Fixed-point denominator for the time multipliers, so fitness stays bigint
 *  wei throughout and no amount is rounded through a float. */
const SCALE = 1_000_000n;

/** Turnovers per year, scaled by SCALE. Marx's `n`.
 *
 *  An unmeasured turnover returns exactly one turnover a year, so a format
 *  with no timing yet is neither rewarded nor punished for it. */
export function turnoverRate(meanTurnoverSecs: number | undefined, cfg: SelectionConfig): bigint {
  if (!meanTurnoverSecs || meanTurnoverSecs <= 0) return SCALE;
  const perYear = SECONDS_PER_YEAR / meanTurnoverSecs;
  const capped = Math.min(perYear, cfg.maxTurnoversPerYear);
  return BigInt(Math.max(1, Math.round(capped * Number(SCALE))));
}

/** What a format's past earnings are still worth, scaled by SCALE.
 *
 *  Exponential decay on the age of the last launch. Returns SCALE (no
 *  discount) when the age is unknown, so missing data never silently writes
 *  a format off. */
export function depreciation(
  lastLaunchAt: number | undefined,
  cfg: SelectionConfig,
  now = Math.floor(Date.now() / 1000),
): bigint {
  if (!lastLaunchAt || lastLaunchAt <= 0 || cfg.incomeHalfLifeDays <= 0) return SCALE;
  const ageDays = Math.max(0, (now - lastLaunchAt) / 86_400);
  const factor = Math.pow(0.5, ageDays / cfg.incomeHalfLifeDays);
  // Floored at one part in SCALE rather than zero: old earnings should be
  // worth almost nothing, not literally nothing. At zero every ageing format
  // ties and the ordering falls back to list position, which is no ordering
  // at all.
  return BigInt(Math.max(1, Math.round(factor * Number(SCALE))));
}

/** Fitness: what the next copy of this format is worth per year, in wei.
 *
 *  Three terms, each answering a different question the business actually
 *  faces:
 *
 *  1. **Income per live copy** — prices saturation in. A format with ten times
 *     the gross income and a hundred times the copies scores a tenth as much,
 *     because the question is what the next copy earns, not what the last
 *     hundred did.
 *  2. **Turnovers per year** — Capital II ch. 16: the annual rate of surplus
 *     value "is always equal to s'n", and two capitals alike in everything but
 *     turnover period differ by "900% in the annual rate". A format that
 *     graduates in three days and one that takes ninety are not equally good
 *     businesses, and scoring them alike was the loop's largest blind spot.
 *     A coin still on the curve has an open turnover, which counts against it:
 *     capital in circulation, in Capital II ch. 5's terms, "produces neither
 *     commodities nor surplus-value".
 *  3. **Depreciation** — earnings decay with age, because a format's takings
 *     from months ago say little about what it would earn now.
 *
 *  The result is a rate (wei per copy per year), not a total, so it stays
 *  comparable across formats of different ages. */
export function fitness(pop: Population, m: Member, cfg: SelectionConfig = defaultSelectionConfig, now?: number): bigint {
  const copies = copiesOfFormat(pop, m.spec);
  if (copies <= 0) return 0n;
  const perCopy = m.feesEthWei / BigInt(copies);
  if (perCopy === 0n) return 0n;
  const n = turnoverRate(m.meanTurnoverSecs, cfg);
  const d = depreciation(m.lastLaunchAt, cfg, now);
  return (perCopy * n * d) / (SCALE * SCALE);
}

/** Income per copy with no time terms, for reporting and for the cost test.
 *  `paysItsWay` compares against what a launch cost us, which is a cash
 *  amount, not a rate — annualising both sides would cancel out and comparing
 *  a rate to a cost would be a category error. */
export function incomePerCopy(pop: Population, m: Member): bigint {
  const copies = copiesOfFormat(pop, m.spec);
  if (copies <= 0) return 0n;
  return m.feesEthWei / BigInt(copies);
}

/** Has this spec earned its keep? Below cost it is reaped — the zero-rent
 *  condition: no surplus is left over what the launch cost. A spec still
 *  inside its grace period is spared, because one launch is not evidence. */
export function paysItsWay(pop: Population, m: Member, cfg: SelectionConfig): boolean {
  if (m.copies < cfg.graceLaunches) return true;
  return incomePerCopy(pop, m) >= cfg.costPerLaunchWei;
}

/** The reaper: poorest first, oldest among equals, down to capacity. Ported
 *  from `--reap-poor`, which orders the queue by (energy, age). */
export function reap(pop: Population, cfg: SelectionConfig, now?: number): Member[] {
  const keep = pop.members.filter((m) => paysItsWay(pop, m, cfg));
  const culled = keep.length ? keep : bestFew(pop, 2, cfg, now);
  if (culled.length <= cfg.carryingCapacity) return culled;
  return [...culled]
    .sort((a, b) => {
      const fa = fitness(pop, a, cfg, now);
      const fb = fitness(pop, b, cfg, now);
      if (fa !== fb) return fa > fb ? -1 : 1;
      return a.born - b.born;
    })
    .slice(0, cfg.carryingCapacity);
}

/** Never let the population empty: a world with nothing alive cannot evolve.
 *  Abiogenesis hit this as error catastrophe and it ended the run. */
function bestFew(pop: Population, n: number, cfg: SelectionConfig = defaultSelectionConfig, now?: number): Member[] {
  return [...pop.members]
    .sort((a, b) => {
      const fa = fitness(pop, a, cfg, now);
      const fb = fitness(pop, b, cfg, now);
      return fa === fb ? a.born - b.born : fa > fb ? -1 : 1;
    })
    .slice(0, n);
}

// ---------------------------------------------------------------------------
// The genome: an ordered field vector, so crossover and mutation stay generic
// ---------------------------------------------------------------------------

/** Mutable fields, in a fixed order. Crossover cuts this vector, so fields
 *  that belong together are adjacent: the four tax buckets sit side by side
 *  and tend to travel as a block. */
const GENES = [
  "profile",
  "runtime",
  "buyTaxBps",
  "sellTaxBps",
  "devBps",
  "dividendBps",
  "liquidityBps",
  "mmBps",
  "minHoldForDividends",
  "dividendMode",
  "firstBuyWei",
] as const;

type Gene = (typeof GENES)[number];

/** The values each gene may take. Anything outside these is a spec the
 *  factory would reject, so the search never proposes one. */
export interface GenePool {
  profiles: string[];
  runtimes: ("python" | "node")[];
  sideTaxBps: number[];
  bucketBps: number[];
  minHold: number[];
  dividendMode: number[];
  firstBuyWei: bigint[];
}

export const defaultGenePool: GenePool = {
  profiles: ["crypto", "x402_ai_payments", "data_products", "quant_research", "generic_profit"],
  runtimes: ["python"],
  // The factory caps each side at 400 bps.
  sideTaxBps: [0, 100, 200, 300, 400],
  // Bucket weights, renormalised to 10000 after any change.
  bucketBps: [0, 500, 1000, 1500, 2000, 2500, 3000, 4000, 5000, 6000],
  minHold: [0, 1_000, 10_000, 100_000],
  dividendMode: [0, 1],
  firstBuyWei: [0n, 10n ** 15n, 5n * 10n ** 15n, 10n ** 16n],
};

function geneValue(s: LaunchSpec, g: Gene): string {
  switch (g) {
    case "profile":
      return s.profile;
    case "runtime":
      return s.runtime;
    case "buyTaxBps":
      return String(s.tax.buyTaxBps);
    case "sellTaxBps":
      return String(s.tax.sellTaxBps);
    case "devBps":
      return String(s.tax.devBps);
    case "dividendBps":
      return String(s.tax.dividendBps);
    case "liquidityBps":
      return String(s.tax.liquidityBps);
    case "mmBps":
      return String(s.tax.mmBps);
    case "minHoldForDividends":
      return String(s.minHoldForDividends);
    case "dividendMode":
      return String(s.dividendMode);
    case "firstBuyWei":
      return s.firstBuyWei.toString();
  }
}

function withGene(s: LaunchSpec, g: Gene, v: string): LaunchSpec {
  const tax: TaxPolicy = { ...s.tax };
  const out: LaunchSpec = { ...s, tax };
  switch (g) {
    case "profile":
      out.profile = v;
      break;
    case "runtime":
      out.runtime = v === "node" ? "node" : "python";
      break;
    case "buyTaxBps":
      tax.buyTaxBps = Number(v);
      break;
    case "sellTaxBps":
      tax.sellTaxBps = Number(v);
      break;
    case "devBps":
      tax.devBps = Number(v);
      break;
    case "dividendBps":
      tax.dividendBps = Number(v);
      break;
    case "liquidityBps":
      tax.liquidityBps = Number(v);
      break;
    case "mmBps":
      tax.mmBps = Number(v);
      break;
    case "minHoldForDividends":
      out.minHoldForDividends = Number(v);
      break;
    case "dividendMode":
      out.dividendMode = Number(v);
      break;
    case "firstBuyWei":
      out.firstBuyWei = BigInt(v);
      break;
  }
  return out;
}

/** Make a spec legal again after a genetic operation: the four tax buckets
 *  must sum to 10000, and a dividend floor or ladder needs a dividend share.
 *  Abiogenesis bans illegal states in the substrate; here the factory does,
 *  so the repair happens before anything is proposed. */
export function repair(s: LaunchSpec): LaunchSpec {
  const tax = { ...s.tax };
  const out = { ...s, tax };
  const keys = ["devBps", "dividendBps", "liquidityBps", "mmBps"] as const;
  let sum = keys.reduce((n, k) => n + Math.max(0, tax[k]), 0);
  for (const k of keys) tax[k] = Math.max(0, tax[k]);
  if (sum === 0) {
    tax.devBps = 10_000;
    sum = 10_000;
  }
  let running = 0;
  for (let i = 0; i < keys.length - 1; i++) {
    const k = keys[i]!;
    tax[k] = Math.floor((tax[k] * 10_000) / sum);
    running += tax[k];
  }
  // The last bucket absorbs the rounding, so the sum is exact.
  tax[keys[keys.length - 1]!] = 10_000 - running;

  tax.buyTaxBps = Math.min(400, Math.max(0, tax.buyTaxBps));
  tax.sellTaxBps = Math.min(400, Math.max(0, tax.sellTaxBps));
  out.minHoldForDividends = Math.min(10_000_000, Math.max(0, out.minHoldForDividends));
  if (tax.dividendBps === 0) {
    out.minHoldForDividends = 0;
    out.dividendMode = 0;
  }
  if (out.dividendMode === 1 && out.minHoldForDividends === 0) out.dividendMode = 0;
  if (out.firstBuyWei < 0n) out.firstBuyWei = 0n;
  return out;
}

/** One-point crossover, the port of `world.rs` `crossover`: one random cut,
 *  everything after it taken from the partner. */
export function crossover(a: LaunchSpec, b: LaunchSpec, rng: Rng): LaunchSpec {
  const cut = 1 + rng.below(GENES.length - 1);
  let out = { ...a, tax: { ...a.tax } };
  for (let i = cut; i < GENES.length; i++) {
    const g = GENES[i]!;
    out = withGene(out, g, geneValue(b, g));
  }
  return repair(out);
}

/** An order-2 model over gene values, trained on the specs that persisted.
 *  The port of `mutant_op` and `train`: a mutation is drawn from what has
 *  worked in this context rather than uniformly, which is why Abiogenesis
 *  calls it learned mutation. */
export class MutationModel {
  /** context "prev2|prev1|gene" -> value -> count */
  private table = new Map<string, Map<string, number>>();

  private key(i: number, s: LaunchSpec): string {
    const g = GENES[i]!;
    const p1 = i >= 1 ? geneValue(s, GENES[i - 1]!) : "^";
    const p2 = i >= 2 ? geneValue(s, GENES[i - 2]!) : "^";
    return `${p2}|${p1}|${g}`;
  }

  /** Count the gene triples of specs worth imitating. */
  train(specs: LaunchSpec[]): void {
    for (const s of specs) {
      for (let i = 0; i < GENES.length; i++) {
        const k = this.key(i, s);
        const row = this.table.get(k) ?? new Map<string, number>();
        const v = geneValue(s, GENES[i]!);
        row.set(v, (row.get(v) ?? 0) + 1);
        this.table.set(k, row);
      }
    }
    // Halve every count once the table grows, so old evidence decays rather
    // than locking the model in. Abiogenesis halves at 2^22 triples.
    if (this.table.size > 4096) {
      for (const row of this.table.values()) {
        for (const [v, n] of row) {
          const half = Math.floor(n / 2);
          if (half === 0) row.delete(v);
          else row.set(v, half);
        }
      }
    }
  }

  /** A value for gene `i` drawn from the model, or undefined if it has never
   *  seen this context — the caller then mutates uniformly. */
  draw(i: number, s: LaunchSpec, rng: Rng): string | undefined {
    const row = this.table.get(this.key(i, s));
    if (!row || row.size === 0) return undefined;
    let total = 0;
    for (const n of row.values()) total += n;
    if (total <= 0) return undefined;
    let r = rng.below(total);
    for (const [v, n] of row) {
      r -= n;
      if (r < 0) return v;
    }
    return undefined;
  }
}

function uniformGene(g: Gene, pool: GenePool, rng: Rng): string {
  switch (g) {
    case "profile":
      return rng.pick(pool.profiles);
    case "runtime":
      return rng.pick(pool.runtimes);
    case "buyTaxBps":
    case "sellTaxBps":
      return String(rng.pick(pool.sideTaxBps));
    case "devBps":
    case "dividendBps":
    case "liquidityBps":
    case "mmBps":
      return String(rng.pick(pool.bucketBps));
    case "minHoldForDividends":
      return String(rng.pick(pool.minHold));
    case "dividendMode":
      return String(rng.pick(pool.dividendMode));
    case "firstBuyWei":
      return rng.pick(pool.firstBuyWei).toString();
  }
}

/** Mutate a few genes. `learnedMutPct` of them come from the order-2 model. */
export function mutate(
  s: LaunchSpec,
  rng: Rng,
  cfg: SelectionConfig,
  pool: GenePool,
  model?: MutationModel,
): LaunchSpec {
  let out = { ...s, tax: { ...s.tax } };
  const n = 1 + rng.below(Math.max(1, cfg.mutationRate));
  for (let k = 0; k < n; k++) {
    const i = rng.below(GENES.length);
    const g = GENES[i]!;
    const learned = model && rng.chance(cfg.learnedMutPct) ? model.draw(i, out, rng) : undefined;
    out = withGene(out, g, learned ?? uniformGene(g, pool, rng));
  }
  return repair(out);
}

export interface BreedOptions {
  cfg?: SelectionConfig;
  pool?: GenePool;
  rng?: Rng;
  /** Tenant and chain the offspring launch into; inherited, not evolved. */
  tenant: string;
  chainId: number;
}

/** One generation: reap, train on the survivors, then breed back to capacity.
 *
 *  Topics are deliberately left blank on the offspring. The brief is not part
 *  of the format — it is what the designer generates per launch — so breeding
 *  carries the terms forward and lets each launch find its own subject. */
export function breed(pop: Population, opts: BreedOptions): Population {
  const cfg = opts.cfg ?? defaultSelectionConfig;
  const pool = opts.pool ?? defaultGenePool;
  const rng = opts.rng ?? new Rng(BigInt(pop.epoch + 1) * 0x9e3779b97f4a7c15n);
  const epoch = pop.epoch + 1;

  const survivors = reap(pop, cfg);
  if (survivors.length === 0) return { ...pop, epoch, members: [] };

  const model = new MutationModel();
  model.train(survivors.map((m) => m.spec));

  const members = [...survivors];
  const want = Math.max(survivors.length, Math.min(cfg.carryingCapacity, survivors.length * 2));
  const ids = new Set(members.map((m) => m.id));
  let guard = 0;
  while (members.length < want && guard++ < want * 8) {
    const a = rng.pick(survivors);
    const b = rng.pick(survivors);
    const base = a.id === b.id ? a.spec : crossover(a.spec, b.spec, rng);
    let child = mutate(base, rng, cfg, pool, model);
    child = { ...child, tenant: opts.tenant, chainId: opts.chainId, topic: "", parents: [a.id, b.id] };
    try {
      validateSpec(child);
    } catch {
      continue; // repair should prevent this; skip rather than launch a reverting spec
    }
    const id = specId(child);
    if (ids.has(id)) continue;
    ids.add(id);
    members.push({
      spec: child,
      id,
      copies: 0,
      feesEthWei: 0n,
      graduated: 0,
      born: epoch,
      lastLaunch: 0,
      parents: [a.id, b.id],
    });
  }
  return { epoch, members, rivals: pop.rivals };
}

/** The spec to launch next: the fittest that is not yet saturated. Ties and
 *  untried specs go first, so a new format gets its grace launches before the
 *  incumbent's record decides everything. */
export function nextSpec(
  pop: Population,
  cfg: SelectionConfig = defaultSelectionConfig,
  now?: number,
): Member | undefined {
  if (pop.members.length === 0) return undefined;
  const untried = pop.members.filter((m) => m.copies < cfg.graceLaunches);
  const pool = untried.length ? untried : pop.members;
  return [...pool].sort((a, b) => {
    const fa = fitness(pop, a, cfg, now);
    const fb = fitness(pop, b, cfg, now);
    if (fa !== fb) return fa > fb ? -1 : 1;
    if (a.copies !== b.copies) return a.copies - b.copies;
    return a.born - b.born;
  })[0];
}

/** A founder population from one starting spec. */
export function seedPopulation(spec: LaunchSpec, count = 6, opts: Partial<BreedOptions> = {}): Population {
  const rng = opts.rng ?? new Rng();
  const pool = opts.pool ?? defaultGenePool;
  const cfg = opts.cfg ?? defaultSelectionConfig;
  const first = repair(spec);
  const members: Member[] = [
    { spec: first, id: specId(first), copies: 0, feesEthWei: 0n, graduated: 0, born: 0, lastLaunch: 0 },
  ];
  const ids = new Set(members.map((m) => m.id));
  let guard = 0;
  while (members.length < count && guard++ < count * 8) {
    const child = mutate(first, rng, cfg, pool);
    const id = specId(child);
    if (ids.has(id)) continue;
    ids.add(id);
    members.push({ spec: child, id, copies: 0, feesEthWei: 0n, graduated: 0, born: 0, lastLaunch: 0 });
  }
  return { epoch: 0, members };
}

// ---------------------------------------------------------------------------
// Persistence: a plain JSON file, so a population survives a restart
// ---------------------------------------------------------------------------

interface SerialMember extends Omit<Member, "spec" | "feesEthWei"> {
  spec: Omit<LaunchSpec, "firstBuyWei"> & { firstBuyWei: string };
  feesEthWei: string;
}

export function serializePopulation(pop: Population): string {
  const members: SerialMember[] = pop.members.map((m) => ({
    ...m,
    spec: { ...m.spec, firstBuyWei: m.spec.firstBuyWei.toString() },
    feesEthWei: m.feesEthWei.toString(),
  }));
  return JSON.stringify({ epoch: pop.epoch, rivals: pop.rivals ?? {}, members }, null, 2);
}

export function parsePopulation(text: string): Population {
  const raw = JSON.parse(text) as { epoch?: number; rivals?: Record<string, number>; members?: SerialMember[] };
  return {
    epoch: raw.epoch ?? 0,
    rivals: raw.rivals ?? {},
    members: (raw.members ?? []).map((m) => ({
      ...m,
      spec: { ...m.spec, firstBuyWei: BigInt(m.spec.firstBuyWei) } as LaunchSpec,
      feesEthWei: BigInt(m.feesEthWei),
    })),
  };
}

/** Fold measured income back into the population, so the next generation
 *  breeds on what happened rather than on what was hoped for. */
export function applyIncome(
  pop: Population,
  income: Map<
    string,
    {
      totalEthWei: bigint;
      tokens: unknown[];
      graduated: number;
      meanTurnoverSecs?: number;
      turnoversComplete?: number;
      lastLaunchAt?: number;
    }
  >,
): Population {
  return {
    ...pop,
    members: pop.members.map((m) => {
      const row = income.get(m.id.toLowerCase());
      if (!row) return m;
      return {
        ...m,
        feesEthWei: row.totalEthWei,
        copies: Math.max(m.copies, row.tokens.length),
        graduated: row.graduated,
        // Measured timing replaces whatever was there; a field the chain did
        // not report is left alone rather than zeroed, since zero reads as
        // "unmeasured" and would quietly drop the turnover term.
        meanTurnoverSecs: row.meanTurnoverSecs ?? m.meanTurnoverSecs,
        turnoversComplete: row.turnoversComplete ?? m.turnoversComplete,
        lastLaunchAt: row.lastLaunchAt ?? m.lastLaunchAt,
      };
    }),
  };
}
