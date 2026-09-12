import {defineConfig} from '@playwright/test';
export default defineConfig({
  testDir: './tests/integration/browser', timeout: 60000, workers: 1,
  outputDir: process.env.PLATFORM_PROFILE==='kind' ? '.local/helm/browser-results' : '.local/browser-results', reporter: 'list',
  use: {baseURL: process.env.PLATFORM_ORIGIN || 'http://127.0.0.1:18080',
        trace: 'off', screenshot: 'off', headless: true,
        ignoreHTTPSErrors: process.env.PLATFORM_PROFILE==='kind',
        launchOptions: process.env.PLATFORM_PROFILE==='kind' ? {args:['--host-resolver-rules=MAP platform.example.test 127.0.0.1']} : {}},
});
