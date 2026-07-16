/**
 * L'IP du Broadlink se configure depuis l'UI, et elle est persistée.
 *
 *   node dev/device_test.mjs [http://127.0.0.1:8099/]
 *
 * Ces appareils sont en DHCP : leur adresse change. Sans ce champ il faudrait
 * éditer les options de l'addon et le redémarrer à chaque bail renouvelé.
 * Le faux RM4 ne répond qu'à FAKE_RM4_IP — toute autre adresse simule un
 * appareil absent, ce qui permet de tester aussi les erreurs.
 */
import { JSDOM, VirtualConsole } from 'jsdom';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const INDEX = path.join(HERE, '..', 'rf_lab', 'www', 'index.html');
const BASE = process.argv[2] || 'http://127.0.0.1:8099/';
const KNOWN = process.env.FAKE_RM4_IP || '192.168.0.99';

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', e => errors.push(e.message));
const dom = new JSDOM(fs.readFileSync(INDEX, 'utf8'), {
  url: BASE, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
  beforeParse(w) {
    w.fetch = (u, o) => fetch(new URL(u, w.location.href), o);
    // jsdom a son propre AbortSignal, que le fetch de node refuse : on donne à
    // la page celui de node, comme pour fetch. Dans un vrai navigateur les deux
    // sont natifs et compatibles — artefact du harnais, pas du produit.
    w.AbortController = AbortController;
  },
});
const { window } = dom;
window.URL.createObjectURL = () => 'blob:stub';

const $ = (id) => window.document.getElementById(id);
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
// Un timeout qui ne dit pas ce qu'il a vu ne sert à rien pour diagnostiquer.
// performance.now() et pas Date.now() : l'horloge murale SAUTE (resync NTP,
// et sous WSL2 des écarts de ~30 s ont été mesurés). La monotone, jamais.
const wait = async (fn, label, ms = 45000) => {
  const t0 = performance.now();
  while (performance.now() - t0 < ms) { if (fn()) return true; await sleep(80); }
  throw new Error(`timeout: ${label} (${ms} ms)\n`
    + `    conn   = « ${$('conn')?.textContent.trim()} »\n`
    + `    ip-msg = « ${$('ip-msg')?.textContent.trim()} »\n`
    + `    ip     = « ${$('ip')?.value} »  ip-set disabled=${$('ip-set')?.disabled}\n`
    + `    erreurs JS : ${errors.length ? errors.join(' | ') : 'aucune'}`);
};
let pass = 0, fail = 0;
const check = (n, c, x = '') => { c ? pass++ : fail++; console.log(`  ${c ? '✓' : '✗'} ${n}${x ? '  — ' + x : ''}`); };
const get = (p) => fetch(new URL(p, BASE)).then(r => r.json());

// Repartir d'un état connu : une passe interrompue laisse `device_ip` persisté,
// et la suivante en hérite — chaque appel devient alors 3 essais × timeout.
await fetch(new URL('api/device', BASE), {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ ip: '' }) }).catch(() => {});

await wait(() => !/connexion…/.test($('conn').textContent), 'statut initial', 45000);

// --- l'IP courante remonte dans la barre d'état
const st0 = await get('api/status');
check('/api/status expose l\'IP et sa provenance',
  st0.ip !== undefined && ['ui', 'option', 'broadcast'].includes(st0.ip_source),
  `${st0.ip} (${st0.ip_source})`);
check('le champ IP de l\'UI est pré-rempli', $('ip').value === (st0.ip || ''),
  `« ${$('ip').value} »`);

// --- une IP où personne ne répond : erreur claire, pas de plantage
$('ip').value = '192.168.0.254';
$('ip-set').dispatchEvent(new window.Event('click'));
await wait(() => /Pas de réponse|Connecté/.test($('ip-msg').textContent), 'connexion ratée');
check('une IP sans appareil -> erreur explicite, l\'app tient debout',
  /Pas de réponse/.test($('ip-msg').textContent) && !!$('conn').querySelector('.dot.off'),
  $('ip-msg').textContent.trim().slice(0, 58));
const bad = await get('api/status');
check('l\'IP fautive est quand même persistée (on la corrige, on ne la perd pas)',
  bad.ip === '192.168.0.254' && bad.ip_source === 'ui', `${bad.ip} (${bad.ip_source})`);

// --- la bonne IP : ça reconnecte sans redémarrer l'addon
$('ip').value = KNOWN;
$('ip-set').dispatchEvent(new window.Event('click'));
await wait(() => /Connecté/.test($('ip-msg').textContent), 'connexion réussie');
check('la bonne IP reconnecte à chaud', /Connecté à RM4 Pro/.test($('ip-msg').textContent),
  $('ip-msg').textContent.trim().slice(0, 52));
