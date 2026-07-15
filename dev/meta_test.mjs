/**
 * Vérifie les paramètres d'état configurables (meta_schema) : types number /
 * bool / enum, nommage automatique, éditeur, validation backend.
 * Exige un serveur lancé avec --no-seed (store vierge -> schéma par défaut).
 *
 *   node dev/meta_test.mjs [http://127.0.0.1:8101/]
 */
import { JSDOM, VirtualConsole } from 'jsdom';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const INDEX = path.join(HERE, '..', 'rf_lab', 'www', 'index.html');
const BASE = process.argv[2] || 'http://127.0.0.1:8101/';

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', e => errors.push(e.message));

const dom = new JSDOM(fs.readFileSync(INDEX, 'utf8'), {
  url: BASE, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
  beforeParse(w) { w.fetch = (u, o) => fetch(new URL(u, w.location.href), o); },
});
const { window } = dom;
window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
window.HTMLDialogElement.prototype.close = function (v) {
  this.open = false; this.returnValue = v ?? this.returnValue;
  this.dispatchEvent(new window.Event('close'));
};
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
const post = (p, b) => fetch(new URL(p, BASE), {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) });

// --- 1. schéma par défaut = les paramètres réels de la RF00234
const def = await fetch(new URL('api/meta-schema', BASE)).then(r => r.json());
const keys = def.meta_schema.map(f => f.key);
check('schéma par défaut = les 8 paramètres de la RF00234',
  JSON.stringify(keys) === JSON.stringify(
    ['light', 'lum', 'cct', 'fan', 'speed', 'reverse', 'mode', 'timer']),
  keys.join(', '));
// nuit et éco s'excluent dans la trame (bits 42-43, un champ à 3 valeurs) : les
// modéliser en deux booléens laissait saisir « nuit ET éco », état impossible.
check('mode moteur = un enum à 3 valeurs, pas deux booléens',
  (() => { const f = def.meta_schema.find(x => x.key === 'mode');
           return f?.type === 'enum' &&
                  JSON.stringify(f.options) === '["normal","nuit","eco"]'; })(),
  JSON.stringify(def.meta_schema.find(x => x.key === 'mode')?.options));
check('plus de booléens nuit/eco séparés',
  !keys.includes('nuit') && !keys.includes('eco'));
check('light / fan sont des bool « always » (éteint est un état, pas une absence)',
  ['light', 'fan'].every(k => {
    const f = def.meta_schema.find(x => x.key === k);
    return f.type === 'bool' && f.always === true;
  }));
check('timer est un enum 0/1/2/4/8',
  JSON.stringify(def.meta_schema.find(f => f.key === 'timer').options) === '[0,1,2,4,8]');
check('reverse est un bool', def.meta_schema.find(f => f.key === 'reverse').type === 'bool');

await wait(() => $('meta-inputs').querySelector('input, select'), 'panneau de capture');

// --- 2. l'UI génère le bon widget par type
check('number -> input[type=number]', $('c-lum')?.type === 'number');
check('bool -> checkbox', $('c-reverse')?.type === 'checkbox');
check('enum -> select avec les options', $('c-timer')?.tagName === 'SELECT' &&
  [...$('c-timer').options].map(o => o.value).join(',') === '0,1,2,4,8',
  [...($('c-timer')?.options || [])].map(o => o.value).join(','));
check('tri proposé sur les 8 paramètres',
  [...$('sort').options].map(o => o.value).join(',') ===
    'name,light,lum,cct,fan,speed,reverse,mode,timer',
  [...$('sort').options].map(o => o.value).join(','));

// --- 3. nommage auto : on/off toujours présents, le reste au repos disparaît
$('c-light').checked = true; $('c-fan').checked = true;
$('c-lum').value = '10'; $('c-cct').value = '3000'; $('c-speed').value = '0';
$('cap-auto').dispatchEvent(new window.Event('click'));
check('nom auto au repos', $('c-name').value === 'light1_lum10_cct3000_fan1_v0',
  $('c-name').value);

// --- 3b. un on/off à OFF doit rester lisible dans le nom
$('c-light').checked = false;
$('cap-auto').dispatchEvent(new window.Event('click'));
check('lumière éteinte visible dans le nom (light0, pas une disparition)',
  $('c-name').value === 'light0_lum10_cct3000_fan1_v0', $('c-name').value);
$('c-light').checked = true;

// --- 4. nommage auto : les paramètres actifs s'ajoutent
$('c-reverse').checked = true;
$('c-timer').value = '4';
$('cap-auto').dispatchEvent(new window.Event('click'));
check('nom auto avec reverse + timer',
  $('c-name').value === 'light1_lum10_cct3000_fan1_v0_rev1_t4', $('c-name').value);

