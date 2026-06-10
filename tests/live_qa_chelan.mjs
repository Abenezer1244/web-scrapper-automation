import { chromium } from 'playwright';

const APP = 'https://app.bridgeleads.io';
const SCREENSHOTS = 'C:/Users/Windows/OneDrive - Seattle Colleges/Pictures/Saved Pictures';

(async () => {
  const browser = await chromium.launch({ headless: false, slowMo: 500 });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();

  // 1. Login
  console.log('1. Logging in...');
  await page.goto(`${APP}/login`);
  await page.waitForTimeout(2000);
  await page.screenshot({ path: `${SCREENSHOTS}/qa_01_login.png` });

  // Check if already logged in (redirected to dashboard)
  if (page.url().includes('dashboard')) {
    console.log('   Already logged in!');
  } else {
    // Need credentials — prompt user
    console.log('   Please log in manually in the browser window...');
    await page.waitForURL('**/dashboard**', { timeout: 120000 });
    console.log('   Logged in!');
  }
  await page.screenshot({ path: `${SCREENSHOTS}/qa_02_dashboard.png` });

  // 2. Navigate to new scraper
  console.log('2. Creating new scraper — Chelan Probate...');
  await page.goto(`${APP}/scrapers/new`);
  await page.waitForTimeout(3000);
  await page.screenshot({ path: `${SCREENSHOTS}/qa_03_new_scraper.png` });

  // 3. Pick Washington state
  const waBtn = page.locator('text=Washington').first();
  if (await waBtn.isVisible()) {
    await waBtn.click();
    await page.waitForTimeout(1000);
  }

  // 4. Pick Chelan county
  const chelanBtn = page.locator('text=Chelan').first();
  if (await chelanBtn.isVisible()) {
    await chelanBtn.click();
    await page.waitForTimeout(1000);
  }
  await page.screenshot({ path: `${SCREENSHOTS}/qa_04_chelan_selected.png` });

  // 5. Pick Probate record type
  const probateBtn = page.locator('text=Probate').first();
  if (await probateBtn.isVisible()) {
    await probateBtn.click();
    await page.waitForTimeout(1000);
  }

  // 6. Go to next step (schedule)
  const nextBtn = page.locator('button:has-text("Next"), button:has-text("Continue")').first();
  if (await nextBtn.isVisible()) {
    await nextBtn.click();
    await page.waitForTimeout(1000);
    // Click next again to get to schedule step
    if (await nextBtn.isVisible()) {
      await nextBtn.click();
      await page.waitForTimeout(1000);
    }
  }
  await page.screenshot({ path: `${SCREENSHOTS}/qa_05_schedule_step.png` });

  // 7. Check for "Rolling 30 days" and warning message
  const pageText = await page.textContent('body');
  const has30Days = pageText.includes('Rolling 30') || pageText.includes('rolling_30');
  const hasWarning = pageText.includes('maximum of 30 days');
  console.log(`   Rolling 30 days visible: ${has30Days}`);
  console.log(`   Max 30 days warning visible: ${hasWarning}`);

  // 8. Also check pricing page
  console.log('3. Checking pricing page...');
  await page.goto(`${APP}/pricing`);
  await page.waitForTimeout(3000);
  await page.screenshot({ path: `${SCREENSHOTS}/qa_06_pricing.png` });

  const pricingText = await page.textContent('body');
  console.log(`   $79 visible: ${pricingText.includes('$79')}`);
  console.log(`   FOUNDING40 visible: ${pricingText.includes('FOUNDING40')}`);
  console.log(`   founding offer visible: ${pricingText.includes('Founding')}`);

  // 9. Check coverage page
  console.log('4. Checking coverage page...');
  await page.goto(`${APP}/coverage`);
  await page.waitForTimeout(3000);
  await page.screenshot({ path: `${SCREENSHOTS}/qa_07_coverage.png` });

  const coverageText = await page.textContent('body');
  console.log(`   Counties listed: ${coverageText.includes('Chelan')}`);
  console.log(`   Probate badge: ${coverageText.includes('Probate')}`);

  // 10. Check settings pricing
  console.log('5. Checking settings page pricing...');
  await page.goto(`${APP}/settings`);
  await page.waitForTimeout(3000);
  await page.screenshot({ path: `${SCREENSHOTS}/qa_08_settings.png` });

  const settingsText = await page.textContent('body');
  console.log(`   $79 in settings: ${settingsText.includes('$79')}`);

  console.log('\nDone! Screenshots saved to Pictures/Saved Pictures/qa_*.png');
  console.log('Keeping browser open for 30 seconds for manual inspection...');
  await page.waitForTimeout(30000);

  await browser.close();
})();
