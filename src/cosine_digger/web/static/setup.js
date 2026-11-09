import {selectors} from './state.js';
import {formatDay, sourceName} from './dom.js';

const fields = [
  ['daily_time', 'set-daily-time'], ['daily_size', 'set-daily-size'], ['weekly_day', 'set-weekly-day'],
  ['weekly_time', 'set-weekly-time'], ['weekly_size', 'set-weekly-size'],
];
const folder = ['music_dir', 'set-music'];
// A key reaches the server only through its provider check (checkConnection), never saveSettings (A5).
const keyCards = {
  cosine_token: {card: 'cosine', thing: 'key', service: 'cosine.club', rejected: 'S-SET-COSINE-REJECTED',
    malformed: 'S-SET-COSINE-REJECTED', unreachable: 'S-SET-COSINE-UNREACHABLE'},
  discogs_token: {card: 'discogs', thing: 'token', service: 'Discogs', ok: 'S-SET-DISCOGS-OK', saved: 'S-SET-TOKEN-SAVED',
    rejected: 'S-SET-DISCOGS-REJECTED', malformed: 'S-SET-DISCOGS-MALFORMED', unreachable: 'S-SET-DISCOGS-UNREACHABLE'},
  exa_api_key: {card: 'exa', thing: 'key', service: 'Exa', ok: 'S-SET-EXA-OK', saved: 'S-SET-KEY-SAVED',
    rejected: 'S-SET-EXA-REJECTED', malformed: 'S-SET-EXA-MALFORMED', unreachable: 'S-SET-EXA-UNREACHABLE'},
};
const formInputs = {discogs: 'set-discogs', exa: 'set-exa', yt: 'yt-headers'};
const checkNames = {
  cosine_token: 'cosine.club key', discogs_token: 'Discogs token', exa_api_key: 'Exa key',
  youtube_login: 'YouTube Music login', music_dir: 'Music folder', db: 'Taste database',
};
const optional = ['discogs_token', 'exa_api_key', 'youtube_login', 'music_dir'];
const last4 = (key) => (key.hint || '').replace(/^…/, '');

