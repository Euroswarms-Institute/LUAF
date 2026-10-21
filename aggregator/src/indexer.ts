/** The polling loop: every source, every chain, forward only.
 *
 *  Each source keeps its own cursor, so adding a launchpad backfills only that
 *  one and a slow chain never holds up a fast one. Block timestamps are read
 *  once per block and cached, because a busy launchpad puts dozens of launches
 *  in the same block and asking for its timestamp each time is the single
 *  largest source of RPC calls in a backfill. */

import { createPublicClient, defineChain, http, type Address, type PublicClient } from "viem";

import { adapterFor, readTokenMeta, type Adapter } from "./adapters.js";
import type { AggregatorConfig, ChainConfig, SourceConfig } from "./config.js";
import type { Store } from "./db.js";

export interface Broadcaster {
  broadcast(channel: string, token: string | undefined, data: unknown): void;
}

export interface IndexerStats {
  launches: number;
  graduations: number;
  errors: number;
  lastBlock: Record<string, number>;
}

function clientFor(chain: ChainConfig): PublicClient {
  const def = defineChain({
    id: chain.chainId,
    name: chain.label,
    nativeCurrency: { name: "Ether", symbol: "ETH", decimals: 18 },
    rpcUrls: { default: { http: [chain.rpcUrl] } },
  });
  return createPublicClient({ chain: def, transport: http(chain.rpcUrl, { retryCount: 3 }) }) as PublicClient;
}

export class Indexer {
  private readonly clients = new Map<number, PublicClient>();
  private readonly adapters = new Map<string, Adapter>();
  private readonly timestamps = new Map<string, number>();
  private timer?: NodeJS.Timeout;
  private running = false;
  readonly stats: IndexerStats = { launches: 0, graduations: 0, errors: 0, lastBlock: {} };

  constructor(
    private readonly cfg: AggregatorConfig,
    private readonly store: Store,
    private readonly hub?: Broadcaster,
  ) {
    for (const chain of cfg.chains) this.clients.set(chain.chainId, clientFor(chain));
    // Build every adapter up front: a malformed source description is a
    // configuration error and should stop the service, not fail silently on
    // the first poll.
    for (const s of cfg.sources) this.adapters.set(s.id, adapterFor(s));
  }

  private client(chainId: number): PublicClient {
    const c = this.clients.get(chainId);
    if (!c) throw new Error(`no client for chain ${chainId}`);
    return c;
  }

  private chain(chainId: number): ChainConfig {
    const c = this.cfg.chains.find((x) => x.chainId === chainId);
    if (!c) throw new Error(`no chain ${chainId}`);
    return c;
  }

  private async blockTime(chainId: number, block: number): Promise<number> {
    const key = `${chainId}:${block}`;
    const hit = this.timestamps.get(key);
    if (hit !== undefined) return hit;
    const b = await this.client(chainId).getBlock({ blockNumber: BigInt(block) });
    const ts = Number(b.timestamp);
    // Bounded cache: a backfill would otherwise hold every block it touched.
    if (this.timestamps.size > 5_000) this.timestamps.clear();
    this.timestamps.set(key, ts);
    return ts;
  }

  /** One pass over one source. Returns the block it reached. */
  private async pollSource(s: SourceConfig): Promise<number> {
    const adapter = this.adapters.get(s.id)!;
    const client = this.client(s.chainId);
    const chunk = BigInt(this.chain(s.chainId).logChunk ?? 10_000);
    const head = await client.getBlockNumber();
    let from = BigInt(this.store.cursor(s.id, s.startBlock));
    if (from > head) return Number(head);

    while (from <= head) {
      const to = from + chunk - 1n > head ? head : from + chunk - 1n;
      const launches = await client.getLogs({
        address: s.address as Address,
        event: adapter.launchEvent,
        fromBlock: from,
        toBlock: to,
      });
      for (const log of launches) {
        const rec = adapter.decodeLaunch(log);
        if (!rec) continue;
        const meta =
          rec.name || rec.symbol
            ? { name: rec.name, symbol: rec.symbol }
            : await readTokenMeta(client, rec.address as Address);
        const launchedAt = await this.blockTime(s.chainId, rec.block);
        this.store.upsertCoin({
          chain_id: s.chainId,
          address: rec.address,
          source_id: s.id,
          name: meta.name,
          symbol: meta.symbol,
          creator: rec.creator,
          launched_at: launchedAt,
          launch_block: rec.block,
          target_raise_wei: rec.targetRaiseWei ?? null,
          deadline: rec.deadline ?? null,
        });
        this.stats.launches++;
        this.hub?.broadcast("coin:launched", rec.address.toLowerCase(), {
          chainId: s.chainId,
          source: s.id,
          address: rec.address.toLowerCase(),
          creator: rec.creator.toLowerCase(),
          launchedAt,
          graduated: false,
          targetRaiseWei: rec.targetRaiseWei ?? null,
          deadline: rec.deadline ?? null,
          ...meta,
        });
      }

      if (adapter.graduateEvent) {
        const grads = await client.getLogs({
          address: s.address as Address,
          event: adapter.graduateEvent,
          fromBlock: from,
          toBlock: to,
        });
        for (const log of grads) {
          const rec = adapter.decodeGraduation(log);
          if (!rec) continue;
          const graduatedAt = await this.blockTime(s.chainId, rec.block);
          this.store.markGraduated(s.chainId, rec.address, graduatedAt, rec.poolId, {
            raisedWei: rec.raisedWei ?? null,
            pairSeededWei: rec.pairSeededWei ?? null,
          });
          this.stats.graduations++;
          this.hub?.broadcast("coin:graduated", rec.address.toLowerCase(), {
            chainId: s.chainId,
            source: s.id,
            address: rec.address.toLowerCase(),
            graduatedAt,
            raisedWei: rec.raisedWei ?? null,
            pairSeededWei: rec.pairSeededWei ?? null,
          });
        }
      }

      // Commit the cursor per page, so an interrupted backfill resumes where
      // it stopped rather than starting the source again.
      this.store.setCursor(s.id, Number(to));
      this.stats.lastBlock[s.id] = Number(to);
      from = to + 1n;
    }
    return Number(head);
  }

  /** One pass over every source. Sources are independent, so one failing
   *  endpoint does not stop the rest. */
  async poll(): Promise<void> {
    for (const s of this.cfg.sources) {
      try {
        await this.pollSource(s);
      } catch (e) {
        this.stats.errors++;
        console.warn(`[${s.id}] ${(e as Error).message}`);
      }
    }
  }

  async start(): Promise<void> {
    if (this.running) return;
    this.running = true;
    const tick = async () => {
      if (!this.running) return;
      await this.poll();
      if (this.running) this.timer = setTimeout(tick, this.cfg.pollIntervalMs);
    };
    await tick();
  }

  stop(): void {
    this.running = false;
    if (this.timer) clearTimeout(this.timer);
  }
}
