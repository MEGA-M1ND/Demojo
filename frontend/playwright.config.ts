import { defineConfig } from '@playwright/test'

// Browser test of the full flow against an isolated backend in FIXTURE mode
// (no AI calls). Requires the frontend to be built (npm run build) because the
// backend serves frontend/dist.
const PORT = 8765
export default defineConfig({
  testDir: './e2e',
  timeout: 240_000,
  expect: { timeout: 30_000 },
  reporter: [['list']],
  use: { baseURL: `http://127.0.0.1:${PORT}`, viewport: { width: 1440, height: 1000 }, acceptDownloads: true },
  webServer: {
    command: `../scripts/e2e-server.sh ${PORT} ${process.env.E2E_DATA_DIR ?? ''}`,
    url: `http://127.0.0.1:${PORT}/api/health`,
    reuseExistingServer: false,
    timeout: 60_000,
  },
})
