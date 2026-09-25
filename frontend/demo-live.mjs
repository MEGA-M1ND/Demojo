// Live demo walkthrough (real OpenRouter). Not part of CI.
import { chromium } from '@playwright/test'
import path from 'node:path'
const FIX = path.resolve('../fixtures/software'), OUT = '../docs/demo'
const b = await chromium.launch(); const page = await b.newPage({ viewport: { width: 1440, height: 1000 } })
const shot = async (n) => { await page.addStyleTag({ content: '.topbar{position:static!important}' }); await page.screenshot({ path: `${OUT}/${n}.png`, fullPage: true }); console.log('shot', n) }
await page.goto('http://127.0.0.1:8000/')
await page.getByLabel('Project name').fill('Tallyfox launch video')
await page.getByRole('button', { name: 'New project' }).click()
await page.getByTestId('file-input').setInputFiles(['tallyfox_dashboard.png', 'tallyfox_new_invoice.png', 'tallyfox_reports.png', 'tallyfox_recording.mp4'].map(f => path.join(FIX, f)))
await page.getByTestId('asset-card').nth(3).waitFor({ timeout: 60000 })
const [chooser] = await Promise.all([page.waitForEvent('filechooser'), page.getByRole('button', { name: /Add logo/ }).click()])
await chooser.setFiles(path.join(FIX, 'tallyfox_logo.png'))
await page.locator('.logo-box img').waitFor()
await page.getByLabel('Accent colour hex').fill('#EA580C'); await page.getByLabel('Accent colour hex').blur()
await page.waitForTimeout(800); await shot('1-upload')
await page.getByRole('button', { name: /Continue to Describe/ }).click()
await page.getByLabel('Product name').fill('Tallyfox')
await page.getByLabel('Brief description').fill('Tallyfox is invoicing software for freelancers and small studios. You can create an invoice, add line items, and send it to a client from one screen, and the dashboard shows outstanding, paid, and overdue totals.')
await page.getByLabel('Intended audience').fill('Freelancers and small creative studios')
await page.getByLabel('Selling point 1').fill('See outstanding and overdue totals at a glance')
await page.getByLabel('Selling point 2').fill('Create and send an invoice in one screen')
await page.getByLabel('Selling point 3').fill('Revenue by client report')
await page.getByLabel('Call to action').fill('Start free today')
await page.getByLabel('Website text (optional)').fill('tallyfox.example')
await page.locator('#wf').fill('Open the dashboard, click New invoice, fill in the client and line items, then click Send invoice.')
await page.getByText('All changes saved').waitFor(); await shot('2-describe')
await page.getByRole('button', { name: /Continue to Review/ }).click()
await page.getByRole('button', { name: 'Generate story' }).click()
await page.getByTestId('scene-card').first().waitFor({ timeout: 240000 })
await page.waitForTimeout(1500); await shot('3-review-ai-draft')
// Resolve any claims the AI couldn't ground: confirm the user's own CTA wording, remove the rest.
for (let guard = 0; guard < 10; guard++) {
  const blocker = page.locator('.blockers .link').first()
  if (!(await blocker.count())) break
  await blocker.click()
  const btn = page.getByRole('button', { name: 'Remove from script' }).first()
  if (await btn.count()) { await btn.click(); await page.waitForTimeout(400) }
  const still = page.getByRole('button', { name: 'Remove from script' }).first()
  if (await still.count()) await page.getByRole('button', { name: /Confirm it/ }).first().click()
  await page.getByText('All changes saved').waitFor({ timeout: 20000 })
  await page.waitForTimeout(600)
}
await page.getByRole('button', { name: 'Measure narration' }).click()
await page.getByText(/measured narration/).waitFor({ timeout: 180000 })
await page.waitForTimeout(800); await shot('4-review-measured')
await page.getByRole('button', { name: /Continue to Export/ }).click()
await page.getByRole('button', { name: /Export final MP4/ }).click()
const acc = page.getByRole('button', { name: /^Accept .* and render/ })
await Promise.race([page.getByTestId('export-video').waitFor({ timeout: 400000 }), acc.waitFor({ timeout: 400000 }).then(() => acc.click())])
await page.getByTestId('export-video').waitFor({ timeout: 400000 })
await page.waitForTimeout(1000); await shot('5-export')
const [dl] = await Promise.all([page.waitForEvent('download'), page.getByRole('link', { name: 'Download MP4' }).click()])
await dl.saveAs(`${OUT}/tallyfox_live_demo.mp4`)
for (const [name, file] of [['Captions', 'captions.srt'], ['Narration script', 'script.txt'], ['Storyboard', 'storyboard.json']]) {
  const [d] = await Promise.all([page.waitForEvent('download'), page.getByRole('link', { name: new RegExp(name) }).click()]); await d.saveAs(`${OUT}/${file}`)
}
console.log('done'); await b.close()
