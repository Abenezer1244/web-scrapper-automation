const { chromium } = require('playwright');

const APP = 'https://app.bridgeleads.io';
const SCREENSHOTS = 'C:/Users/Windows/OneDrive - Seattle Colleges/Pictures/Saved Pictures';

(async () => {
  const browser = await chromium.launch({ headless: false, slowMo: 300 });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();

  console.log('1. Opening login page...');
  await page.goto(`${APP}/login`);
  await page.waitForTimeout(2000);

  if (page.url().includes('dashboard') || page.url().includes('scrapers')) {
    console.log('   Already logged in!');
  } else {
    console.log('   >> Please log in manually. Waiting up to 2 minutes...');
    await page.waitForURL('**/dashboard**', { timeout: 120000 });
    console.log('   Logged in!');
  }
  await page.screenshot({ path: `${SCREENSHOTS}/qa_02_dashboard.png`, fullPage: false });

  // New scraper flow
  console.log('2. New scraper — picking Chelan Probate...');
  await page.goto(`${APP}/scrapers/new`);
  await page.waitForTimeout(3000);
  await page.screenshot({ path: `${SCREENSHOTS}/qa_03_new_scraper.png` });

  // Step through the form — click WA, Chelan, Probate
  try {
    await page.click('text=Washington', { timeout: 5000 });
    await page.waitForTimeout(1000);
    await page.click('text=Chelan', { timeout: 5000 });
    await page.waitForTimeout(1000);
    await page.screenshot({ path: `${SCREENSHOTS}/qa_04_chelan_selected.png` });

    await page.click('text=Probate', { timeout: 5000 });
    await page.waitForTimeout(1000);

    // Click Next to get to schedule
    const nextBtns = await page.locator('button:has-text("Next")').all();
    for (const btn of nextBtns) {
      if (await btn.isVisible()) {
        await btn.click();
        await page.waitForTimeout(1500);
      }
    }
    // Click Next again if needed (multi-step)
    const nextBtns2 = await page.locator('button:has-text("Next")').all();
    for (const btn of nextBtns2) {
      if (await btn.isVisible()) {
        await btn.click();
        await page.waitForTimeout(1500);
      }
    }

    await page.screenshot({ path: `${SCREENSHOTS}/qa_05_schedule.png` });

    const body = await page.textContent('body');
    console.log(`   "Rolling 30" visible: ${body.includes('Rolling 30')}`);
    console.log(`   "max 30 days" warning: ${body.includes('maximum of 30')}`);
  } catch (e) {
    console.log(`   Scraper form error: ${e.message.slice(0, 100)}`);
    await page.screenshot({ path: `${SCREENSHOTS}/qa_05_error.png` });
  }

  // Pricing page
  console.log('3. Pricing page...');
  await page.goto(`${APP}/pricing`);
  await page.waitForTimeout(4000);
  await page.screenshot({ path: `${SCREENSHOTS}/qa_06_pricing.png`, fullPage: true });

  const pText = await page.textContent('body');
  console.log(`   $79: ${pText.includes('$79')}`);
  console.log(`   FOUNDING40: ${pText.includes('FOUNDING40')}`);
  console.log(`   Founding Member: ${pText.includes('Founding')}`);

  // Coverage page
  console.log('4. Coverage page...');
  await page.goto(`${APP}/coverage`);
  await page.waitForTimeout(4000);
  await page.screenshot({ path: `${SCREENSHOTS}/qa_07_coverage.png`, fullPage: true });

  const cText = await page.textContent('body');
  console.log(`   Chelan listed: ${cText.includes('Chelan')}`);
  console.log(`   Probate badge: ${cText.includes('Probate')}`);

  // Settings
  console.log('5. Settings pricing...');
  await page.goto(`${APP}/settings`);
  await page.waitForTimeout(4000);
  await page.screenshot({ path: `${SCREENSHOTS}/qa_08_settings.png`, fullPage: true });

  const sText = await page.textContent('body');
  console.log(`   $79 in settings: ${sText.includes('79')}`);

  console.log('\n=== QA COMPLETE ===');
  console.log('Screenshots saved as qa_02 through qa_08 in Pictures.');
  console.log('Browser stays open 20s for inspection...');
  await page.waitForTimeout(20000);
  await browser.close();
})();
