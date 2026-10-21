import type { WebSocket } from "ws";

/**
 * WebSocket broadcast hub. Clients subscribe to channels keyed by
 * channel|token|interval; broadcasts fan out to exact and wildcard
 * subscribers.
 */
export class Hub {
  private subs = new Map<string, Set<WebSocket>>();

  private key(channel: string, token?: string, interval?: string) {
    return [channel, (token ?? "*").toLowerCase(), interval ?? ""].join("|");
  }

  subscribe(ws: WebSocket, channel: string, token?: string, interval?: string) {
    const key = this.key(channel, token, interval);
    let set = this.subs.get(key);
    if (!set) {
      set = new Set();
      this.subs.set(key, set);
    }
    set.add(ws);
  }

  unsubscribe(ws: WebSocket, channel: string, token?: string, interval?: string) {
    const key = this.key(channel, token, interval);
    const set = this.subs.get(key);
    if (!set) return;
    set.delete(ws);
    if (set.size === 0) this.subs.delete(key);
  }

  drop(ws: WebSocket) {
    for (const [key, set] of this.subs) {
      set.delete(ws);
      if (set.size === 0) this.subs.delete(key);
    }
  }

  broadcast(channel: string, token: string | undefined, data: unknown, interval?: string) {
    const payload = JSON.stringify({ channel, token, data });
    const keys = [this.key(channel, token, interval), this.key(channel, "*")];
    if (interval) keys.splice(1, 0, this.key(channel, token));
    const sent = new Set<WebSocket>();
    for (const key of keys) {
      const set = this.subs.get(key);
      if (!set) continue;
      for (const ws of set) {
        if (sent.has(ws) || ws.readyState !== 1) continue;
        sent.add(ws);
        ws.send(payload);
      }
    }
  }
}
