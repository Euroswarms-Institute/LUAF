import { describe, expect, it } from "vitest";

import { designerSystemPrompt, listProfiles, loadProfile, ProfileError, OUTPUT_FORMAT_FRAGMENT, RUNTIME_FRAGMENT } from "./profiles.js";

const profiles = listProfiles();

/** Vendor SDKs and agent frameworks the designer must not be told to import.
 *  A generated agent inherits every dependency its prompt mandates, and a
 *  framework's outage then becomes the launched unit's outage. The prompts
 *  mandate none, and this is the guard that keeps it that way. */
const FORBIDDEN = [/swarms/i, /crewai/i, /langchain/i, /autogen/i, /\bsolders\b/i, /sol-402/i];

/** Dangling references: a prompt must not promise the designer a section the
 *  pipeline does not actually send. */
const DANGLING = [/required quality[- ]packages/i, /quality packages \(mandatory\)/i];

describe("the designer profiles", () => {
  it("ships a usable set", () => {
    expect(profiles.length).toBeGreaterThan(20);
    expect(profiles).toContain("crypto");
    expect(profiles).toContain("x402_ai_payments");
  });

  it("loads each one with every prompt it needs", () => {
    for (const id of profiles) {
      const p = loadProfile(id);
      expect(p.id, id).toBe(id);
      expect(p.displayName.length, id).toBeGreaterThan(0);
      expect(p.systemPrompt.length, id).toBeGreaterThan(500);
      expect(p.topicPrompt.length, id).toBeGreaterThan(20);
    }
  });

  it("mandates no agent framework or vendor SDK", () => {
    for (const id of profiles) {
      const text = designerSystemPrompt(loadProfile(id));
      for (const pattern of FORBIDDEN) {
        expect(pattern.test(text), `${id} mentions ${pattern}`).toBe(false);
      }
    }
  });

  it("promises the designer nothing the pipeline does not send", () => {
    for (const id of profiles) {
      const text = designerSystemPrompt(loadProfile(id));
      for (const pattern of DANGLING) {
        expect(pattern.test(text), `${id} refers to ${pattern}, which is never supplied`).toBe(false);
      }
    }
  });

  it("carries no leftover format-escaping artifact", () => {
    for (const id of profiles) {
      // "{ {" came from the Python pipeline's str.format escaping and means
      // nothing to a model reading the prompt directly.
      expect(loadProfile(id).systemPrompt.includes("{ {"), id).toBe(false);
    }
  });

  it("assembles a prompt carrying the runtime and output contracts", () => {
    const text = designerSystemPrompt(loadProfile("crypto"));
    expect(text).toContain(RUNTIME_FRAGMENT);
    expect(text).toContain(OUTPUT_FORMAT_FRAGMENT);
    // The output contract names exactly the keys the parser requires.
    for (const key of ["name", "ticker", "description", "agent", "useCases", "tags", "requirements", "language"]) {
      expect(text).toContain(key);
    }
  });

  it("asks for a dependency list that matches what the code imports", () => {
    const text = designerSystemPrompt(loadProfile("crypto"));
    expect(text).toMatch(/import nothing you do not call/i);
  });

  it("refuses an unknown profile, and says what there is", () => {
    expect(() => loadProfile("not-a-profile")).toThrow(ProfileError);
    expect(() => loadProfile("not-a-profile")).toThrow(/crypto/);
  });
});
