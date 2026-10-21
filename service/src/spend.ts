/** Charging a grant for what it actually signed.
 *
 *  A debit is taken before the work starts, because the alternative — charging
 *  afterwards — means a tenant with two requests in flight is checked twice
 *  against the same balance and spends it twice. So the question this file
 *  answers is the refund: how much of a reservation to give back.
 *
 *  A run that never signed is refunded in full. A run that signed is charged
 *  what the launch really cost, which can be less than reserved when the
 *  factory clamps an opening buy to its launch-window cap.
 *
 *  A run that threw is the awkward case, because the throw may have come from
 *  after the transaction was broadcast. Rather than guess from an error
 *  message, ask the chain: if the operator's nonce has not moved, nothing was
 *  sent and the reservation is refunded. If it has moved — or cannot be read —
 *  the spend stays debited. The one operator key signs for every tenant, so
 *  another tenant's launch can move the nonce and make this read conservative;
 *  erring towards charging is the right direction when the alternative is
 *  giving away ETH. */

import { defaultGenePool, type LaunchSpec } from "@lucreocracy/core";

export interface Spend {
  wei: bigint;
  launches: number;
}

interface PublishLike {
  dryRun: boolean;
  firstBuyWei: bigint;
}

/** What one finished pipeline run really spent. A dry run spent nothing. */
export function spentByRun(result: { publish?: PublishLike }): Spend {
  const p = result.publish;
  if (!p || p.dryRun) return { wei: 0n, launches: 0 };
  return { wei: p.firstBuyWei, launches: 1 };
}

/** What a loop really spent, summed over the launches it managed. */
export function spentByLoop(result: { results: { publish?: PublishLike }[] }): Spend {
  let wei = 0n;
  let launches = 0;
  for (const r of result.results) {
    const one = spentByRun(r);
    wei += one.wei;
    launches += one.launches;
  }
  return { wei, launches };
}

/** The most one launch of this spec could cost once the loop starts breeding.
 *
 *  `firstBuyWei` is a gene, so a later generation can raise it; reserving the
 *  seed's own opening buy would under-reserve a loop that mutates upwards. The
 *  gene pool's ceiling is the honest worst case. */
export function worstCasePerLaunch(spec: LaunchSpec): bigint {
  let max = spec.firstBuyWei;
  for (const v of defaultGenePool.firstBuyWei) if (v > max) max = v;
  return max;
}

/** Did a failed run get as far as broadcasting? Undefined either side means we
 *  could not tell, which counts as yes. */
export function broadcast(before: number | undefined, after: number | undefined): boolean {
  if (before === undefined || after === undefined) return true;
  return after !== before;
}
