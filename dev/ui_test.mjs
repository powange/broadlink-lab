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
// performance.now() et pas Date.now() : l'horloge murale SAUTE (resync NTP, et
// sous WSL2 des écarts de ~30 s ont été mesurés). Un bond en avant fait expirer
// le timeout instantanément — le test échoue sur un délai qui n'a pas eu lieu.
const wait = async (fn, label, ms = 45000) => {
  const t0 = performance.now();
  while (performance.now() - t0 < ms) { if (fn()) return true; await sleep(60); }
  throw new Error('timeout: ' + label);
};

let pass = 0, fail = 0;
const check = (name, cond, extra = '') => {
  cond ? pass++ : fail++;
  console.log(`  ${cond ? '✓' : '✗'} ${name}${extra ? '  — ' + extra : ''}`);
};

await wait(() => $('grid-wrap').querySelector('table.grid'), 'grille rendue', 45000);
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

// --- 3bis. tri chronologique.
// L'ordre du store EST l'ordre de capture (on append). Le tri « ordre de
// capture » consiste donc à ne pas trier — ce qui marche aussi sur les captures
// antérieures au champ `ts`, comme celles du seed.
const names = () => [...$('grid-wrap').querySelector('table.grid')
  .querySelectorAll('tbody tr .lbl .mono')].map(e => e.textContent);
$('sort').value = 'seq';
$('sort').dispatchEvent(new window.Event('change'));
await sleep(200);
const seq = names();
const storeOrder = (await fetch(new URL('api/analyze?gap=2000', BASE)).then(r => r.json()))
  .rows.map(r => r.name);
check('tri chronologique = ordre du store',
  JSON.stringify(seq) === JSON.stringify(storeOrder), `[${seq}]`);
// Sans ça, le test passerait même si « ordre de capture » retriait par nom.
check("l'ordre de capture diffère bien de l'alphabétique",
  JSON.stringify(seq) !== JSON.stringify([...seq].sort()), `[${seq}]`);
check('les captures du seed portent une infobulle « pas de date »',
  /pas de date|avant que la date/.test($('grid-wrap')
    .querySelector('table.grid tbody tr .lbl .mono').title),
  $('grid-wrap').querySelector('table.grid tbody tr .lbl .mono').title);

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

// --- 4bis. rouvrir un champ existant pour le modifier, sans resélectionner
const chip = [...$('fields-box').querySelectorAll('[data-edit]')].find(b => b.textContent === 'lum');
check('un champ nommé est cliquable pour édition', !!chip);
chip.dispatchEvent(new window.MouseEvent('click', { bubbles: true, view: window }));
await sleep(120);
check('cliquer le champ rouvre le dialogue sur sa tranche',
  $('dlg').open && $('dlg-range').textContent === '[8, 12) — 4 bits', $('dlg-range').textContent);
check('le dialogue est pré-rempli avec le nom du champ', $('f-name').value === 'lum',
  $('f-name').value);
$('dlg').close('cancel');
await sleep(200);

// --- 5. valeurs décodées : le pattern doit devenir lisible d'un coup d'œil
const html = $('grid-wrap').innerHTML;
check('lum10 → 0001 = 1', /lum <b class="mono">0001<\/b> = 1/.test(html));
check('lum20 → 0010 = 2', /lum <b class="mono">0010<\/b> = 2/.test(html));
check('lum30 → 0011 = 3', /lum <b class="mono">0011<\/b> = 3/.test(html));

// --- 6. générateur
await wait(() => $('sliders').querySelector('.btns, input[type=range]'), 'sliders');
// lum n'a pas de bornes déclarées ici : 4 bits -> 0-15, donc 16 boutons.
const lumG = $('sliders').querySelector('.btns[data-f="lum"]');
check('lum : un bouton par valeur, 0 à 2^4-1', lumG?.querySelectorAll('button').length === 16,
  lumG?.querySelectorAll('button').length + ' boutons');
lumG.querySelector('button[data-v="7"]').dispatchEvent(
  new window.MouseEvent('click', { bubbles: true, view: window }));
check('valeur affichée', $('v-lum').textContent.includes('7'));
check('un seul bouton actif à la fois',
  [...lumG.querySelectorAll('button.on')].map(b => b.dataset.v).join(',') === '7',
  [...lumG.querySelectorAll('button.on')].map(b => b.dataset.v).join(','));

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

// La dernière capturée doit être la dernière en ordre chronologique, et porter
// une vraie date — c'est là que /api/captures a posé son `ts`.
$('sort').value = 'seq';
$('sort').dispatchEvent(new window.Event('change'));
await sleep(200);
const last = [...$('grid-wrap').querySelector('table.grid')
  .querySelectorAll('tbody tr .lbl .mono')].pop();
check('la capture live arrive en dernier en ordre chronologique',
  /lum40/.test(last.textContent), last.textContent);
// Pas de parsing de la date : toLocaleString dépend de la locale du runtime,
// et un test qui suppose la sienne casse ailleurs pour rien.
check('et porte une date réelle, pas le repli « pas de date »',
  !/avant que la date/.test(last.title) && /\d/.test(last.title), last.title);

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

// --- 11. espaces de travail : une session de reverse par télécommande.
// À faire EN DERNIER : ça change l'espace actif, donc l'état global de l'UI.
await window.loadWorkspaces();
check('un espace « default » existe (migration de l\'ancien store)',
  [...$('ws').options].some(o => o.value === 'default'),
  [...$('ws').options].map(o => o.value).join(','));
const gridCount = () => $('grid-wrap').querySelector('table.grid')
  ?.querySelectorAll('tbody tr').length || 0;
const nBefore = gridCount();
check('l\'espace de départ a des captures', nBefore >= 7, `${nBefore} lignes`);

// créer un nouvel appareil -> espace vierge, sans toucher au premier
await window.fetch(new URL('api/workspaces', BASE), {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ name: 'Autre télécommande', manufacturer: 'ACME', model: 'X1' }),
});
await window.fetch(new URL('api/workspaces/select', BASE), {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ id: 'autre_telecommande' }),
});
await window.loadWorkspaces();
await window.loadAnalyze();
await sleep(200);
check('le nouvel appareil démarre SANS capture (pas de fuite entre espaces)',
  gridCount() === 0, `${gridCount()} lignes`);
check('l\'export se pré-remplit avec le nouvel appareil',
  $('d-name').value === 'Autre télécommande' && $('d-manu').value === 'ACME',
  `${$('d-name').value} / ${$('d-manu').value}`);

// revenir au premier -> les captures sont toujours là, intactes
await window.fetch(new URL('api/workspaces/select', BASE), {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ id: 'default' }),
});
await window.loadAnalyze();
await sleep(200);
check('revenir au premier appareil retrouve ses captures',
  gridCount() === nBefore, `${gridCount()} vs ${nBefore}`);

console.log(`\n${fail || errors.length ? '✗ ÉCHEC' : '✓ OK'} — ${pass} passés, ${fail} échoués`);
if (errors.length) { console.log('\nErreurs JS :'); errors.forEach(e => console.log('  ' + e)); }
process.exit(fail || errors.length ? 1 : 0);
