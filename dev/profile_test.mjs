/**
 * Exerce le profil d'appareil — le livrable du labo, et l'entrée de RF Bridge.
 *
 *   node dev/profile_test.mjs [http://127.0.0.1:8099/]
 *
 * Le profil mélange deux choses : le savoir sur le MODÈLE (carte des bits,
 * checksum, entités), partageable, et la capture de référence, qui porte l'ID
 * appairé d'UNE télécommande. D'où l'import avec ou sans réancrage.
 */
import { JSDOM, VirtualConsole } from 'jsdom';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const INDEX = path.join(HERE, '..', 'rf_lab', 'www', 'index.html');
const OUT = path.join(HERE, '.out.profile.json');
const BASE = process.argv[2] || 'http://127.0.0.1:8099/';

const post = (p, b) => fetch(new URL(p, BASE), {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) });

// La carte des champs et les captures viennent de --seed-real (dev/real_seed.py).

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
const wait = async (fn, label, ms = 20000) => {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) { if (fn()) return true; await sleep(80); }
  throw new Error('timeout: ' + label);
};
let pass = 0, fail = 0;
const check = (n, c, x = '') => { c ? pass++ : fail++; console.log(`  ${c ? '✓' : '✗'} ${n}${x ? '  — ' + x : ''}`); };

await wait(() => $('sliders').querySelector('input[type=range]'), 'sliders');

// --- sliders bornés aux valeurs réelles, pas à la largeur des bits
const sl = Object.fromEntries([...$('sliders').querySelectorAll('input[type=range]')]
  .map(s => [s.dataset.f, [s.min, s.max]]));
check('un slider par champ data, const exclus',
  Object.keys(sl).sort().join(',') === 'cct,fan,light,lum,mode,reverse,speed,timer',
  Object.keys(sl).join(','));
check('lum borné 1-11, pas 0-15 (bornes réelles)', sl.lum?.join('-') === '1-11', sl.lum?.join('-'));
check('cct borné 1-7', sl.cct?.join('-') === '1-7', sl.cct?.join('-'));
check('speed borné 1-8', sl.speed?.join('-') === '1-8', sl.speed?.join('-'));
check('timer 0-255 (8 bits, pas seulement 1/2/4/8 h)', sl.timer?.join('-') === '0-255', sl.timer?.join('-'));

// --- construction du profil : le livrable du labo
$('d-name').value = 'Mantra Nenufar';
$('d-manu').value = 'Mantra';
$('d-model').value = 'RF00234';
$('d-id').value = 'mantra_nenufar';
$('p-build').dispatchEvent(new window.Event('click'));
await wait(() => $('exp-out').querySelector('pre'), 'profil construit');
const prof = JSON.parse($('exp-out').querySelector('pre').textContent);
fs.writeFileSync(OUT, JSON.stringify(prof, null, 2));

check('profil versionné', prof.version === 1);
check('appareil identifié', prof.device.id === 'mantra_nenufar'
  && prof.device.manufacturer === 'Mantra' && prof.device.model === 'RF00234');
check('la référence voyage dans le profil (elle porte l\'ID appairé)',
  typeof prof.rf.reference_b64 === 'string' && prof.rf.reference_b64.length > 500,
  `${prof.rf.reference_b64?.length} car.`);
check('la carte des 64 bits est embarquée', prof.fields.length === 11, prof.fields.length);
check('le checksum est embarqué', prof.checksum.kind === 'sub8' && prof.checksum.k === 85);

const ents = Object.fromEntries(prof.entities.map(e => [e.type, e]));
check('3 entités déduites : light, fan, number',
  Object.keys(ents).sort().join(',') === 'fan,light,number', prof.entities.map(e => e.type).join(','));
check('lumière : power + luminosité 1-11 + CCT 1-7 en kelvins',
  ents.light.power === 'light' && ents.light.brightness.max === 11
  && ents.light.color_temp.max === 7
  && JSON.stringify(ents.light.color_temp.kelvin) === '[3000,5000]');
check('ventilateur : vitesse 1-8, sens, presets',
  ents.fan.power === 'fan' && ents.fan.percentage.max === 8
  && ents.fan.direction === 'reverse'
  && JSON.stringify(ents.fan.preset.options) === '["normal","nuit","eco"]');
check('minuterie : échelle 2 min (la télécommande n\'offre que 1/2/4/8 h)',
  ents.number.field === 'timer' && ents.number.scale === 2 && ents.number.unit === 'min');
check('les champs const ne deviennent pas des entités',
  !prof.entities.some(e => ['preambule', 'cmd'].includes(e.power || e.field)));
check('profil compact', JSON.stringify(prof).length < 20000,
  `${JSON.stringify(prof).length} octets`);
check('pas d\'erreur JS', errors.length === 0, errors.join(' '));

// --- import : le partage, et le réancrage
const imported = await post('api/profile/import', { profile: prof, keep_reference: true })
  .then(r => r.json());
check('un profil se réimporte', imported.ok === true && imported.fields === 11, imported);

const shared = JSON.parse(JSON.stringify(prof));
shared.rf.reference_b64 = 'PAS_DU_B64_VALIDE';
const badref = await post('api/profile/import', { profile: shared, keep_reference: false })
  .then(r => r.json());
check('importer sans garder la référence dit qu\'il faut réancrer',
  badref.ok === true && badref.reference_kept === false && /Capture une trame/.test(badref.hint || ''),
  badref.hint?.slice(0, 60));

const invalid = await post('api/profile/import', { profile: { version: 99 } });
check('un profil invalide est refusé au chargement', invalid.status === 400,
  `HTTP ${invalid.status}`);

// --- /api/set : le contrat que le pont utilise aussi
const st = { light: 1, cct: 4, lum: 5, fan: 1, speed: 3, reverse: 0, mode: 0, timer: 0 };
const r = await post('api/set', { ...st, send: false }).then(r => r.json());
check('/api/set génère l\'état demandé', r.ok === true &&
  ['light=1', 'cct=4', 'lum=5', 'fan=1', 'speed=3'].every(s => r.state.includes(s)),
  r.state || JSON.stringify(r));
const bad = await post('api/set', { lumiere: 5 });
check('/api/set rejette un champ inconnu', bad.status === 400, `HTTP ${bad.status}`);
const t20 = await post('api/set', { timer: 10, send: false }).then(r => r.json());
check('/api/set accepte un timer arbitraire (20 min)', t20.state?.includes('timer=10'), t20.state);

console.log(`\n${fail ? '✗ ÉCHEC' : '✓ OK'} — ${pass} passés, ${fail} échoués`);
console.log(`profil -> dev/.out.profile.json`);
process.exit(fail ? 1 : 0);
