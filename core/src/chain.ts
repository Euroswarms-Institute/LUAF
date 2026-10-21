/** The chain definition the pipeline talks to, and the operator's nonce.
 *
 *  Robinhood Chain is not in viem's registry, so every client here is built
 *  from the deployment's own chain id and the configured RPC. The deployment
 *  JSON is the authority on which chain a tenant is on; nothing infers it. */

import { createPublicClient, defineChain, http, type Chain, type PublicClient } from "viem";

import type { LauncherDeployment } from "./config.js";

export function chainFor(chainId: number, rpcUrl: string): Chain {
  return defineChain({
    id: chainId,
    name: `chain-${chainId}`,
    nativeCurrency: { name: "Ether", symbol: "ETH", decimals: 18 },
    rpcUrls: { default: { http: [rpcUrl] } },
  });
}

/** The operator wallet's transaction count.
 *
 *  This is how a run that failed before broadcasting is told apart from one
 *  that failed after. Undefined means the question could not be answered, and
 *  the caller must treat that as "it may have broadcast": the cost of guessing
 *  wrong the other way is ETH. */
export async function operatorNonce(dep: LauncherDeployment, rpcUrl: string): Promise<number | undefined> {
  try {
    const client = createPublicClient({ chain: chainFor(dep.chainId, rpcUrl), transport: http(rpcUrl) }) as PublicClient;
    return await client.getTransactionCount({ address: dep.operator });
  } catch {
    return undefined;
  }
}
