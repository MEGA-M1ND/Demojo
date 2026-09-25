import { expect, test } from '@playwright/test'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const FIX = path.resolve(HERE, '../../fixtures/software')
const SHOTS = process.env.E2E_SCREENSHOTS

async function shot(page: import('@playwright/test').Page, name: string) {
  if (!SHOTS) return
  // Full-page captures render sticky headers mid-page; un-stick it for the screenshot only.
  await page.addStyleTag({ content: '.topbar { position: static !important; }' })
  await page.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: true })
}

test('upload → describe → editable storyboard → playable export → download', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByText('Fixture mode · not AI')).toBeVisible()
  await page.getByLabel('Project name').fill('E2E Tallyfox launch')
  await page.getByRole('button', { name: 'New project' }).click()

  // Upload: three screenshots and one recording, with real upload progress rows.
  const files = ['tallyfox_dashboard.png', 'tallyfox_new_invoice.png', 'tallyfox_reports.png', 'tallyfox_recording.mp4'].map((f) => path.join(FIX, f))
  await page.getByTestId('file-input').setInputFiles(files)
  await expect(page.getByTestId('asset-card')).toHaveCount(4, { timeout: 60_000 })
  await expect(page.getByText('Recording · 14.0s')).toBeVisible()
  // Reorder: move the recording to the front, then rename it.
  const cards = page.getByTestId('asset-card')
  await cards.nth(3).getByRole('button', { name: 'Move earlier' }).click()
  await expect(cards.nth(2).getByText('Recording · 14.0s')).toBeVisible()
  await shot(page, '01-upload')

  await page.getByRole('button', { name: /Continue to Describe/ }).click()
  await page.getByLabel('Product name').fill('Tallyfox')
  await page.getByLabel('Brief description').fill(
    'Tallyfox is invoicing software for freelancers. You can create an invoice, add line items, and send it to a client from one screen.',
  )
  await page.getByLabel('Intended audience').fill('Freelancers and small studios')
  await page.getByLabel('Selling point 1').fill('Create and send an invoice in one screen')
  await page.getByLabel('Selling point 2').fill('See outstanding and overdue totals at a glance')
  await page.getByLabel('Call to action').fill('Start free today')
  await expect(page.getByText('All changes saved')).toBeVisible()
  await shot(page, '02-describe')

  await page.getByRole('button', { name: /Continue to Review/ }).click()
  await page.getByRole('button', { name: 'Generate story' }).click()
  await expect(page.getByTestId('scene-card').first()).toBeVisible({ timeout: 90_000 })
  const n = await page.getByTestId('scene-card').count()
  expect(n).toBeGreaterThanOrEqual(3)

  // Edit the second scene's headline and confirm autosave creates a new revision.
  await page.getByTestId('scene-card').nth(1).locator('.scene-main').click()
  const headline = page.getByTestId('inspector').getByLabel('Headline')
  await headline.fill('Edited headline — “quotes” & ünïcödé ✓')
  await expect(page.getByText('All changes saved')).toBeVisible()
  await expect(page.getByTestId('scene-card').nth(1)).toContainText('Edited headline')
  await expect(page.getByTestId('scene-card').nth(1)).toContainText('edited')
  await shot(page, '03-review')

  // Reload: edits persist.
  await page.reload()
  await expect(page.getByTestId('scene-card').nth(1)).toContainText('Edited headline')

  await page.getByRole('button', { name: /Continue to Export/ }).click()
  await page.getByRole('button', { name: /Render draft preview/ }).click()
  await expect(page.getByText('Synthesising narration').first()).toBeVisible()
  const video = page.getByTestId('export-video')
  await expect(video).toBeVisible({ timeout: 180_000 })
  // Playback: Playwright's open-source Chromium ships without H.264/AAC decoders, so real
  // playback is asserted only when the browser supports the codec (Chrome, Edge, Safari do).
  const h264 = await video.evaluate((v: HTMLVideoElement) => v.canPlayType('video/mp4; codecs="avc1.640028"'))
  if (h264) {
    await expect
      .poll(async () => video.evaluate((v: HTMLVideoElement) => (v.readyState >= 1 ? v.duration : 0)), { timeout: 30_000 })
      .toBeGreaterThan(10)
    await video.evaluate((v: HTMLVideoElement) => {
      v.muted = true
      return v.play()
    })
    await expect.poll(async () => video.evaluate((v: HTMLVideoElement) => v.currentTime), { timeout: 15_000 }).toBeGreaterThan(0.3)
  } else {
    test.info().annotations.push({ type: 'note', description: 'Browser lacks H.264; verified byte-range seeking instead of playback.' })
  }
  // Seeking support: the media endpoint answers byte-range requests with 206.
  const src = await video.getAttribute('src')
  const ranged = await page.request.get(src!, { headers: { Range: 'bytes=0-1023' } })
  expect(ranged.status()).toBe(206)
  expect(ranged.headers()['content-type']).toBe('video/mp4')
  expect((await ranged.body()).length).toBe(1024)
  await shot(page, '04-export')

  const [download] = await Promise.all([page.waitForEvent('download'), page.getByRole('link', { name: 'Download MP4' }).click()])
  const dest = path.join(test.info().outputDir, download.suggestedFilename())
  await download.saveAs(dest)
  const head = fs.readFileSync(dest).subarray(4, 8).toString('latin1')
  expect(head).toBe('ftyp')
  expect(fs.statSync(dest).size).toBeGreaterThan(100_000)

  // Captions and storyboard downloads are available too.
  const [srt] = await Promise.all([page.waitForEvent('download'), page.getByRole('link', { name: /Captions/ }).click()])
  const srtPath = path.join(test.info().outputDir, srt.suggestedFilename())
  await srt.saveAs(srtPath)
  expect(fs.readFileSync(srtPath, 'utf8')).toContain('-->')
})
