import {selectors, formatRoute} from './state.js';
import {formatWhen, sourceName} from './dom.js';

// Library rows shown per search (the server returns them best match first).
const LIBRARY_ROWS = 8;

function seedLink(query) {
  const value = query.trim();
  try {
    const url = new URL(value);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.port) return null;
    if (!['cosine.club', 'discogs.com', 'youtube.com', 'music.youtube.com', 'youtu.be']
      .includes(url.hostname.replace(/^www\./, ''))) return null;
    return {point_id: `link:${value}`, kind: 'link', name: value, url: value};
  } catch { return null; }
}

function shortLink(value) {
  const url = new URL(value);
  const name = url.host + url.pathname + url.search;
  return name.length > 48 ? name.slice(0, 47) + '…' : name;
}

/** Search and submission authority stays in the store; this mount only renders it. */
export function mountSeedbox(root, ctx) {
  const {store, h, text, clock} = ctx;
  const actions = store.actions;
  let destroyed = false, wasActive = false, rowSerial = 0, actionGesture = false, countdownTimer = null;
  const chipNodes = new Map(), resultNodes = new Map();
  let recentSignature = null, noteSignature = null, blockedSignature = null;

  function singleClick(event) {
    event.stopPropagation();
    event.preventDefault();
    return event.detail <= 1;
  }
  function stopDoubleClick(event) { event.stopPropagation(); event.preventDefault(); }
  function report(error) {
    if (!destroyed) actions.showToast(null, {}, {message: error.message, error: true});
  }
  function focusInput() { if (!destroyed && !root.hidden) input.focus({preventScroll: true}); }
  function isActive(s) { return s.ui.route.page === 'dig' && !s.ui.route.stepId; }
  function repeatedAction(event) {
    if (event.type === 'mousedown' && event.detail === 1) actionGesture = !!event.target.closest('[data-action]');
    if (event.detail <= 1 || !actionGesture) return;
    event.preventDefault();
    event.stopPropagation();
    focusInput();
  }
  // Removing a chip can change the second click's target. Capture the whole gesture.
  for (const name of ['mousedown', 'click', 'dblclick']) root.addEventListener(name, repeatedAction, true);

  const chips = h('div', {className: 'seedbox-chips'});
  const input = h('input', {id: 'seed-input', type: 'text', autocomplete: 'off', spellcheck: 'false',
    role: 'combobox', 'aria-label': text('S-SB-TITLE'), 'aria-autocomplete': 'list',
    'aria-controls': 'seed-results', 'aria-expanded': 'false', 'aria-describedby': 'seed-hint seed-links',
    onfocus: () => actions.focusRegion('search'),
    oninput: () => { actions.searchSeeds(input.value); },
    onkeydown: (event) => {
      if (event.isComposing) return;
      event.stopPropagation();
      const handled = ['Enter', 'ArrowUp', 'ArrowDown', 'Escape'].includes(event.key) ||
        (event.key === 'Backspace' && !input.value);
      if (handled) event.preventDefault();
      if (event.key === 'Enter' && event.repeat) return;
      actions.seedInputKey(event.key);
      if (event.key === 'Escape' && store.getState().ui.focusRegion === 'page') root.focus();
      if (['ArrowUp', 'ArrowDown'].includes(event.key)) {
        const id = input.getAttribute('aria-activedescendant');
        if (id) document.getElementById(id)?.scrollIntoView({block: 'nearest'});
      }
    }});
  const hint = h('p', {id: 'seed-hint', className: 'seedbox-hint', 'aria-live': 'polite'});
  const error = h('div', {className: 'alert seedbox-start-error', role: 'alert', hidden: true});
  const linkRow = h('div', {id: 'seed-link-result', className: 'seedbox-result', role: 'option', hidden: true,
    'data-action': 'add-seed-link', onclick: (event) => {
      if (!singleClick(event)) return;
      const point = seedLink(store.getState().ui.search.query);
      if (point && !store.getState().ui.search.chips.some((chip) => chip.point_id === point.point_id)) actions.addChip(point);
      focusInput();
    }, ondblclick: stopDoubleClick});

  function setupLine(id) {
    const parts = text(id).split('Setup');
    return h('p', {className: 'seedbox-message'}, parts[0], h('a', {href: '#setup'}, 'Setup'), parts[1]);
  }
  function section(source, titleId, asideId) {
    const title = h('h3', {id: `seed-${source}-heading`, className: 'seedbox-section-title'}, text(titleId));
    const pending = h('span', {className: 'seedbox-pending'});
    const rows = h('div', {className: 'seedbox-matches'});
    const message = h('p', {className: 'seedbox-message'});
    const more = h('p', {className: 'seedbox-message', hidden: true}, text('S-SB-MORE'));
    const group = h('div', {className: 'seedbox-section', role: 'group', 'aria-labelledby': title.id,
      'data-source': source}, h('div', {className: 'seedbox-section-bar'}, title,
      asideId && h('span', {className: 'seedbox-aside'}, text(asideId)), pending), rows, message, more);
    return {group, pending, rows, message, more};
  }
  const library = section('library', 'S-SB-SEC-LIB', 'S-SB-SEC-LIB-ASIDE');
  const noLibrary = setupLine('S-SB-NOLIB');
  const cosine = section('cosine', 'S-SB-SEC-COSINE');
  // cosine.club's answer when it isn't results: a key to add or fix, a wait, or no connection.
  const note = h('p', {className: 'seedbox-note', hidden: true});
  cosine.group.insertBefore(note, cosine.more);
  const results = h('div', {id: 'seed-results', role: 'listbox', 'aria-label': text('S-SB-TITLE'), hidden: true},
    linkRow, cosine.group, library.group, noLibrary);
  // Start a dig without a usable key: no dig is created (P11).
  const blocked = h('p', {id: 'seed-blocked', className: 'seedbox-note', role: 'alert', hidden: true});
  const start = h('button', {type: 'button', className: 'btn btn--go', 'data-action': 'start-dig',
    onclick: (event) => { if (singleClick(event)) actions.startDig().catch(report); }, ondblclick: stopDoubleClick});
  function keyButton(kind) {
    return h('button', {type: 'button', className: 'btn btn--small', 'data-action': kind === 'fix' ? 'fix-key' : 'add-key',
      onclick: (event) => { if (singleClick(event)) actions.openConnect().catch(report); }, ondblclick: stopDoubleClick},
    text(kind === 'fix' ? 'S-KEY-FIX' : 'S-KEY-ADD'));
  }
  function waitLeft(value) { return value?.until == null ? 0 : Math.max(0, Math.ceil((value.until - clock.now()) / 1000)); }
  function noteParts(value, s) {
    switch (value.state) {
      case 'missing': return ['! ' + text('S-SB-NOKEY'), ' ', keyButton('add')];
      case 'rejected': return ['! ' + text('S-SB-KEY-REJECTED'), ' ', keyButton('fix')];
      case 'unreachable':
        return ['! ' + text('S-SB-UNREACHABLE') + (selectors.hasLibrary(s) ? ' ' + text('S-SB-LIB-STILL') : '')];
      case 'busy': case 'server_error': {
        const left = waitLeft(value), id = value.state === 'busy' ? 'S-SB-BUSY' : 'S-SB-SERVER-ERR';
        if (left) return ['! ' + text(id, {s: left})];
        // The wait is over and the one automatic rerun was spent: the user decides.
        return ['! ' + text(id + '-NOW'), ' ', h('button', {type: 'button', className: 'btn btn--small', 'data-action': 'search-again',
          onclick: (event) => { if (singleClick(event)) actions.searchSeeds(store.getState().ui.search.query); focusInput(); },
          ondblclick: stopDoubleClick}, text('S-KEY-TRY-AGAIN'))];
      }
      default: return ['! ' + text('S-SB-DIGGER-ERR', {reason: value.params?.reason || 'unknown error'})];
    }
  }
  const recentRows = h('div', {className: 'seedbox-recent-rows'});
  const recent = h('section', {className: 'seedbox-recent', hidden: true, 'aria-labelledby': 'seed-recent-heading'},
    h('h2', {id: 'seed-recent-heading', className: 'seedbox-section-title'}, text('S-SB-RECENT')), recentRows);
  const lede = h('p', {className: 'seedbox-lede'}, text('S-SB-LEDE'));
  root.tabIndex = -1;
  root.replaceChildren(h('section', {className: 'panel seedbox-panel'},
    h('div', {className: 'panel__bar'}, h('h1', {className: 'panel__title'}, text('S-SB-TITLE')),
      h('span', {className: 'seedbox-aside'}, text('S-SB-ASIDE'))),
    h('div', {className: 'seedbox-body'}, lede,
      h('div', {className: 'seedbox-input-row'}, chips, input, h('kbd', {className: 'seedbox-slash'}, '/')),
      hint, blocked, error, results,
      h('p', {id: 'seed-links', className: 'seedbox-links'}, text('S-SB-LINKS')),
      h('div', {className: 'seedbox-start-row'}, start, h('span', {className: 'seedbox-aside'}, text('S-SB-BTN-HINT'))), recent)));

  // Keep existing nodes in place so store updates preserve focus and pointer targets.
  function reconcile(parent, nodes) {
    nodes.forEach((node, index) => {
      if (parent.children[index] !== node) parent.insertBefore(node, parent.children[index] || null);
    });
    while (parent.children.length > nodes.length) parent.lastElementChild.remove();
  }
  function renderChips(search) {
    const ids = new Set(search.chips.map((point) => point.point_id));
    for (const [id, node] of chipNodes) if (!ids.has(id)) { node.remove(); chipNodes.delete(id); }
    reconcile(chips, search.chips.map((point) => {
      let node = chipNodes.get(point.point_id);
      if (!node) {
        const label = point.kind === 'link' && seedLink(point.url || point.name) ? shortLink(point.url || point.name) : point.name;
        const warning = point.library_id != null && point.indexed === false;
        node = h('span', {className: 'seedbox-chip', 'data-point-id': point.point_id,
          'data-indexed': String(point.indexed ?? 'unknown'),
          title: warning ? text('S-SB-CHIP-NOTIDX', {artist: point.artist}) : point.name},
          warning && h('span', {className: 'seedbox-chip-warning', 'aria-hidden': 'true'}, '!'),
          h('span', {className: 'seedbox-chip-name', title: point.name}, label),
          h('button', {type: 'button', className: 'seedbox-chip-remove', 'data-action': 'remove-seed',
            'aria-label': text('S-SB-CHIP-X', {chipText: point.name}), onclick: (event) => {
              if (singleClick(event)) actions.removeChip(point.point_id);
              focusInput();
            }, ondblclick: stopDoubleClick}, '×'));
        chipNodes.set(point.point_id, node);
      }
      return node;
    }));
  }
  function renderRows(parent, points, search, source) {
    reconcile(parent, points.map((point) => {
      let row = resultNodes.get(point.point_id);
      if (!row) {
        const label = h('span', {className: 'seedbox-result-name'});
        const detail = h('span', {className: 'seedbox-result-detail'});
        const added = h('span', {className: 'seedbox-result-added'}, text('S-SB-ADDED'));
        const node = h('div', {id: `seed-result-${++rowSerial}`, className: 'seedbox-result', role: 'option',
          'data-action': 'add-seed', 'data-point-id': point.point_id,
          onpointermove: () => actions.highlightSeed(point.point_id), onclick: (event) => {
            if (!singleClick(event)) return;
            if (!store.getState().ui.search.chips.some((chip) => chip.point_id === point.point_id)) actions.addChip(row.point);
            focusInput();
          }, ondblclick: stopDoubleClick}, h('span', {className: 'seedbox-result-text'}, label, detail), added);
        row = {node, label, detail, added};
        resultNodes.set(point.point_id, row);
      }
      row.point = point;
      row.label.textContent = point.name;
      row.node.title = point.name;
      row.node.dataset.source = source;
      row.node.dataset.indexed = String(point.indexed ?? 'unknown');
      row.detail.hidden = source !== 'library';
      // Scanned files read "your file"; imported rows name their catalogue ("in your Plex library").
      const imported = point.library_source && point.library_source !== 'file';
      const libraryId = point.indexed === true ? 'S-SB-LIB-IDX' : point.indexed === false ? 'S-SB-LIB-NOTIDX' : 'S-SB-LIB-IDX-UNKNOWN';
      row.detail.textContent = source === 'library' ? text(imported ? libraryId + '-IMP' : libraryId,
        {artist: point.artist, Source: sourceName(point.library_source)}) : '';
      const added = search.chips.some((chip) => chip.point_id === point.point_id);
      row.added.hidden = !added;
      row.node.setAttribute('aria-disabled', String(added));
      row.node.setAttribute('aria-selected', String(!added && !seedLink(search.query) &&
        selectors.seedResults(store.getState())[search.highlight]?.point_id === point.point_id));
      return row.node;
    }));
  }
  function render(s) {
    if (destroyed) return;
    const active = isActive(s), search = s.ui.search;
    root.hidden = !active;
    if (active && !wasActive) queueMicrotask(() => {
      if (destroyed || !isActive(store.getState())) return;
      focusInput();
      actions.loadSetup().catch(report);
    });
    wasActive = active;
    if (input.value !== search.query) input.value = search.query;
    input.placeholder = text(search.chips.length ? 'S-SB-PH-N' : 'S-SB-PH-0');
    lede.hidden = s.digs.order.length > 0;
    renderChips(search);
    hint.textContent = search.hint?.id ? text(search.hint.id, search.hint.params) : '';
    hint.hidden = !hint.textContent;
    const blockedState = search.blocked && !selectors.cosineKey(s).usable ? search.blocked : null;
    blocked.hidden = !blockedState;
    if (blockedState !== blockedSignature) {
      blockedSignature = blockedState;
      blocked.replaceChildren(...(blockedState === 'missing' ? [text('S-SB-DIG-NOKEY'), ' ', keyButton('add')] :
        blockedState ? ['! ' + text('S-SB-KEY-REJECTED'), ' ', keyButton('fix')] : []));
    }
    error.hidden = s.ui.toast?.id !== 'S-SB-START-ERR';
    error.textContent = error.hidden ? '' : text(s.ui.toast.id, s.ui.toast.params);
    input.setAttribute('aria-describedby', 'seed-hint seed-links' + (error.hidden ? '' : ' seed-start-error'));
    error.id = 'seed-start-error';
    results.hidden = !search.resultsOpen;
    results.setAttribute('aria-busy', String(search.pending.library || search.pending.cosine));
    input.setAttribute('aria-expanded', String(search.resultsOpen));
    const link = seedLink(search.query);
    linkRow.hidden = !link;
    if (link) {
      const added = search.chips.some((point) => point.point_id === link.point_id);
      linkRow.textContent = text('S-SB-LINK-ROW', {'host/path, 48 characters max': shortLink(link.url)}) +
        (added ? ' · ' + text('S-SB-ADDED') : '');
      linkRow.title = link.url;
      linkRow.dataset.pointId = link.point_id;
      linkRow.setAttribute('aria-disabled', String(added));
      linkRow.setAttribute('aria-selected', String(!added));
    }
    const allRows = selectors.seedResults(s), libraryRows = allRows.filter((point) => search.library.some((item) => item.point_id === point.point_id));
    const cosineRows = allRows.filter((point) => !libraryRows.includes(point));
    const noFiles = selectors.setupState(s).enrichment?.overview?.total_tracks === 0;
    library.group.hidden = noFiles;
    // The first screen is the lede, the box and the link line; the library line waits for results.
    noLibrary.hidden = !noFiles;
    renderRows(library.rows, libraryRows.slice(0, LIBRARY_ROWS), search, 'library');
    renderRows(cosine.rows, cosineRows.slice(0, 8), search, 'cosine');
    const visibleIds = new Set([...libraryRows.slice(0, LIBRARY_ROWS), ...cosineRows.slice(0, 8)].map((point) => point.point_id));
    for (const [id] of resultNodes) if (!visibleIds.has(id)) resultNodes.delete(id);
    library.pending.textContent = search.pending.library ? '~' : '';
    library.message.hidden = search.pending.library || libraryRows.length > 0;
    library.message.textContent = text('S-SB-LIB-NOMATCH');
    cosine.pending.textContent = search.pending.cosine ? text('S-SB-SEARCHING') : '';
    const cosineNote = search.pending.cosine ? null : search.note;
    cosine.message.hidden = search.pending.cosine || cosineRows.length > 0 || !!cosineNote;
    cosine.message.classList.toggle('seedbox-warning', !search.pending.cosine);
    cosine.message.textContent = text('S-SB-NOMATCH', {query: search.query.trim()});
    note.hidden = !cosineNote;
    const noteKey = cosineNote && JSON.stringify([cosineNote.state, cosineNote.params, waitLeft(cosineNote), selectors.hasLibrary(s)]);
    if (noteKey !== noteSignature) {
      noteSignature = noteKey;
      note.replaceChildren(...(cosineNote ? noteParts(cosineNote, s) : []));
    }
    clock.clearTimeout(countdownTimer);
    countdownTimer = active && waitLeft(cosineNote) ? clock.setTimeout(() => render(store.getState()), 1000) : null;
    cosine.more.hidden = cosineRows.length <= 8;
    const highlighted = link && linkRow.getAttribute('aria-disabled') === 'false' ? linkRow :
      resultNodes.get(allRows[search.highlight]?.point_id)?.node;
    if (search.resultsOpen && highlighted?.isConnected) input.setAttribute('aria-activedescendant', highlighted.id);
    else input.removeAttribute('aria-activedescendant');
    start.disabled = !search.chips.length || search.submitting || s.ui.server !== 'up';
    start.textContent = text(search.submitting ? 'S-SB-STARTING' : 'S-SB-BTN', {n: search.chips.length, seeds: search.chips});

    const digs = s.digs.order.map((id) => s.digs.byId[id]).filter(Boolean)
      .sort((a, b) => b.created_at.localeCompare(a.created_at) || a.id.localeCompare(b.id)).slice(0, 5);
    recent.hidden = !digs.length;
    const routes = digs.map((dig) => ({dig: {...dig,
      kept_count: s.steps.byDig[dig.id] ? selectors.keepsForDig(s, dig.id).length : dig.kept_count},
      stepId: s.ui.lastStepByDig[dig.id] || dig.last_step_id || dig.root_step_id}));
    const signature = JSON.stringify(routes);
    if (signature !== recentSignature) {
      recentSignature = signature;
      recentRows.replaceChildren(...routes.map(({dig, stepId}) => {
        const route = {page: 'dig', digId: dig.id, stepId, view: 'list'};
        return h('a', {className: 'seedbox-recent-link', href: formatRoute(route), 'data-action': 'open-dig', 'data-dig-id': dig.id,
          'data-step-id': stepId, onclick: (event) => {
            event.stopPropagation();
            if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
            if (singleClick(event)) actions.openRoute(route).catch(report);
          }, ondblclick: stopDoubleClick}, h('strong', {title: dig.name}, dig.name),
          h('span', {className: 'seedbox-aside'}, text('S-SB-RECENT-META',
            {when: formatWhen(dig.created_at), s: dig.step_count, k: dig.kept_count})));
      }));
    }
  }
  const unsubscribe = store.subscribe(render);
  render(store.getState());
  return {destroy() {
    if (destroyed) return;
    destroyed = true;
    unsubscribe();
    clock.clearTimeout(countdownTimer);
    for (const name of ['mousedown', 'click', 'dblclick']) root.removeEventListener(name, repeatedAction, true);
    chipNodes.clear(); resultNodes.clear();
    root.replaceChildren();
  }};
}
