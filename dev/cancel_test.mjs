/**
 * Vérifie l'annulation de capture, de bout en bout dans l'UI.
 * Exige un serveur lancé avec FAKE_LATENCY_POLLS élevé (personne n'appuie).
 *
 *   node dev/cancel_test.mjs [http://127.0.0.1:8099/]
 */
import { JSDOM, VirtualConsole } from 'jsdom';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const INDEX = path.join(HERE, '..', 'rf_lab', 'www', 'index.html');
const BASE = process.argv[2] || 'http://127.0.0.1:8099/';

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', e => errors.push(e.message));

const dom = new JSDOM(fs.readFileSync(INDEX, 'utf8'), {
  url: BASE, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
  beforeParse(w) { w.fetch = (u, o) => fetch(new URL(u, w.location.href), o); },
});
const { window } = dom;
window.URL.createObjectURL = () => 'blob:stub';

const $ = (id) => window.document.getElementById(id);
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const wait = async (fn, label, ms = 15000) => {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) { if (fn()) return true; await sleep(60); }
  throw new Error('timeout: ' + label);
};
let pass = 0, fail = 0;
const check = (n, c, x = '') => { c ? pass++ : fail++; console.log(`  ${c ? '✓' : '✗'} ${n}${x ? '  — ' + x : ''}`); };

await wait(() => $('grid-wrap').querySelector('table.grid, .empty'), 'page prête');

// --- bouton Annuler caché au repos
check('bouton Annuler caché au repos', $('cap-cancel').hidden);

// --- lance une capture que personne ne satisfera
$('c-lum').value = '10'; $('c-cct').value = '3000'; $('c-speed').value = '0';
$('cap').dispatchEvent(new window.Event('click'));
await wait(() => /Appuie sur la touche/.test($('cap-msg').textContent), 'écoute');
check('bouton Annuler visible pendant la capture', !$('cap-cancel').hidden);
check('bouton Capturer désactivé pendant la capture', $('cap').disabled);

const st = await fetch(new URL('api/capture/poll', BASE)).then(r => r.json());
check('backend en écoute', st.state === 'listening', st.state);

// --- une 2e capture doit être refusée (409), pas se superposer
const dup = await fetch(new URL('api/capture/start', BASE), { method: 'POST' });
check('capture concurrente refusée en 409', dup.status === 409, `HTTP ${dup.status}`);

// --- annule via le bouton
const t0 = Date.now();
$('cap-cancel').dispatchEvent(new window.Event('click'));
await wait(() => /annulée/i.test($('cap-msg').textContent), 'annulation');
const ms = Date.now() - t0;
check('annulation prise en compte', /Capture annulée/.test($('cap-msg').textContent),
  $('cap-msg').textContent.trim());
check('annulation rapide (< 3s)', ms < 3000, `${ms} ms`);
check('bouton Annuler re-caché', $('cap-cancel').hidden);
check('bouton Capturer réactivé', !$('cap').disabled);

const after = await fetch(new URL('api/capture/poll', BASE)).then(r => r.json());
check('état backend = cancelled', after.state === 'cancelled', after.state);

// --- le verrou doit être relâché : une capture doit repartir
const again = await fetch(new URL('api/capture/start', BASE), { method: 'POST' });
check('verrou relâché, nouvelle capture acceptée', again.status === 200, `HTTP ${again.status}`);
await fetch(new URL('api/capture/cancel', BASE), { method: 'POST' });
await sleep(900);

// --- annuler quand rien ne tourne ne doit pas exploser
const noop = await fetch(new URL('api/capture/cancel', BASE), { method: 'POST' });
const noopBody = await noop.json();
check('annuler à vide est sans effet', noop.status === 200 && noopBody.ok === true,
  JSON.stringify(noopBody));

check('pas d\'erreur JS', errors.length === 0, errors.join(' '));
console.log(`\n${fail ? '✗ ÉCHEC' : '✓ OK'} — ${pass} passés, ${fail} échoués`);
process.exit(fail ? 1 : 0);