check('la barre d\'état repasse au vert', !!$('conn').querySelector('.dot.on'),
  $('conn').textContent.trim());
const good = await get('api/status');
check('l\'IP saisie est persistée et prioritaire',
  good.connected && good.ip === KNOWN && good.ip_source === 'ui', `${good.ip} (${good.ip_source})`);

// --- vider le champ = revenir à l'option de l'addon / au broadcast
$('ip').value = '';
$('ip-set').dispatchEvent(new window.Event('click'));
await wait(() => /Connecté|Pas de réponse/.test($('ip-msg').textContent), 'retour au défaut');
const empty = await get('api/status');
check('champ vide -> on retombe sur l\'option ou le broadcast',
  empty.ip_source !== 'ui', `source=${empty.ip_source}`);

// --- la recherche
const disc = await get('api/discover');
check('/api/discover trouve le RM4 en broadcast',
  disc.devices.length === 1 && disc.devices[0].ip === KNOWN && disc.method === 'broadcast',
  JSON.stringify(disc.devices));
check('il est marqué capable de RF (0x520b = RM4 Pro)',
  disc.devices[0].rf === true && disc.devices[0].devtype === 0x520b);

$('ip-find').dispatchEvent(new window.Event('click'));
await wait(() => /appareil\(s\)/.test($('ip-msg').textContent), 'bouton Chercher');
check('le bouton Chercher pré-remplit l\'IP trouvée', $('ip').value === KNOWN,
  $('ip').value);
check('il annonce ce qu\'il a trouvé', /RF ✓/.test($('ip-msg').textContent),
  $('ip-msg').textContent.trim().slice(0, 64));

// --- l'annulation : une IP fautive coûte 18 s, on doit pouvoir abandonner
$('ip').value = '192.168.0.251';
check('le bouton Annuler est caché au repos', $('ip-stop').hidden);
const t0 = performance.now();
$('ip-set').dispatchEvent(new window.Event('click'));
await wait(() => !$('ip-stop').hidden, 'bouton Annuler visible');
check('Annuler apparaît pendant la tentative', !$('ip-stop').hidden);
check('Connecter et Chercher sont bloqués pendant la tentative',
  $('ip-set').disabled && $('ip-find').disabled);

await sleep(300);
$('ip-stop').dispatchEvent(new window.Event('click'));
await wait(() => /Abandonné/.test($('ip-msg').textContent), 'abandon');
// PAS d'assertion sur le temps écoulé : cette machine a montré des décrochages
// d'ordonnancement de ~30 s sous WSL2 (un time.sleep(2) mesuré à 31 s), ce qui
// rend toute mesure d'horloge murale ininterprétable dans la suite. Le
// comportement se vérifie ici, la latence dans dev/cancel_timing_test.py qui
// contrôle son environnement.
check('l\'abandon aboutit', /Abandonné/.test($('ip-msg').textContent),
  `${Math.round(performance.now() - t0)} ms écoulées`);
check('l\'UI le dit clairement', /Corrige l'IP/.test($('ip-msg').textContent),
  $('ip-msg').textContent.trim().slice(0, 52));
check('Annuler se recache, les boutons reviennent',
  $('ip-stop').hidden && !$('ip-set').disabled && !$('ip-find').disabled);

// le serveur a bien reçu l'ordre : la tentative suivante ne doit pas être polluée
const again = await fetch(new URL('api/device', BASE), {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ ip: KNOWN }) }).then(r => r.json());
check('une nouvelle connexion repart proprement après un abandon',
  again.connected === true, again.error || `${again.model} @ ${again.host}`);

// annuler quand rien ne tourne ne doit pas casser la suite
const noop = await fetch(new URL('api/device/cancel', BASE), { method: 'POST' })
  .then(r => r.json());
check('annuler à vide est sans effet', noop.ok === true);
const still = await get('api/status');
check('la connexion en cours survit à une annulation tardive', still.connected === true,
  still.error || 'connecté');

check('pas d\'erreur JS', errors.length === 0, errors.join(' '));

// remettre la config par défaut pour les tests suivants
await fetch(new URL('api/device', BASE), {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ ip: '' }) });

console.log(`\n${fail ? '✗ ÉCHEC' : '✓ OK'} — ${pass} passés, ${fail} échoués`);
process.exit(fail ? 1 : 0);
