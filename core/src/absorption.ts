/** Whether the market is still taking what we produce.
 *
 *  The loop had two limits on how much it launched: the reaper, which culls a
 *  spec that does not pay, and `maxLaunches`, a fixed number. Neither asks the
 *  question Engels puts at the centre of the anarchy of production — "No one
 *  knows how much of his particular article is coming on the market, nor how
 *  much of it will be wanted" — and so neither notices the case he describes
 *  next: "the markets are glutted, products accumulate, as multitudinous as
 *  they are unsaleable", filtering off only "more or less depreciated in
 *  value" (Anti-Dühring III.2).
 *
 *  A coin launched and still sitting on its curve is exactly that
 *  accumulation. Each one ties up the gas and the opening buy that went into
 *  it, and produces nothing until it graduates. Launching more while they pile
 *  up is paying to add to the glut.
 *
 *  So two limits, both measured from our own launches rather than guessed:
 *  a ceiling on how many coins may be open at once, and a floor on the share
 *  that ever graduates. */

import type { TokenIncome } from "./income.js";

export interface AbsorptionState {
  /** Coins we have launched in total. */
  launched: number;
  /** Launched and still on the curve: the unsold stock. */
  open: number;
  graduated: number;
  /** Share of launches that graduated, or null before anything launched. */
  graduationRate: number | null;
  /** The oldest open coin's age in seconds, 0 when none are open. A single
   *  ancient straggler is a different problem from a wall of fresh ones, and
   *  the caller can report which it is. */
  oldestOpenSecs: number;
}

export interface AbsorptionLimits {
  /** Most coins left sitting on the curve at once. */
  maxOpen: number;
  /** Below this share graduating, stop adding supply. 0 disables the floor. */
  minGraduationRate: number;
  /** Launches before the rate is taken seriously; below this the sample is
   *  too small to act on and the floor is not applied. */
  graceLaunches: number;
}

export const defaultAbsorptionLimits: AbsorptionLimits = {
  maxOpen: 8,
  minGraduationRate: 0.1,
  graceLaunches: 5,
};

/** Measure absorption from the chain's own record of our launches. */
export function absorptionFrom(rows: TokenIncome[], now = Math.floor(Date.now() / 1000)): AbsorptionState {
  let open = 0;
  let graduated = 0;
  let oldestOpenSecs = 0;
  for (const r of rows) {
    if (r.graduated) {
      graduated++;
      continue;
    }
    open++;
    if (r.launchedAt > 0) oldestOpenSecs = Math.max(oldestOpenSecs, Math.max(0, now - r.launchedAt));
  }
  const launched = rows.length;
  return {
    launched,
    open,
    graduated,
    graduationRate: launched > 0 ? graduated / launched : null,
    oldestOpenSecs,
  };
}

export interface LaunchVerdict {
  ok: boolean;
  reason: string;
}

/** May we add another coin to the market?
 *
 *  Refusing is not failure. It is the one thing the anarchy of production
 *  cannot do: stop when the market has had enough. */
export function mayLaunch(state: AbsorptionState, limits: AbsorptionLimits = defaultAbsorptionLimits): LaunchVerdict {
  if (limits.maxOpen > 0 && state.open >= limits.maxOpen) {
    const days = (state.oldestOpenSecs / 86_400).toFixed(1);
    return {
      ok: false,
      reason:
        `${state.open} coins are still on the curve (limit ${limits.maxOpen}, oldest ${days} days). ` +
        `Launching more would add to a glut, not to income.`,
    };
  }
  if (
    limits.minGraduationRate > 0 &&
    state.launched >= limits.graceLaunches &&
    state.graduationRate !== null &&
    state.graduationRate < limits.minGraduationRate
  ) {
    return {
      ok: false,
      reason:
        `only ${(state.graduationRate * 100).toFixed(1)}% of ${state.launched} launches graduated ` +
        `(floor ${(limits.minGraduationRate * 100).toFixed(0)}%). The market is not absorbing these.`,
    };
  }
  return { ok: true, reason: `${state.open} open, ${state.graduated} of ${state.launched} graduated` };
}
