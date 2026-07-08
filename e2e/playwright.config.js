const { defineConfig, devices } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './tests',
  timeout: 30000,
  retries: 1,
  reporter: [['list'], ['html', { open: 'never' }]],

  use: {
    baseURL: 'http://localhost:3000',
    headless: true,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },

  // Spin up a local static file server before any test run
  webServer: {
    command: 'npx serve .. -p 3000 --no-clipboard',
    url: 'http://localhost:3000',
    reuseExistingServer: !process.env.CI,
    timeout: 15000,
  },

  projects: [
    // Desktop browsers
    { name: 'chromium',      use: { ...devices['Desktop Chrome'] } },
    { name: 'firefox',       use: { ...devices['Desktop Firefox'] } },
    { name: 'webkit',        use: { ...devices['Desktop Safari'] } },

    // Mobile emulation — catches iOS Safari and Android Chrome issues
    { name: 'mobile-webkit', use: { ...devices['iPhone 14'] } },
    { name: 'mobile-chrome', use: { ...devices['Pixel 7'] } },
  ],
});
