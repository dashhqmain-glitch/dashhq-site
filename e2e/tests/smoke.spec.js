const { test, expect } = require('@playwright/test');

// ── Homepage ─────────────────────────────────────────────────────────────────

test('homepage loads with correct title', async ({ page }) => {
  await page.goto('/');
  await expect(page).toHaveTitle(/Dash HQ/);
});

test('testimonial marquee is scrolling on all browsers', async ({ page }) => {
  await page.goto('/');

  const track = page.locator('.voices-track');
  await expect(track).toBeVisible();

  // Give the JS marquee time to initialise and measure scrollWidth
  await page.waitForTimeout(1000);

  const t1 = await track.evaluate(el => el.style.transform);
  // translate3d, not translateX — GPU-layer fix for iOS Safari.
  expect(t1).toMatch(/translate3d/);

  // Poll instead of a fixed wait: CI's headless webkit runners can be slow
  // to advance rAF, so give it real headroom before declaring it stuck.
  await expect.poll(() => track.evaluate(el => el.style.transform), { timeout: 5000 }).not.toBe(t1);
});

test('nav anchor clicks do not add hash to URL', async ({ page }) => {
  await page.goto('/');
  const toggle = page.locator('.nav-toggle');
  if (await toggle.isVisible()) {
    // Mobile layout: the desktop nav-links are display:none and the closed
    // drawer link sits at opacity:0/pointer-events:none, so it must be
    // opened first, same as a real user would.
    await toggle.click();
    await page.locator('.nav-drawer a[href="#team"]').click();
  } else {
    await page.locator('.nav-links a[href="#team"]').click();
  }
  await page.waitForTimeout(300);
  expect(page.url()).not.toContain('#');
});

// ── Research modal ────────────────────────────────────────────────────────────

test('article opens with slide-in animation', async ({ page }) => {
  await page.goto('/');

  // Click the first article card on the main page
  await page.locator('.post').first().click();
  await page.waitForTimeout(600);

  const article = page.locator('#rvArticle');
  await expect(article).toHaveClass(/on/);

  // Article panel must have content and a visible heading
  await expect(page.locator('#rvPanel h1')).toBeVisible();
});

test('back-to-grid works inside modal', async ({ page }) => {
  await page.goto('/');
  await page.locator('.post').first().click();
  await page.waitForTimeout(500);

  // Click the back button
  await page.locator('.rv-backwrap button, .rv-back').first().click();
  await page.waitForTimeout(400);

  await expect(page.locator('#rvGrid')).toHaveClass(/on/);
});

// ── Portal ────────────────────────────────────────────────────────────────────

test('portal page loads at clean /portal URL', async ({ page }) => {
  await page.goto('/portal');
  await expect(page).toHaveTitle(/Citizens Portal/);
  // URL must not end with .html or contain a hash
  expect(page.url()).toMatch(/\/portal$/);
});

test('portal back-link returns to homepage', async ({ page }) => {
  await page.goto('/portal');
  await page.locator('a.logo, a.nav-back').first().click();
  await expect(page).toHaveTitle(/Dash HQ/);
});
