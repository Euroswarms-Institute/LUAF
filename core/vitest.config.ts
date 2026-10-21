import { defineConfig } from "vitest/config";

// Only this package's tests. The Solidity suite under contracts/ is Hardhat's,
// and vitest cannot load it.
export default defineConfig({
  test: { include: ["src/**/*.test.ts"] },
});
