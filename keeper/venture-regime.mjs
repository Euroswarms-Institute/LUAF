// Which way the market is going, and what the treasury should do about it.
//
// The treasury keeper sold a fixed share of its coin holdings every run, and
// the flywheel would spend up to half the keeper's ETH buying coins back. Both
// are policies for a rising market. Capital III ch. 30 describes the other
// case: "a crisis must obviously occur — a tremendous rush for means of
// payment — when credit suddenly ceases and only cash payments have validity",
// and in it "this fictitious money-capital is enormously reduced". Luxemburg
// puts the timing on it: "at the first symptom of the crisis, credit melts
// away." A launched coin is exactly that fictitious capital, so a contraction
// is when holdings must be converted fastest and when buying them back is
// worst.
//
// There is a second reason to hold ETH through a downturn. Lenin ch. 3:
// "during periods of depression, small and unsound businesses go out of
// existence, and the big banks acquire 'holdings' in them by buying them up
// for a mere song." A reserve is not idle capital; it is what makes a bust
// useful. So the flywheel may never spend below a floor.
//
// What this reads is deliberately only what is observable on chain: routed
// volume per epoch, and the ETH the treasury's coin holdings would actually
// fetch. No price feed, no oracle, no sentiment.

const MIN_HISTORY = 3;

/** The default policy per regime. Conversion is always on — Capital III ch. 30
 *  is about settling in means of payment — only the speed changes. */
export const DEFAULT_POLICY = {
  expansion: { sellBps: 2000, flywheelAllowed: true },
  normal: { sellBps: 2500, flywheelAllowed: true },
  contraction: { sellBps: 6000, flywheelAllowed: false },
};

/** How far a reading must move from the trailing median to count. */
export const DEFAULT_THRESHOLDS = {
  /** Routed volume down this many bps versus the median is a contraction. */
  volumeDropBps: 4000,
  /** Volume up this many bps versus the median is an expansion. */
  volumeRiseBps: 4000,
  /** The realisable ETH value of a coin falling this far is a contraction on
   *  its own, whatever volume did. Measured per coin, then taken at the
   *  median across coins. */
  valueDropBps: 3000,
};

function median(values) {
  if (values.length === 0) return 0n;
  const sorted = [...values].sort((a, b) => (a === b ? 0 : a < b ? -1 : 1));
  const mid = sorted.length >> 1;
  return sorted.length % 2 === 1 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2n;
}

/** Change from `base` to `now`, in basis points. Positive is a rise. */
function changeBps(now, base) {
  if (base === 0n) return now > 0n ? Number.POSITIVE_INFINITY : 0;
  return Number(((now - base) * 10_000n) / base);
}

/**
 * The median change in realisable value, measured per coin.
 *
 * Each coin is compared against its own trailing median and only then are the
 * changes pooled, which is the only construction that survives the basket
 * changing. Totalling the treasury's quotes instead would move whenever a coin
 * graduated or a fee arrived, and the detector would fire on bookkeeping rather
 * than on price. Summing raw token units across coins would be worse still:
 * a unit of one coin is not a unit of another, so the sum means nothing.
 *
 * Returns null when no coin appears in both the latest reading and the
 * history, which is the honest answer rather than zero.
 */
export function valueChangeBps(series, latest) {
  const history = new Map();
  for (const o of series) {
    for (const [coin, value] of Object.entries(o.coins ?? {})) {
      const v = BigInt(value);
      if (v <= 0n) continue;
      const key = coin.toLowerCase();
      history.set(key, [...(history.get(key) ?? []), v]);
    }
  }
  const changes = [];
  for (const [coin, value] of Object.entries(latest.coins ?? {})) {
    const now = BigInt(value);
    if (now <= 0n) continue;
    const past = history.get(coin.toLowerCase());
    // A coin with no history of its own says nothing about a trend.
    if (!past || past.length < MIN_HISTORY) continue;
    changes.push(changeBps(now, median(past)));
  }
  if (changes.length === 0) return null;
  changes.sort((a, b) => a - b);
  const mid = changes.length >> 1;
  return changes.length % 2 === 1 ? changes[mid] : (changes[mid - 1] + changes[mid]) / 2;
}

/**
 * Classify the regime from a trailing series plus the latest observation.
 *
 * An observation is `{ at, volumeWei, coins: { [address]: valuePerUnitWei } }`:
 * the routed volume of the window, and what one whole unit of each held coin
 * would currently fetch in ETH.
 *
 * Pure: no clock, no network. The caller supplies the series.
 */
export function classify(series, latest, opts = {}) {
  const th = { ...DEFAULT_THRESHOLDS, ...(opts.thresholds ?? {}) };
  const reasons = [];
  // Too little history to judge: hold the middle course rather than guess.
  if (!Array.isArray(series) || series.length < MIN_HISTORY) {
    return {
      regime: "normal",
      reasons: [`only ${series?.length ?? 0} prior observations; need ${MIN_HISTORY}`],
      volumeChangeBps: 0,
      valueChangeBps: null,
    };
  }

  const volMedian = median(series.map((o) => BigInt(o.volumeWei ?? 0)));
  const volNow = BigInt(latest.volumeWei ?? 0);
  const volBps = changeBps(volNow, volMedian);
  const valBps = valueChangeBps(series, latest);

  let regime = "normal";
  if (volBps <= -th.volumeDropBps) {
    regime = "contraction";
    reasons.push(`routed volume ${(volBps / 100).toFixed(1)}% against its trailing median`);
  }
  // Falling realisable value is a contraction by itself: the holdings are
  // worth less whatever the volume says.
  if (valBps !== null && valBps <= -th.valueDropBps) {
    regime = "contraction";
    reasons.push(`realisable value ${(valBps / 100).toFixed(1)}% at the median coin`);
  }
  if (regime === "normal" && volBps >= th.volumeRiseBps) {
    regime = "expansion";
    reasons.push(`routed volume +${(volBps / 100).toFixed(1)}% against its trailing median`);
  }
  if (reasons.length === 0) reasons.push("no reading past its threshold");
  return { regime, reasons, volumeChangeBps: volBps, valueChangeBps: valBps };
}

/** The policy for a regime: how fast to convert, and whether to spend. */
export function policyFor(regime, opts = {}) {
  const policy = { ...DEFAULT_POLICY, ...(opts.policy ?? {}) };
  return policy[regime] ?? policy.normal;
}

/**
 * What the flywheel may spend: never below the reserve floor, and nothing at
 * all in a contraction.
 *
 * The old rule was half the balance, which in a downturn spends the means of
 * payment on the asset that is falling.
 */
export function spendable(balanceWei, wanted, regime, reserveFloorWei, opts = {}) {
  const { flywheelAllowed } = policyFor(regime, opts);
  if (!flywheelAllowed) return { amount: 0n, why: `held: ${regime}` };
  const free = balanceWei > reserveFloorWei ? balanceWei - reserveFloorWei : 0n;
  if (free === 0n) return { amount: 0n, why: "held: the reserve floor is the whole balance" };
  if (wanted <= free) return { amount: wanted, why: "within the free balance" };
  return { amount: free, why: "capped by the reserve floor" };
}

/** Keep the series bounded, newest last. */
export function appendObservation(series, observation, keep = 12) {
  const next = [...(Array.isArray(series) ? series : []), observation];
  return next.slice(-keep);
}