// --- 5. une capture enregistre bien bool et enum
$('cap').dispatchEvent(new window.Event('click'));
await wait(() => /enregistrée|Rien reçu|erreur/i.test($('cap-msg').textContent), 'capture', 20000);
check('capture enregistrée', /enregistrée/.test($('cap-msg').textContent),
  $('cap-msg').textContent.trim().slice(0, 46));
const rows = await fetch(new URL('api/analyze?gap=2000&mode=pwm', BASE)).then(r => r.json());
const m = rows.rows[0]?.meta || {};
check('bool stocké en booléen, enum en nombre',
  m.reverse === true && m.timer === 4 && m.lum === 10 &&
  m.light === true && m.fan === true,
  JSON.stringify(m));
// la grille se redessine de façon asynchrone après la capture
await wait(() => /light on/.test($('grid-wrap').innerHTML), 'grille redessinée');
check('chips : light/fan affichent on ET off, pas seulement on',
  /light on/.test($('grid-wrap').innerHTML) && /fan on/.test($('grid-wrap').innerHTML),
  'chips « light on » / « fan on »');

// --- 6. un number vide bloque la capture, un bool non coché ne bloque pas
$('c-lum').value = '';
$('cap').dispatchEvent(new window.Event('click'));
await sleep(250);
check('number manquant -> capture refusée',
  !!$('cap-msg').querySelector('.msg.warn') && /Luminosité/.test($('cap-msg').textContent),
  $('cap-msg').textContent.trim().slice(0, 52));

// --- 7. l'éditeur ajoute un paramètre
$('meta-edit').dispatchEvent(new window.Event('click'));
await sleep(150);
check('éditeur ouvert avec une ligne par paramètre',
  $('meta-dlg').open && $('meta-rows').querySelectorAll('.row').length === 8,
  $('meta-rows').querySelectorAll('.row').length + ' lignes');

$('meta-add').dispatchEvent(new window.Event('click'));
await sleep(100);
const last = [...$('meta-rows').querySelectorAll('.row')].at(-1);
last.querySelector('.m-key').value = 'boost';
last.querySelector('.m-label').value = 'Boost';
last.querySelector('.m-short').value = 'b';
last.querySelector('.m-type').value = 'bool';
$('meta-save').dispatchEvent(new window.Event('click'));
await wait(() => !$('meta-dlg').open, 'sauvegarde du schéma');
await sleep(500);
check('paramètre ajouté et persisté', !!$('c-boost'), 'input c-boost présent');
const after = await fetch(new URL('api/meta-schema', BASE)).then(r => r.json());
check('schéma relu depuis le backend contient boost',
  after.meta_schema.some(f => f.key === 'boost' && f.type === 'bool'));

// --- 8. une capture antérieure n'a pas la nouvelle clé : ça doit se VOIR
await wait(() => /boost \?/.test($('grid-wrap').innerHTML), 'chip boost ?');
check('paramètre manquant signalé en jaune sur les anciennes captures',
  /boost \?/.test($('grid-wrap').innerHTML), 'chip « boost ? »');

// --- 9. validation backend
const bad1 = await post('api/meta-schema', { meta_schema: [] });
check('schéma vide rejeté', bad1.status === 400, `HTTP ${bad1.status}`);
const bad2 = await post('api/meta-schema', { meta_schema: [{ key: 'x', type: 'wat' }] });
check('type inconnu rejeté', bad2.status === 400, `HTTP ${bad2.status}`);
const bad3 = await post('api/meta-schema', { meta_schema: [{ key: 'x', type: 'enum' }] });
check('enum sans options rejeté', bad3.status === 400, `HTTP ${bad3.status}`);
const bad4 = await post('api/meta-schema', { meta_schema: [{ type: 'number' }] });
check('paramètre sans clé rejeté', bad4.status === 400, `HTTP ${bad4.status}`);

// --- 10. validation côté UI : clés dupliquées
$('meta-edit').dispatchEvent(new window.Event('click'));
await sleep(150);
$('meta-rows').querySelector('.row .m-key').value = 'cct';   // light -> cct : doublon
$('meta-save').dispatchEvent(new window.Event('click'));
await sleep(300);
check('clés dupliquées refusées par l\'UI',
  $('meta-dlg').open && /même clé/.test($('meta-dlg-msg').textContent),
  $('meta-dlg-msg').textContent.trim());

check('pas d\'erreur JS', errors.length === 0, errors.join(' '));
console.log(`\n${fail ? '✗ ÉCHEC' : '✓ OK'} — ${pass} passés, ${fail} échoués`);
process.exit(fail ? 1 : 0);