/** Five capability cards and Health; all requests and job lifetimes belong to the store. */
export function mountSetup(root, {store, h, text}) {
  const actions = store.actions, find = (id) => root.querySelector('#' + id);
  const disposers = [], refreshing = new Set();
  const busy = new Set();
  const dirtyFields = new Set(), savedValues = new Map();
  const notes = {};  // per key: [tone, message] from its last check that didn't connect
  let destroyed = false, settingsSignature = null, healthSignature = null;
  let wasActive = false, scanSignature = null, budgetInitialized = false;

  function listen(node, type, handler) {
    node.addEventListener(type, handler);
    disposers.push(() => node.removeEventListener(type, handler));
  }
  function alert(id, message = '') {
    const node = find(id);
    node.textContent = message;
    node.hidden = !message;
  }
  async function perform(key, errorId, work) {
    if (busy.has(key) || destroyed) return;
    busy.add(key);
    alert(errorId);
    render(store.getState());
    try { await work(); }
    catch (error) { if (!destroyed) alert(errorId, error.message); }
    finally {
      busy.delete(key);
      if (!destroyed) render(store.getState());
    }
  }
  function refreshDoctor() {
    return perform('doctor', 'health-error', async () => {
      const doctor = await actions.refreshDoctor();
      // A verdict the key in use doesn't carry was about a key saved over meanwhile: check this one.
      if (['connected', 'rejected'].includes(doctor?.checks?.cosine_token?.state) &&
          !store.getState().ui.settings?.cosine_token?.checked) await actions.refreshDoctor();
    });
  }
  function refreshAfter(key) {
    if (refreshing.has(key) || destroyed) return;
    refreshing.add(key);
    actions.loadSetup().catch((error) => {
      if (!destroyed) alert('labels-error', error.message);
    });
  }

  find('set-music').closest('.field').querySelector('.field__help').textContent = text('S-SET-LIB-HELP');
  const status = h('p', {id: 'labels-status', role: 'status', 'aria-live': 'polite'});
  const progress = h('progress', {id: 'labels-progress', max: 1, value: 0, hidden: true,
    'aria-label': 'Album label lookups'});
  const counters = h('p', {id: 'labels-counts', className: 'field__help'});
  const go = h('button', {id: 'labels-go', type: 'button', className: 'btn',
    'data-action': 'start-enrichment', 'aria-describedby': 'labels-status labels-detail'}, text('S-SET-FILL-BTN'));
  const stop = h('button', {id: 'labels-stop', type: 'button', className: 'btn', hidden: true,
    'data-action': 'cancel-enrichment'}, 'Stop');
  const detail = h('p', {id: 'labels-detail', className: 'field__help'});
  const budget = h('input', {id: 'labels-budget', className: 'input', type: 'number',
    min: 1, max: 500, step: 1, inputmode: 'numeric', value: 100,
    'aria-describedby': 'labels-budget-help'});
  const budgetHelp = h('p', {id: 'labels-budget-help', className: 'field__help'});
  const labelField = h('div', {id: 'labels-field', className: 'field', role: 'group',
    'aria-labelledby': 'labels-heading'},
    h('h4', {id: 'labels-heading', className: 'field__label'}, text('S-SET-FILL-LABEL')),
    status, progress, counters, h('div', {className: 'labels-actions'}, go, stop), detail,
    h('div', {className: 'labels-budget-row'},
      h('label', {className: 'field__label', htmlFor: 'labels-budget'}, 'Discogs lookup budget'), budget),
    budgetHelp,
    h('p', {className: 'field__help'}, 'The limit counts requests, including retries. Cached lookups use no requests.'),
    h('div', {id: 'labels-error', className: 'alert', role: 'alert', hidden: true}));
  find('card-music').querySelector('.panel__body').append(labelField);
  const librarySummary = find('library-summary');
  const [neededLabel, dbProblem] = [text('S-SET-HEALTH-NEEDED').split(' ✓')[0], text('S-SET-HEALTH-DB').split(' · ')];
  const dbLine = h('p', {id: 'health-db', className: 'health__needed', hidden: true},
    dbProblem[0], ' · ', h('span', {className: 'health__bad'}, dbProblem[1]));
  find('health-needed').after(dbLine);
  const about = find('about');
  find('about-heading').textContent = text('S-ABOUT-TITLE');
  const aboutBody = about.querySelector('.panel__body');
  aboutBody.lastElementChild.textContent = text('S-ABOUT-3');
  // Build links with the shared safe DOM helper; retain the browser's Referer.
  const [intro] = text('S-ABOUT-2').split('YouTube Terms of Service');
  aboutBody.children[1].replaceChildren(intro,
    h('a', {href: 'https://www.youtube.com/t/terms', target: '_blank', rel: 'noopener'}, 'YouTube Terms of Service'),
    ' · ', h('a', {href: 'https://policies.google.com/privacy', target: '_blank', rel: 'noopener'}, 'Google Privacy Policy'));
  aboutBody.firstElementChild.after(h('p', {id: 'about-rank'}, text('S-ABOUT-RANK')));

  function currentJob(setup) {
    return setup.enrichment?.id ? setup.enrichment : setup.enrichment?.overview?.last_job;
  }
  function resumeJob(setup) {
    const job = currentJob(setup);
    const remaining = job?.result?.remaining_pairs ?? job?.progress?.work?.remaining_pairs ??
      job?.progress?.work?.pending_pair_ids?.length;
    return job && ['stopped', 'error', 'failed'].includes(job.state) && remaining !== 0 ? job.id : null;
  }
  listen(go, 'click', (event) => {
    if (event.detail > 1) return;
    const value = Number(budget.value);
    budget.removeAttribute('aria-invalid');
    if (!budget.value || !Number.isInteger(value) || value < 1 || value > 500) {
      budget.setAttribute('aria-invalid', 'true');
      alert('labels-error', 'Discogs lookup budget must be a whole number from 1 to 500.');
      budget.focus();
      return;
    }
    perform('enrichment', 'labels-error', () => actions.startEnrichment({
      budget: value, resume_job_id: resumeJob(selectors.setupState(store.getState())),
    }));
  });
  listen(stop, 'click', (event) => {
    if (event.detail > 1) return;
    const id = currentJob(selectors.setupState(store.getState()))?.id;
    if (id) perform('cancel', 'labels-error', () => actions.cancelEnrichment(id));
  });
  listen(budget, 'input', () => {
    budget.removeAttribute('aria-invalid');
    budgetHelp.textContent = text('S-SET-FILL-BUDGET', {b: budget.value || '100'});
  });

  for (const [key, id] of [folder, ...fields]) {
    const input = find(id);
    listen(input, 'input', () => {
      if (input.value === savedValues.get(key) && !busy.has('settings') && !busy.has('scan')) dirtyFields.delete(key);
      else dirtyFields.add(key);
    });
  }

  // CONNECT opens a card's field; Cancel or a connect that works closes it.
  function openForm(card) {
    find(card + '-form').hidden = false;
    find(card + '-connect').setAttribute('aria-expanded', 'true');
    render(store.getState());
    find(formInputs[card]).focus();
  }
  function closeForm(card) {
    find(card + '-form').hidden = true;
    find(card + '-connect').setAttribute('aria-expanded', 'false');
    find(formInputs[card]).value = '';
  }
  for (const button of root.querySelectorAll('[data-action="close-card"]')) {
    const card = button.closest('form').id.replace(/-form$/, '');
    listen(button, 'click', () => {
      closeForm(card);
      if (card === 'yt') alert('yt-error');
      for (const [name, spec] of Object.entries(keyCards)) if (spec.card === card) delete notes[name];
      render(store.getState());
      find(card + '-connect').focus();
    });
  }
  function noteFor(name, result) {
    const spec = keyCards[name], s = result.retry_after_s ?? 30;
    const message = {
      rejected: spec.rejected && text(spec.rejected), malformed: text(spec.malformed), unreachable: text(spec.unreachable),
      busy: text('S-SET-BUSY', {service: spec.service, s}), server_error: text('S-SET-SERVER-ERR', {service: spec.service, s}),
      save_failed: text('S-SET-SAVE-FAILED', {thing: spec.thing, reason: result.reason || 'unknown error'}),
    }[result.state];
    return message ? ['warn', message] : null;
  }
  function closeAfterConnect(card) {
    closeForm(card);
    render(store.getState());
    find(card + '-connect').focus();
  }
  function check(name, key) {
    const spec = keyCards[name];
    delete notes[name];
    return perform('check:' + name, spec.card + '-error', async () => {
      const result = await actions.checkConnection(name, key);
      if (!result || destroyed) return;
      if (result.state === 'connected') { if (key != null) closeAfterConnect(spec.card); }
      // A refused or malformed key in use is now in settings; a typed one, or no answer, isn't.
      else if (key != null || !['rejected', 'malformed'].includes(result.state)) notes[name] = noteFor(name, result);
      refreshDoctor();
    });
  }
  for (const [name, {card}] of Object.entries(keyCards)) {
    listen(find(card + '-check'), 'click', () => check(name));
    if (name === 'cosine_token') continue;  // the key screen (#connect) owns the cosine.club key
    listen(find(card + '-connect'), 'click', () => openForm(card));
    listen(find(card + '-form'), 'submit', (event) => {
      event.preventDefault();
      const input = find(formInputs[card]);
      if (!input.value.trim()) {
        notes[name] = ['warn', text('S-SET-EMPTY', {thing: keyCards[name].thing})];
        render(store.getState());
        input.focus();
        return;
      }
      check(name, input.value);
    });
  }

  listen(find('settings-form'), 'submit', (event) => {
    event.preventDefault();
    if (busy.has('settings')) return;
    const settings = selectors.setupState(store.getState()).settings || {};
    const body = {}, problems = [], submittedValues = new Map();
    find('settings-note').textContent = '';
    alert('settings-error');
    for (const [key, id] of fields) {
      const input = find(id);
      submittedValues.set(key, input.value);
      input.removeAttribute('aria-invalid');
      let value = input.value.trim(), problem;
      if (value === String(settings[key] ?? '')) continue;
      if (key.endsWith('_time') && !/^([01]\d|2[0-3]):[0-5]\d$/.test(value))
        problem = `${key === 'daily_time' ? 'Daily' : 'Weekly'} time must be HH:MM.`;
      if (key.endsWith('_size')) {
        value = Number(value);
        if (!input.value || !Number.isInteger(value) || value < 5 || value > 100)
          problem = `${key === 'daily_size' ? 'Daily' : 'Weekly'} size must be a whole number from 5 to 100.`;
      }
      if (problem) { problems.push(problem); input.setAttribute('aria-invalid', 'true'); }
      else body[key] = value;
    }
    if (problems.length) { alert('settings-error', problems.join(' ')); return; }
    if (!Object.keys(body).length) {
      for (const [key] of fields) dirtyFields.delete(key);
      find('settings-note').textContent = 'Nothing changed.';
      return;
    }
    perform('settings', 'settings-error', async () => {
      const result = await actions.saveSettings(body);
      if (!result || destroyed) return;
      for (const [key, id] of fields) {
        // Edits made while the save was in flight still belong to the user.
        if (find(id).value === submittedValues.get(key)) {
          dirtyFields.delete(key);
          find(id).value = String(result[key] ?? '');
        }
      }
      find('settings-note').textContent = `Saved: ${Object.keys(body).map((key) => key.replaceAll('_', ' ')).join(', ')}.`;
      refreshDoctor();
    });
  });
  // Scan now keeps a typed folder as the music folder, then scans it.
  listen(find('scan-go'), 'click', () => {
    const input = find('set-music'), path = input.value.trim();
    const saved = String(selectors.setupState(store.getState()).settings?.music_dir ?? '');
    perform('scan', 'scan-error', async () => {
      if (path && path !== saved) {
        const result = await actions.saveSettings({music_dir: path});
        if (!result || destroyed) return;
        if (input.value.trim() === path) { dirtyFields.delete('music_dir'); input.value = String(result.music_dir ?? ''); }
        refreshDoctor();
      }
      await actions.scanLibrary(path || null);
    });
  });
  listen(find('health-refresh'), 'click', refreshDoctor);
  listen(find('yt-connect'), 'click', () => openForm('yt'));
  listen(find('yt-chrome'), 'click', () => perform('chrome', 'yt-chrome-error', async () => {
    const result = await actions.loginYouTube(null, {chrome: true});
    if (!result || destroyed) return;
    if (['connected', 'saved'].includes(result.state) && !find('yt-form').hidden) closeAfterConnect('yt');
    refreshDoctor();
  }));
  listen(find('yt-form'), 'submit', (event) => {
    event.preventDefault();
    const area = find('yt-headers');
    if (!area.value.trim()) {
      alert('yt-error', 'Paste the request headers first.');
      area.focus();
      return;
    }
    perform('login', 'yt-error', async () => {
      const result = await actions.loginYouTube(area.value);
      if (!result || destroyed) return;
      // Keep the headers only for a check that got no answer, so Log in can simply run again.
      if (result.state !== 'unreachable') area.value = '';
      if (['connected', 'saved'].includes(result.state)) closeAfterConnect('yt');
      refreshDoctor();
    });
  });

  function show(id, tone, message) {
    const node = find(id);
    node.textContent = message || '';
    node.dataset.tone = tone;
    node.hidden = !message;
  }
  function source(id, key) {
    const env = key?.source === 'env';
    show(id, 'muted', env ? text('S-SET-SOURCE-ENV', {env: key.env}) : '');
  }
  function cosineState(setup) {
    const key = setup.settings?.cosine_token;
    const check = busy.has('doctor') ? null : setup.doctor?.checks?.cosine_token;
    if (!key) return null;
    if (!key.set) return 'missing';
    // Connected or refused only by the server's verdict on the key in use: Health's check counts
    // through it, so a check of a key saved over since never speaks for this one.
    if (key.malformed || ['rejected', 'malformed'].includes(key.checked)) return 'rejected';
    // A Health check that got no verdict (busy, a server error, no answer) and is newer than the
    // key's own check: cosine.club isn't answering now, whatever it said before.
    const silent = ['busy', 'server_error', 'unreachable'].includes(check?.state);
    if (silent && setup.doctorAsked > setup.keyCheckedAt) return 'unreachable';
    // warn is the fixture test mode's unchecked key.
    if (key.checked === 'connected' || check?.status === 'warn') return 'connected';
    return silent ? 'unreachable' : 'checking';
  }
  /** Why cosine.club couldn't check the key, as the key screen words it (busy, server error, no answer). */
  function silentNote(setup) {
    const check = setup.doctor?.checks?.cosine_token;
    return (check && noteFor('cosine_token', check)) || ['warn', text('S-SET-COSINE-UNREACHABLE')];
  }
  function renderCosine(setup) {
    const key = setup.settings?.cosine_token, state = cosineState(setup);
    const [tone, message] = busy.has('check:cosine_token') || state === 'checking' ? ['muted', text('S-SET-CHECKING')] :
      state === 'missing' ? ['fail', text('S-SET-COSINE-MISSING')] :
      state === 'rejected' ? ['fail', text('S-SET-COSINE-REJECTED')] :
      state === 'connected' ? ['ok', key.hint ? text('S-SET-COSINE-OK', {last4: last4(key)}) : text('S-SET-CONNECTED')] :
      notes.cosine_token || (state === 'unreachable' ? silentNote(setup) : ['muted', '']);
    show('cosine-state', tone, message);
    source('cosine-source', key);
    const fix = find('cosine-fix');
    fix.hidden = !['missing', 'rejected'].includes(state);
    fix.textContent = text(state === 'missing' ? 'S-SET-ADD-KEY' : 'S-SET-FIX-KEY');
    find('cosine-check').hidden = state !== 'unreachable' || busy.has('check:cosine_token');
  }
  function renderKey(name, setup) {
    const spec = keyCards[name], key = setup.settings?.[name], env = key?.source === 'env';
    const pending = busy.has('check:' + name), form = find(spec.card + '-form');
    const verdict = !key?.set ? null : key.malformed ? 'malformed' : key.checked;
    const [tone, message] = !key ? ['muted', ''] : pending ? ['muted', text('S-SET-CHECKING')] :
      notes[name] || (!key.set ? ['muted', text('S-SET-NOT-CONNECTED')] :
      verdict === 'connected' ? ['ok', key.hint ? text(spec.ok, {last4: last4(key)}) : text('S-SET-CONNECTED')] :
      // A key from the environment has no field to paste into: say where to fix it.
      ['rejected', 'malformed'].includes(verdict) ? ['warn', verdict === 'malformed' && env ?
        text('S-SET-ENV-MALFORMED', {thing: spec.thing, env: key.env}) : text(spec[verdict])] :
      ['muted', env ? '' : text(spec.saved)]);
    show(spec.card + '-state', tone, message);
    source(spec.card + '-source', key);
    // An environment key is read-only here (A7): its source shows, never a field.
    const toggle = find(spec.card + '-connect');
    toggle.hidden = !key || env || !form.hidden;
    toggle.textContent = text(verdict === 'connected' ? 'S-SET-CHANGE' : 'S-SET-CONNECT');
    find(spec.card + '-check').hidden = !key?.set || pending || !form.hidden ||
      ['connected', 'rejected', 'malformed'].includes(verdict);
  }
  function renderYouTube(setup) {
    const youtube = setup.settings?.youtube, form = find('yt-form');
    // "missing": the pasted headers saved no login (an empty {} isn't one).
    const refused = setup.login?.state === 'missing' && !youtube?.saved;
    const [tone, message] = busy.has('chrome') ? ['muted', text('S-SET-YT-CHROME-WAIT')] :
      busy.has('login') ? ['muted', text('S-SET-CHECKING')] : !youtube ? ['muted', ''] :
      refused || youtube.checked === 'rejected' ? ['warn', text('S-SET-YT-REJECTED')] :
      !youtube.saved ? ['muted', text('S-SET-NOT-CONNECTED')] :
      youtube.checked === 'connected' ? ['ok', text('S-SET-CONNECTED')] :
      youtube.checked === 'unreachable' ? ['warn', text('S-SET-YT-UNREACHABLE')] : ['muted', text('S-SET-YT-SAVED')];
    show('yt-state', tone, message);
    find('yt-chrome-block').hidden = !youtube?.chrome;
    find('yt-chrome').disabled = busy.has('chrome') || busy.has('login');
    const toggle = find('yt-connect');
    toggle.hidden = !youtube || !form.hidden;
    toggle.textContent = text(youtube?.checked === 'connected' ? 'S-SET-CHANGE' : 'S-SET-CONNECT');
  }
  function renderSettings(settings) {
    if (!settings) return;
    show('music-state', 'muted', settings.music_dir ? '' : text('S-SET-MUSIC-NONE'));
    const signature = JSON.stringify([folder, ...fields].map(([key]) => settings[key]));
    if (signature === settingsSignature) return;
    settingsSignature = signature;
    for (const [key, id] of [folder, ...fields]) {
      const value = String(settings[key] ?? '');
      savedValues.set(key, value);
      if (!dirtyFields.has(key)) find(id).value = value;
    }
  }
  function renderHealth(setup) {
    const doctor = setup.doctor, settings = setup.settings, cosine = cosineState(setup);
    const verdicts = {discogs_token: settings?.discogs_token?.checked, exa_api_key: settings?.exa_api_key?.checked,
      youtube_login: settings?.youtube?.checked};
    const signature = JSON.stringify([doctor, verdicts, cosine]);
    if (signature === healthSignature) return;
    healthSignature = signature;
    const checks = doctor?.checks || {};
    const words = {missing: text('S-SET-COSINE-MISSING'), rejected: text('S-SET-COSINE-REJECTED'),
      checking: text('S-SET-CHECKING')};
    const word = cosine === 'unreachable' ? silentNote(setup)[1] : words[cosine];
    // What digging needs comes first; the optional items stay quiet until they're set up (P8).
    find('health-needed').replaceChildren(...!cosine ? [] : cosine === 'connected' ?
      [neededLabel, ' ', h('span', {className: 'health__ok'}, '✓')] :
      [neededLabel, ' · ', h('span', {className: cosine === 'checking' ? 'health__wait' : 'health__bad'}, word)]);
    dbLine.hidden = !checks.db || checks.db.status === 'ok';
    const row = (name, state, label, info) => h('li', {className: 'check', 'data-status': state, 'data-check': name},
      h('span', {className: 'check__led', 'aria-hidden': 'true'}),
      h('span', {className: 'check__name'}, checkNames[name] || name),
      h('span', {className: 'check__status'}, label),
      info ? h('span', {className: 'check__detail'}, info) : null);
    find('health-list').replaceChildren(...optional.filter((name) => checks[name]).map((name) => {
      const check = checks[name], rejected = check.status !== 'off' && verdicts[name] === 'rejected';
      const state = rejected ? 'warn' : check.status;
      return row(name, state, rejected ? 'rejected' : state === 'off' ? text('S-SET-HEALTH-OFF') : state,
        ['warn', 'fail'].includes(state) && !rejected ? check.detail : null);
    }));
    // Sources, HTTP codes and the database path stay one click away.
    find('health-details-list').replaceChildren(...Object.entries(checks).map(([name, check]) => row(name, check.status,
      check.status, [check.detail, check.source ? `(source: ${check.source})` : ''].filter(Boolean).join(' '))));
    if (doctor && !Object.keys(checks).length) find('health-list').append(h('li', {className: 'empty-note'}, 'The server returned no checks.'));
  }
  function renderScan(scan) {
    if (!scan) return;
    const signature = JSON.stringify(scan);
    if (signature === scanSignature) return;
    scanSignature = signature;
    const log = find('scan-log');
    if (log.dataset.jobId !== scan.job_id) { log.replaceChildren(); log.dataset.jobId = scan.job_id; }
    log.hidden = false;
    if (scan.state === 'error') { alert('scan-error', scan.error); return; }
    const message = scan.state === 'done' ? `Done: added ${scan.result?.added ?? 0} · total ${scan.result?.total ?? 0}` :
      scan.progress?.message || 'Scanning your music folder';
    if (log.lastElementChild?.textContent !== message) log.append(h('li', {}, message));
    if (scan.state === 'done') { refreshAfter('scan:' + scan.job_id); refreshDoctor(); }
  }
  function renderLibrary(overview) {
    const sources = overview?.sources;
    librarySummary.hidden = !sources;
    if (!sources) return;
    // imported_at is SQLite's UTC "YYYY-MM-DD HH:MM:SS"; show the local day.
    const from = (row) => row.source === 'file' ? text('S-SET-LIB-SUM-FILE') :
      text(row.imported_at ? 'S-SET-LIB-SUM-IMP' : 'S-SET-LIB-SUM-IMP-NODATE',
        {Source: sourceName(row.source), date: formatDay(String(row.imported_at).replace(' ', 'T') + 'Z')});
    const n = overview.total_tracks.toLocaleString('en-US');
    librarySummary.textContent = !overview.total_tracks ? text('S-SET-LIB-SUM-EMPTY') :
      sources.length === 1 ? text('S-SET-LIB-SUM', {n, from: from(sources[0])}) :
      text('S-SET-LIB-SUM-MIX', {n, parts: sources.map((row) => `${row.count.toLocaleString('en-US')} ${from(row)}`).join('; ')});
  }
  function renderLabels(setup, connected) {
    const enrichment = setup.enrichment, overview = enrichment?.overview, job = currentJob(setup);
    renderLibrary(overview);
    const work = {...job?.progress?.work, ...job?.result};
    const running = job?.state === 'running';
    const noToken = !setup.settings?.discogs_token?.set;
    const count = work.processed_pairs ?? 0, total = work.total_pairs ?? overview?.eligible_pairs ?? 0;
    const params = {n: total, i: count, t: `${work.estimated_seconds ?? Math.ceil(total * 1.1)} s`,
      k: work.filled_tracks ?? 0, u: work.missing_tracks ?? 0};
    let code;
    if (running) code = 'S-SET-FILL-RUN';
    else if (!overview && !job) code = null;
    else if (overview?.total_tracks === 0) code = 'S-SET-FILL-NOLIB';
    else if (noToken) code = 'S-SET-FILL-NOTOKEN';
    else if (job?.error || ['error', 'failed'].includes(job?.state)) code = 'S-SET-FILL-ERR';
    else if (job?.state === 'stopped') code = 'S-SET-FILL-STOPPED';
    else if (job?.state === 'done') code = 'S-SET-FILL-DONE';
    else if (overview?.missing_tracks === 0) code = 'S-SET-FILL-NONE';
    else { code = 'S-SET-FILL-IDLE'; params.n = overview?.missing_tracks; params.m = overview?.total_tracks; }
    status.textContent = code ? text(code, params) : 'Loading library labels…';
    status.dataset.state = running ? 'running' : code?.replace('S-SET-FILL-', '').toLowerCase() || 'loading';
    status.dataset.stringId = code || '';
    progress.hidden = !running;
    progress.max = total || 1;
    progress.value = count;
    progress.setAttribute('aria-valuetext', `${count} of ${total} album lookups`);
    counters.hidden = !job;
    counters.textContent = `${count} of ${total} album lookups · ${params.k} tracks filled · ${params.u} tracks with no label found · ` +
      `${work.ineligible_tracks ?? 0} tracks missing an artist or album. ` +
      `${work.requests_used ?? 0} of ${work.budget ?? budget.value} requests used this run.`;
    const resume = resumeJob(setup);
    detail.textContent = work.reason === 'budget' ?
      `Lookup budget reached. ${work.remaining_pairs ?? Math.max(0, total - count)} album lookups remain; run again to resume.` :
      resume ? 'The next run resumes unfinished album lookups. Track counts include previous runs; the request budget starts fresh.' :
      !running && overview?.missing_tracks > 0 && overview?.eligible_pairs === 0 ?
        'These tracks need an artist and album for a Discogs lookup.' : '';
    if (!budgetInitialized && overview) { budget.value = overview.budget_default; budgetInitialized = true; }
    budget.disabled = running || busy.has('enrichment');
    budgetHelp.textContent = text('S-SET-FILL-BUDGET', {b: running ? work.budget ?? budget.value : budget.value || '100'});
    go.dataset.action = resume ? 'resume-enrichment' : 'start-enrichment';
    go.disabled = !connected || busy.has('enrichment') || running || !setup.settings || !code || noToken ||
      overview?.total_tracks === 0 || overview?.missing_tracks === 0 || overview?.eligible_pairs === 0;
    go.title = !connected ? 'Reconnect to the local server first.' : running ? 'A label lookup is already running.' :
      noToken ? text('S-SET-FILL-NOTOKEN') : go.disabled ? status.textContent + ' ' + detail.textContent :
      resume ? 'Resume unfinished album lookups.' : '';
    stop.hidden = !running;
    stop.disabled = !connected || busy.has('cancel');
    if (job && !running) refreshAfter('labels:' + job.id);
  }
  function render(s) {
    if (destroyed) return;
    const setup = selectors.setupState(s), connected = s.ui.server === 'up';
    renderSettings(setup.settings);
    renderCosine(setup);
    for (const name of ['discogs_token', 'exa_api_key']) renderKey(name, setup);
    renderYouTube(setup);
    renderHealth(setup);
    renderScan(setup.scan);
    renderLabels(setup, connected);
    for (const [id, key] of [['settings-save', 'settings'], ['yt-go', 'login'], ['scan-go', 'scan'],
      ['cosine-check', 'check:cosine_token'], ['discogs-check', 'check:discogs_token'], ['discogs-go', 'check:discogs_token'],
      ['exa-check', 'check:exa_api_key'], ['exa-go', 'check:exa_api_key']]) {
      const button = find(id);
      button.disabled = !connected || busy.has(key) || (key === 'scan' && setup.scan?.state === 'running');
      button.title = !connected ? 'Reconnect to the local server first.' : '';
    }
    find('health-refresh').disabled = busy.has('doctor');
    const active = s.ui.route.page === 'setup' && connected;
    const entering = active && !wasActive;
    wasActive = active;
    if (entering) refreshDoctor();
  }
  const unsubscribe = store.subscribe(render);
  render(store.getState());
  return {destroy() {
    destroyed = true;
    unsubscribe();
    for (const dispose of disposers) dispose();
    labelField.remove();
    dbLine.remove();
  }};
}
