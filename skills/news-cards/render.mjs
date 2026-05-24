#!/usr/bin/env node
import puppeteer from 'puppeteer';
import { readFileSync, existsSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';
import { execSync } from 'child_process';

const __dirname = dirname(fileURLToPath(import.meta.url));

const args = process.argv.slice(2);
function getArg(name, fallback) {
  const idx = args.indexOf(`--${name}`);
  if (idx === -1 || idx + 1 >= args.length) return fallback;
  return args[idx + 1];
}

const template = getArg('template', 'dark-editorial');
const category = getArg('category', 'Breaking');
const headline = getArg('headline', '');
const highlight = getArg('highlight', '');
const subline = getArg('subline', '');
const output = getArg('output', '');
const source = getArg('source', 'Gen AI Spotlight');
const date = getArg('date', new Date().toLocaleDateString('en-US', { month: 'short', year: 'numeric' }));
const statLabels = getArg('stat-labels', 'Stat 1,Stat 2,Stat 3');
const statValues = getArg('stat-values', 'N/A,N/A,N/A');

if (!headline || !output) {
  console.error('Usage: node render.mjs --template <name> --headline "..." --subline "..." --output <path>');
  console.error('Templates: dark-editorial (default), classic');
  process.exit(1);
}

const templatePath = join(__dirname, 'templates', `${template}.html`);
if (!existsSync(templatePath)) {
  console.error(`Template not found: ${templatePath}`);
  process.exit(1);
}

let html = readFileSync(templatePath, 'utf8');

let headlineHtml = headline;
if (highlight) {
  const words = highlight.split(',').map(w => w.trim()).filter(w => w);
  let result = headline;
  for (const word of words) {
    const esc = word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    result = result.replace(new RegExp('(' + esc + ')([\'.,!?:;]*)', 'gi'), (_, match, punct) => `<span class="highlight">${match}${punct}</span>`);
  }
  headlineHtml = result;
}

const labels = statLabels.split(',');
const values = statValues.split(',');

html = html
  .replace('{{CATEGORY}}', category)
  .replace('{{HEADLINE}}', headlineHtml)
  .replace('{{SUBLINE}}', subline)
  .replace('{{SOURCE}}', source)
  .replace('{{DATE}}', date)
  .replace('{{STAT_LABEL_1}}', labels[0] || '')
  .replace('{{STAT_VALUE_1}}', values[0] || '')
  .replace('{{STAT_LABEL_2}}', labels[1] || '')
  .replace('{{STAT_VALUE_2}}', values[1] || '')
  .replace('{{STAT_LABEL_3}}', labels[2] || '')
  .replace('{{STAT_VALUE_3}}', values[2] || '');

// Audit with Impeccable (non-blocking)
try {
  const tmpHtml = '/tmp/_news_card_audit.html';
  const { writeFileSync } = await import('fs');
  writeFileSync(tmpHtml, html);
  const result = execSync(`npx impeccable detect ${tmpHtml} 2>&1`, { encoding: 'utf8', timeout: 15000 });
  if (result.includes('anti-pattern')) {
    console.log('⚠️  Impeccable audit:');
    console.log(result.trim());
  } else {
    console.log('✅ Impeccable: no issues found');
  }
} catch (e) {
  if (e.stdout && e.stdout.includes('anti-pattern')) {
    console.log('⚠️  Impeccable audit:');
    console.log(e.stdout.trim());
  }
}

// Render
const browser = await puppeteer.launch({ headless: true, args: ['--no-sandbox'] });
const page = await browser.newPage();
await page.setViewport({ width: 1280, height: 720 });
await page.setContent(html, { waitUntil: 'networkidle0', timeout: 15000 });
await page.waitForSelector('.card', { timeout: 5000 });
// Ensure page captures all content by sizing to content height
const contentHeight = await page.evaluate(() => {
  document.body.style.height = Math.max(document.body.scrollHeight, 720) + 'px';
  return document.body.scrollHeight;
});
await page.setViewport({ width: 1280, height: Math.max(contentHeight, 720) });
await page.screenshot({ path: output, type: 'png', fullPage: true });
await browser.close();

console.log(`✅ Rendered: ${output}`);
