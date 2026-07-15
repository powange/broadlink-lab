/**
 * Pilote la vraie www/index.html dans un DOM headless, contre un serveur lancé
 * par dev/serve.py. Vérifie la chaîne complète : état → grille → nommage de
 * champs → génération → émission → export.
 *
 *   node dev/ui_test.mjs [http://127.0.0.1:8099/]
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
vc.on('jsdomError', e => errors.push('jsdomError: ' + e.message));
vc.on('error', (...a) => errors.push('console.error: ' + a.join(' ')));

const dom = new JSDOM(fs.readFileSync(INDEX, 'utf8'), {
  url: BASE, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
  // jsdom n'implémente pas fetch : on branche celui de node. Résoudre les URL
  // contre window.location valide au passage que les chemins relatifs de la
  // page survivent à un préfixe d'ingress.
  beforeParse(w) { w.fetch = (u, o) => fetch(new URL(u, w.location.href), o); },
});
const { window } = dom;

// jsdom n'implémente ni <dialog> ni les URL de blob
window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
window.HTMLDialogElement.prototype.close = function (v) {
  this.open = false; this.returnValue = v ?? this.returnValue;
  this.dispatchEvent(new window.Event('close'));
};
window.URL.createObjectURL = () => 'blob:stub';
window.confirm = () => true;

const $ = (id) => window.document.getElementById(id);
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const wait = async (fn, label, ms = 15000) => {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) { if (fn()) return true; await sleep(60); }
  throw new Error('timeout: ' + label);
};

let pass = 0, fail = 0;
const check = (name, cond, extra = '') => {
  cond ? pass++ : fail++;
  console.log(`  ${cond ? '✓' : '✗'} ${name}${extra ? '  — ' + extra : ''}`);
};

await wait(() => $('grid-wrap').querySelector('table.grid'), 'grille rendue');
await sleep(400);

// --- 1. barre d'état
check("barre d'état : RM4 connecté",
  /RM4 Pro/.test($('conn').textContent) && !!$('conn').querySelector('.dot.on'),
  $('conn').textContent.trim());

// --- 2. grille de bits
const gridTable = $('grid-wrap').querySelector('table.grid');   // la 2e table = valeurs décodées
const rows = gridTable.querySelectorAll('tbody tr');
const bits0 = [...rows[0].querySelectorAll('td.bit')].map(td => td.textContent).join('');
check('grille : 7 captures', rows.length === 7, `${rows.length} lignes`);
check('grille : 24 colonnes', rows[0].querySelectorAll('td.bit').length === 24);
check('grille : bits intacts', /^[01]{24}$/.test(bits0), bits0);

const vary = [...new Set([...$('grid-wrap').querySelectorAll('td.bit.vary')]
  .map(td => +td.dataset.col))].sort((a, b) => a - b);
// lum[8:12] -> 10,11 ; cct[12:14] -> 12,13 ; speed[14:17] -> 15,16.
// id[0:8] et fixe[17:24] ne doivent JAMAIS apparaître.
check('grille : colonnes variables = champs qui bougent, ID exclu',
  JSON.stringify(vary) === JSON.stringify([10, 11, 12, 13, 15, 16]), `[${vary}]`);

// --- 3. tri par métadonnée
$('sort').value = 'lum';
$('sort').dispatchEvent(new window.Event('change'));
await sleep(200);
const lums = [...$('grid-wrap').querySelector('table.grid')
  .querySelectorAll('tbody tr .lbl .mono')].map(e => +e.textContent.match(/lum(\d+)/)[1]);
check('tri par luminosité croissante',
  lums.every((v, i, a) => i === 0 || a[i - 1] <= v), `[${lums}]`);

// --- 4. nommage d'un champ au clic-glisser
const cell = (c) => $('grid-wrap').querySelector(`td.bit[data-col="${c}"]`);
const mouse = (el, t) => el.dispatchEvent(new window.MouseEvent(t, { bubbles: true, view: window }));
mouse(cell(8), 'mousedown'); mouse(cell(10), 'mousemove'); mouse(cell(11), 'mousemove');
const sel = $('grid-wrap').querySelectorAll('td.bit.sel').length;
window.dispatchEvent(new window.MouseEvent('mouseup', { bubbles: true }));
check('clic-glisser sélectionne les colonnes 8→11', sel === 4 * 7, `${sel} cellules (4 col × 7 lignes)`);
check('dialogue ouvert sur la bonne tranche',
  $('dlg').open && $('dlg-range').textContent === '[8, 12) — 4 bits', $('dlg-range').textContent);
$('f-name').value = 'lum';
$('dlg').close('ok');
await sleep(700);
check('champ persisté via POST /api/fields', $('fields-box').textContent.includes('lum'));

// --- 5. valeurs décodées : le pattern doit devenir lisible d'un coup d'œil
const html = $('grid-wrap').innerHTML;
check('lum10 → 0001 = 1', /lum <b class="mono">0001<\/b> = 1/.test(html));
check('lum20 → 0010 = 2', /lum <b class="mono">0010<\/b> = 2/.test(html));
check('lum30 → 0011 = 3', /lum <b class="mono">0011<\/b> = 3/.test(html));

// --- 6. générateur
await wait(() => $('sliders').querySelector('input[type=range]'), 'sliders');
const lumS = [...$('sliders').querySelectorAll('input[type=range]')].find(s => s.dataset.f === 'lum');
check('slider lum borné à 2^4-1', lumS.max === '15');
lumS.value = '7';
lumS.dispatchEvent(new window.Event('input'));
check('valeur du slider affichée', $('v-lum').textContent.includes('7'));

$('gen').dispatchEvent(new window.Event('click'));
await wait(() => $('gen-out').querySelector('pre'), 'génération');
check('génération vérifiée', !!$('gen-out').querySelector('.msg.ok'),
  $('gen-out').querySelector('.msg')?.textContent.trim().slice(0, 52));
check('diff visuel', $('gen-out').querySelectorAll('.diff b').length === 2,
  $('gen-out').querySelectorAll('.diff b').length + ' bits (1→7 = 0001→0111)');
check('bouton Émettre débloqué', !$('send').disabled);

// --- 7. émission
$('send').dispatchEvent(new window.Event('click'));
await wait(() => /Émis/.test($('gen-out').textContent), 'émission');
check('émission OK', /Observe le ventilo/.test($('gen-out').textContent));

// le faux RM4 décode ce qu'il reçoit : on vérifie que lum=7 est bien parti sur les ondes
const sent = await fetch(new URL('api/status', BASE)).then(r => r.json());
check('RM4 toujours joignable après émission', sent.connected === true);

// --- 8. capture live (le faux RM4 rejoue la séquence de §9)
$('c-lum').value = '40'; $('c-cct').value = '3000'; $('c-speed').value = '0';
$('cap').dispatchEvent(new window.Event('click'));
await wait(() => /Capturée et enregistrée|Rien reçu|erreur/i.test($('cap-msg').textContent),
  'capture', 20000);
check('capture live enregistrée via le polling',
  /Capturée et enregistrée/.test($('cap-msg').textContent),
  $('cap-msg').textContent.trim());
await sleep(400);
check('la nouvelle capture apparaît dans la grille',
  $('grid-wrap').querySelector('table.grid').querySelectorAll('tbody tr').length === 8,
  $('grid-wrap').querySelector('table.grid').querySelectorAll('tbody tr').length + ' lignes');

// --- 9. métadonnées obligatoires (sans elles le diff ne veut rien dire, §6.2)
$('c-lum').value = ''; $('c-cct').value = ''; $('c-speed').value = '';
$('cap').dispatchEvent(new window.Event('click'));
await sleep(200);
check('capture refusée sans métadonnées',
  !!$('cap-msg').querySelector('.msg.warn'), $('cap-msg').textContent.trim().slice(0, 48));

// --- 10. export JSON
$('exp-json').dispatchEvent(new window.Event('click'));
await sleep(200);
const map = JSON.parse($('exp-out').querySelector('pre').textContent);
check('export JSON : champs + correspondance observée',
  map.fields.length >= 1 && map.observed.length === 8 && map.observed[0].values.lum !== undefined,
  `${map.fields.length} champ(s), ${map.observed.length} captures`);

console.log(`\n${fail || errors.length ? '✗ ÉCHEC' : '✓ OK'} — ${pass} passés, ${fail} échoués`);
if (errors.length) { console.log('\nErreurs JS :'); errors.forEach(e => console.log('  ' + e)); }
process.exit(fail || errors.length ? 1 : 0);
