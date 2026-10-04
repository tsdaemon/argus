import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  workers: 1,
  // One image per platform: fonts render differently on WSL, macOS, and CI.
  snapshotPathTemplate: "{testDir}/__screenshots__/{arg}-{platform}{ext}",
  use: { baseURL: "http://127.0.0.1:8422", trace: "retain-on-failure" },
  webServer: {
    command: ".venv/bin/python -m tests.frontend_server",
    cwd: "..",
    url: "http://127.0.0.1:8422/agent/health",
    timeout: 30000,
    reuseExistingServer: false,
    gracefulShutdown: { signal: "SIGTERM", timeout: 5000 },
  },
});
