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
// jsdom n'implémente ni <dialog> ni les URL de blob
window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
window.HTMLDialogElement.prototype.close = function (v) {
  this.open = false; this.returnValue = v ?? this.returnValue;
  this.dispatchEvent(new window.Event('close'));
};
window.URL.createObjectURL = () => 'blob:stub';

const $ = (id) => window.document.getElementById(id);
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
// performance.now() et pas Date.now() : l'horloge murale SAUTE (resync NTP,
// et sous WSL2 des écarts de ~30 s ont été mesurés). La monotone, jamais.
const wait = async (fn, label, ms = 45000) => {
  const t0 = performance.now();
  while (performance.now() - t0 < ms) { if (fn()) return true; await sleep(80); }
  throw new Error('timeout: ' + label);
};
let pass = 0, fail = 0;
const check = (n, c, x = '') => { c ? pass++ : fail++; console.log(`  ${c ? '✓' : '✗'} ${n}${x ? '  — ' + x : ''}`); };

await wait(() => $('sliders').querySelector('.btns, input[type=range]'), 'sliders');

// --- le widget suit ce que le champ ACCEPTE : interrupteur si 0-1, un bouton
//     par valeur si elles tiennent en 4 bits, slider au-delà.
const sl = Object.fromEntries([...$('sliders').querySelectorAll('input[type=range]')]
  .map(s => [s.dataset.f, [s.min, s.max]]));
const sw = Object.fromEntries([...$('sliders').querySelectorAll('.switch input')]
  .map(c => [c.dataset.f, c.checked]));
const bt = Object.fromEntries([...$('sliders').querySelectorAll('.btns')]
  .map(g => [g.dataset.f, [...g.querySelectorAll('button')].map(b => +b.dataset.v)]));
check('un interrupteur pour les champs qui ne valent que 0 ou 1',
  Object.keys(sw).sort().join(',') === 'fan,light,reverse', Object.keys(sw).join(','));
check('des boutons pour les champs à peu de valeurs',
  Object.keys(bt).sort().join(',') === 'cct,lum,mode,speed', Object.keys(bt).join(','));
check('un slider seulement au-delà de 15', Object.keys(sl).join(',') === 'timer',
  Object.keys(sl).join(','));

// Les bornes RÉELLES, pas la largeur des bits : sans elles lum irait de 0 à 15
// alors que la télécommande ne fait que 1 à 11.
check('lum : boutons 1 à 11, pas 0 à 15',
  bt.lum?.join(',') === '1,2,3,4,5,6,7,8,9,10,11', bt.lum?.join(','));
check('cct : boutons 1 à 7', bt.cct?.join(',') === '1,2,3,4,5,6,7', bt.cct?.join(','));
check('speed : boutons 1 à 8', bt.speed?.join(',') === '1,2,3,4,5,6,7,8', bt.speed?.join(','));
check('mode : boutons 0 à 2', bt.mode?.join(',') === '0,1,2', bt.mode?.join(','));
check('timer reste un slider 0-255 (8 bits, pas seulement 1/2/4/8 h)',
  sl.timer?.join('-') === '0-255', sl.timer?.join('-'));

// L'interrupteur doit être LU à la génération. sliderValues() ne ramassait que
// les input[type=range] : un booléen basculé n'aurait rien changé à la trame, en
// silence — et le bouton « Émettre » serait resté vert.
const lightSw = [...$('sliders').querySelectorAll('.switch input')].find(c => c.dataset.f === 'light');
const was = lightSw.checked;
lightSw.checked = !was;
lightSw.dispatchEvent(new window.Event('change'));
check("l'étiquette suit l'interrupteur", $('v-light').textContent === (was ? 'off' : 'on'),
  $('v-light').textContent);
$('gen').dispatchEvent(new window.Event('click'));
await wait(() => $('gen-out').querySelector('pre'), 'génération');
const genBits = $('gen-out').querySelector('pre .diff').textContent;
// bit 32 = alimentation de la lampe (§10)
check("basculer l'interrupteur change bien le bit 32 de la trame générée",
  genBits[32] === (was ? '0' : '1'), `bit32=${genBits[32]}, interrupteur ${!was}`);
lightSw.checked = was;
lightSw.dispatchEvent(new window.Event('change'));

