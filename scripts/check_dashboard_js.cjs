'use strict';
// Compile every inline dashboard script without executing browser or API calls.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'radar/static/pages.html'), 'utf8');
if (!html.includes('/* PREFLIGHT_CARD_SYNC_V2: compiled */')) {
  throw new Error('Preflight card sync module was not assembled into the dashboard');
}
let checked = 0;
for (const match of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script\s*>/gi)) {
  const [, attrs, code] = match;
  if (!code.trim() || /\bsrc\s*=/i.test(attrs) || /application\/(?:ld\+)?json/i.test(attrs)) continue;
  new vm.Script(code, {filename: 'pages-inline-' + checked + '.js'});
  checked++;
}
if (!checked) throw new Error('No inline JavaScript found');
new vm.Script(fs.readFileSync(path.join(root, 'radar/static/service-worker.js'), 'utf8'), {filename: 'service-worker.js'});
console.log('PASS: ' + checked + ' assembled dashboard scripts and service worker syntax');