// --- déclarer ce que le RÉCEPTEUR fait d'un champ (profil v2).
// Ça ne se lit pas dans les bits : sur une Mantra R00143, le sens de rotation
// s'inverse à chaque trame quoi que porte le bit, et la vitesse n'est appliquée
// que si l'octet de commande vaut 10. Le labo doit pouvoir le déclarer, sinon le
// pont ne peut pas piloter l'appareil.
const openField = async (name) => {
  const f = [...window.document.querySelectorAll('.field-chip b')]
    .find(b => b.textContent === name);
  const cell = (c) => $('grid-wrap').querySelector(`td.bit[data-col="${c}"]`);
  const fld = (await fetch(new URL('api/analyze?gap=2000', BASE)).then(r => r.json()))
    .fields.find(x => x.name === name);
  const mouse = (el, t) => el.dispatchEvent(new window.MouseEvent(t, { bubbles: true, view: window }));
  mouse(cell(fld.start), 'mousedown');
  for (let i = fld.start; i < fld.end; i++) mouse(cell(i), 'mousemove');
  window.dispatchEvent(new window.MouseEvent('mouseup', { bubbles: true }));
  await sleep(120);
  return fld;
};

await openField('reverse');
check('le dialogue propose la sémantique du récepteur', !!$('f-sem'), 'select f-sem');
check('… et la liste des champs conditionnants inclut les const',
  [...$('f-req-f').options].map(o => o.value).includes('cmd'),
  [...$('f-req-f').options].map(o => o.value).join(','));
check('la valeur de la condition est bloquée tant qu\'aucun champ n\'est choisi',
  $('f-req-v').disabled);
$('f-sem').value = 'toggle';
$('f-req-f').value = 'cmd';
$('f-req-f').dispatchEvent(new window.Event('change'));
check('choisir un champ débloque la valeur', !$('f-req-v').disabled);
$('f-req-v').value = '12';
$('dlg').close('ok');
await sleep(600);
check('la sémantique est persistée', (await fetch(new URL('api/analyze?gap=2000', BASE))
  .then(r => r.json())).fields.find(f => f.name === 'reverse')?.semantics === 'toggle');

await openField('speed');
$('f-req-f').value = 'cmd';
$('f-req-f').dispatchEvent(new window.Event('change'));
$('f-req-v').value = '10';
$('dlg').close('ok');
await sleep(600);
const back = (await fetch(new URL('api/analyze?gap=2000', BASE)).then(r => r.json())).fields;
check('la condition est persistée',
  JSON.stringify(back.find(f => f.name === 'speed')?.requires) === '{"cmd":10}',
  JSON.stringify(back.find(f => f.name === 'speed')?.requires));
check('les chips montrent la bascule et la condition',
  /⇄/.test($('fields-box').innerHTML) && /si cmd=10/.test($('fields-box').innerHTML));

// --- construction du profil : le livrable du labo
$('d-name').value = 'Mantra Nenufar';
$('d-manu').value = 'Mantra';
$('d-model').value = 'RF00234';
$('d-id').value = 'mantra_nenufar';
$('p-build').dispatchEvent(new window.Event('click'));
await wait(() => $('exp-out').querySelector('pre'), 'profil construit');
const prof = JSON.parse($('exp-out').querySelector('pre').textContent);
fs.writeFileSync(OUT, JSON.stringify(prof, null, 2));

check('profil versionné', prof.version === 2, prof.version);
check('appareil identifié', prof.device.id === 'mantra_nenufar'
  && prof.device.manufacturer === 'Mantra' && prof.device.model === 'RF00234');
check('la référence voyage dans le profil (elle porte l\'ID appairé)',
  typeof prof.rf.reference_b64 === 'string' && prof.rf.reference_b64.length > 500,
  `${prof.rf.reference_b64?.length} car.`);
check('la carte des 64 bits est embarquée', prof.fields.length === 11, prof.fields.length);
check('le checksum est embarqué', prof.checksum.kind === 'sub8' && prof.checksum.k === 85);
// LE livrable : ce que le pont a besoin de savoir et que les bits ne disent pas.
check('le profil embarque la sémantique du récepteur',
  prof.fields.find(f => f.name === 'reverse')?.semantics === 'toggle');
check('le profil embarque la condition d\'application',
  JSON.stringify(prof.fields.find(f => f.name === 'speed')?.requires) === '{"cmd":10}',
  JSON.stringify(prof.fields.find(f => f.name === 'speed')?.requires));

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
