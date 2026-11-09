import {selectors, formatRoute, youtubeLoginProblem} from './state.js';
import {formatWhen} from './dom.js';

const nameOf = (pick) => pick.artist ? `${pick.artist} - ${pick.title}` : pick.title;

/** Why sending this dig to YouTube Music needs Setup first: 'missing', 'rejected' or null. */
function youtubeLogin(s, digId) {
  const sent = s.ui.digExports[digId], youtube = s.ui.settings?.youtube;
  return youtubeLoginProblem(sent?.state === 'error' ? sent.error : null) ||
    (youtube && !youtube.saved ? 'missing' : youtube?.checked === 'rejected' ? 'rejected' : null);
}

export function mountTrail(root, {store, h, text, safeUrl, mounts}) {
  const records = new Map(), keptRecords = new Map(), menuRecords = new Map(), disposers = [];
  const narrow = matchMedia('(max-width: 1279px)');
  let focusKey = null, restoringFocus = false, switcherOpen = false, destroyed = false;
  const name = h('h2', {className: 'trail-name'});
  const switchButton = h('button', {type: 'button', className: 'btn btn--small trail-switch',
    'data-action': 'switch', 'aria-label': text('S-TR-SWITCH'), 'aria-haspopup': 'menu',
    'aria-expanded': false, 'aria-controls': 'trail-switcher'}, '▾');
  const meta = h('p', {className: 'trail-meta'});
  // The dig as a private YouTube Music playlist: it plays on in another tab, where Digger's player pauses.
  const ytOpen = h('a', {className: 'btn btn--small btn--yt trail-yt-open', target: '_blank', hidden: true},
    text('S-TR-YT-OPEN'));
  const ytSend = h('button', {type: 'button', className: 'btn btn--small trail-yt-send', 'data-action': 'youtube'});
  const ytStatus = h('p', {className: 'trail-yt-status', hidden: true});
  const youtube = h('div', {className: 'trail-yt'}, ytOpen, ytSend, ytStatus);
  const header = h('header', {className: 'trail-header'}, h('div', {className: 'trail-heading'}, name, switchButton),
    meta, youtube);
  const tree = h('div', {className: 'trail-tree', role: 'tree', 'aria-label': 'Trail'});
  const keptLabel = h('span');
  const keptGlyph = h('span', {'aria-hidden': 'true'});
  const keptButton = h('button', {type: 'button', className: 'trail-kept-toggle',
    'data-action': 'kept', 'aria-controls': 'trail-kept-list', 'aria-expanded': false}, keptGlyph, keptLabel);
  const keptList = h('div', {id: 'trail-kept-list', className: 'trail-kept-list'});
  const kept = h('section', {className: 'trail-kept'}, keptButton, keptList);
  const menu = h('div', {id: 'trail-switcher', className: 'trail-switcher', role: 'menu',
    'aria-label': text('S-TR-SWITCH'), hidden: true});
  const panel = h('div', {className: 'trail-panel'},
    h('div', {className: 'trail-scroll'}, header, tree, kept),
    h('p', {className: 'trail-legend'}, text('S-TR-LEGEND')), menu);
  root.classList.add('trail');
  root.replaceChildren(panel);

  function listen(target, event, handler) {
    target.addEventListener(event, handler);
    disposers.push(() => target.removeEventListener(event, handler));
  }
  function run(result) {
    Promise.resolve(result).catch((error) => {
      if (!destroyed) store.actions.showToast(null, {}, {message: error.message, error: true});
    });
  }
  // A first send opens its playlist: the tab opens inside the click (browsers block a later one)
  // and goes to the playlist once the send finishes, or closes if it fails.
  const playlistTabs = new Map();
  function openPlaylistTab(digId) {
    const tab = window.open('', '_blank');
    if (!tab) return;
    tab.document.title = text('S-TR-YT-TAB');
    tab.document.body.style.cssText = 'margin: 0; padding: 32px; background: #07080c; color: #e6e9f1; font: 16px system-ui, sans-serif;';
    tab.document.body.textContent = text('S-TR-YT-TAB-WAIT');
    playlistTabs.set(digId, tab);
  }
  function followPlaylistTabs(s) {
    for (const [digId, tab] of playlistTabs) {
      const sent = s.ui.digExports[digId];
      if (sent?.state === 'running') continue;
      playlistTabs.delete(digId);
      const url = sent?.state === 'done' ? safeUrl(sent.result?.playlist_url) : null;
      if (tab.closed) continue;
      if (url) { tab.opener = null; tab.location.replace(url); } else tab.close();
    }
  }
  function connectYouTube() {
    // Setup's YouTube Music card, at its quickest way in: Log in from Chrome, else the headers form.
    run(store.actions.openRoute({page: 'setup'}).then(() => {
      if (destroyed) return;
      mounts.cardYoutube.scrollIntoView({block: 'nearest', behavior: 'auto'});
      if (!mounts.ytChromeBlock.hidden) mounts.ytChrome.focus({preventScroll: true});
      else if (mounts.ytForm.hidden) mounts.ytConnect.click();
      else mounts.ytHeaders.focus();
    }));
  }
  async function openStep(digId, stepId, sel) {
    const s = store.getState(), same = s.ui.route.digId === digId && s.ui.route.stepId === stepId;
    if (same && sel === undefined) return;
    const target = await store.actions.openRoute({page: 'dig', digId, stepId,
      view: s.ui.route.view, ...(sel === undefined ? {} : {sel})}, {history: same ? 'replace' : 'push'});
    if (target && sel && store.getState().ui.route.stepId === stepId && store.getState().ui.route.sel === sel) {
      const point = selectors.selectedPoint(store.getState(), stepId);
      if (point) store.actions.selectPoint(stepId, point, {scroll: true});
    }
    if (target && root.contains(document.activeElement)) store.actions.focusRegion('trail');
  }
  function focusRow(node) {
    if (!node) return;
    focusKey = node.dataset.key;
    for (const item of tree.children) item.tabIndex = item === node ? 0 : -1;
    node.focus({preventScroll: true});
    node.scrollIntoView({block: 'nearest', inline: 'nearest', behavior: 'auto'});
  }
  function setRail(open) {
    if (narrow.matches && store.getState().ui.trailRail !== open) store.actions.setTrailRail(open);
  }
  function reconcile(container, nodes) {
    nodes.forEach((node, i) => {
      if (container.children[i] !== node) container.insertBefore(node, container.children[i] || null);
    });
    const retained = new Set(nodes);
    for (const node of [...container.children]) if (!retained.has(node)) node.remove();
  }

  listen(root, 'click', (event) => {
    const control = event.target.closest('[data-action]');
    if (!control || !root.contains(control) || event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault(); event.stopPropagation();
    if (event.detail > 1) return;
    const s = store.getState();
    switch (control.dataset.action) {
      case 'step': focusRow(control); run(openStep(s.ui.route.digId, control.dataset.stepId)); break;
      case 'branch': {
        const child = (s.steps.byDig[s.ui.route.digId] || []).find((id) => s.steps.byId[id].parent_id === control.dataset.stepId);
        focusKey = child ? `step:${child}` : `step:${control.dataset.stepId}`;
        store.actions.toggleBranch(control.dataset.stepId);
        focusRow(records.get(focusKey)?.node);
        break;
      }
      case 'kept': store.actions.toggleKeptList(s.ui.route.digId); break;
      case 'youtube': {
        const digId = s.ui.route.digId;
        if (youtubeLogin(s, digId)) { store.actions.clearDigExport(digId); connectYouTube(); break; }
        if (!s.digs.byId[digId]?.youtube_playlist_url && s.ui.digExports[digId]?.state !== 'running') openPlaylistTab(digId);
        run(store.actions.exportDig(digId));
        break;
      }
      case 'switch': store.actions.setSwitcher(!s.ui.switcher); break;
      case 'new': run(store.actions.openNewDig()); break;
      case 'dig': run(store.actions.openRoute({page: 'dig', digId: control.dataset.digId, view: s.ui.route.view})); break;
      case 'open-kept': run(openStep(control.dataset.digId, control.dataset.stepId, control.dataset.pointId)); break;
    }
  });
  listen(root, 'dblclick', (event) => { event.preventDefault(); event.stopPropagation(); });
  listen(root, 'keydown', (event) => {
    if (event.defaultPrevented || event.isComposing || event.metaKey || event.ctrlKey || event.altKey) return;
    const row = event.target.closest('[role="treeitem"]');
    if (row && ['ArrowUp', 'ArrowDown', 'Home', 'End', 'Enter'].includes(event.key)) {
      event.preventDefault(); event.stopPropagation();
      const items = [...tree.children], index = items.indexOf(row);
      if (event.key === 'Enter') { if (!event.repeat) row.click(); }
      else focusRow(items[event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 :
        Math.max(0, Math.min(items.length - 1, index + (event.key === 'ArrowDown' ? 1 : -1)))]);
    } else if (menu.contains(event.target) && ['ArrowUp', 'ArrowDown', 'Home', 'End', 'Escape'].includes(event.key)) {
      event.preventDefault(); event.stopPropagation();
      if (event.key === 'Escape') store.actions.setSwitcher(false);
      else {
        const items = [...menu.children], index = items.indexOf(document.activeElement);
        const next = items[event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 :
          Math.max(0, Math.min(items.length - 1, index + (event.key === 'ArrowDown' ? 1 : -1)))];
        next?.focus({preventScroll: true}); next?.scrollIntoView({block: 'nearest', behavior: 'auto'});
      }
    }
  });
  listen(root, 'pointerenter', () => setRail(true));
  listen(root, 'pointerleave', () => { if (!root.contains(document.activeElement)) setRail(false); });
  listen(root, 'focusin', (event) => {
    if (event.target.matches('[role="treeitem"]')) {
      focusKey = event.target.dataset.key;
      for (const node of tree.children) node.tabIndex = node === event.target ? 0 : -1;
    }
    if (!restoringFocus) setRail(true);
    if (store.getState().ui.focusRegion !== 'trail') store.actions.focusRegion('trail');
  });
  listen(root, 'focusout', (event) => {
    if (root.contains(event.relatedTarget)) return;
    if (store.getState().ui.switcher) store.actions.setSwitcher(false);
    setRail(false);
  });
  listen(document, 'pointerdown', (event) => {
    if (!root.contains(event.target) && store.getState().ui.switcher) store.actions.setSwitcher(false);
  });
  listen(narrow, 'change', () => {
    if (!narrow.matches && store.getState().ui.trailRail) store.actions.setTrailRail(false);
    render(store.getState());
  });

  function createRow(row) {
    const number = h('span', {className: 'trail-number', 'aria-hidden': 'true'});
    const label = h('span', {className: 'trail-row-name'});
    const changed = h('span', {className: 'trail-changed'}, 'changed');
    const kind = h('span', {className: 'trail-kind'});
    const further = h('span', {className: 'trail-further', title: 'one step further'}, '»');
    const playing = h('span', {className: 'trail-playing'}, '▶');
    const keeps = h('span', {className: 'trail-keeps'});
    const passes = h('span', {className: 'trail-passes'});
    const status = h('span', {className: 'trail-status'});
    const rail = h('span', {className: 'trail-rail-marker', 'aria-hidden': 'true'});
    const node = h(row.type === 'stub' ? 'button' : 'a', {className: 'trail-row', role: 'treeitem', tabindex: -1,
      type: row.type === 'stub' ? 'button' : null, 'data-step-id': row.stepId,
      'data-key': `${row.type}:${row.stepId}`, 'data-action': row.type === 'stub' ? 'branch' : 'step'},
    number, label, changed, kind, further,
    h('span', {className: 'trail-markers'}, playing, keeps, passes, status), rail);
    return {node, number, label, changed, kind, further, playing, keeps, passes, status, rail};
  }
  function renderTree(s, digId) {
    const steps = (s.steps.byDig[digId] || []).map((id) => s.steps.byId[id]);
    const children = (id) => steps.filter((step) => step.parent_id === id);
    const rows = selectors.trailRows(s, digId), nodes = [];
    const active = tree.contains(document.activeElement), oldIndex = [...tree.children].indexOf(document.activeElement);
    for (const row of rows) {
      const key = `${row.type}:${row.stepId}`;
      if (!records.has(key)) records.set(key, createRow(row));
      const r = records.get(key), {node} = r;
      node.dataset.type = row.type;
      node.style.setProperty('--trail-depth', Math.min(4, row.depth));
      node.setAttribute('aria-level', row.depth + 1);
      if (row.type === 'stub') {
        const hidden = [], visit = (id) => children(id).forEach((step) => { hidden.push(step); visit(step.id); });
        visit(row.stepId);
        const k = new Set(hidden.flatMap((step) => selectors.keepsForStep(s, step.id).map((pick) => pick.track_key))).size;
        const label = text('S-TR-STUB', {n: hidden.length, k});
        r.number.textContent = '+'; r.label.textContent = label;
        node.setAttribute('aria-label', label); node.setAttribute('aria-expanded', 'false');
        node.title = label;
      } else {
        const step = row.step, counts = row.counts || {picks: 0, kept: 0, passed: 0};
        const playing = row.playing && s.player.state !== 'idle';
        const label = !step.parent_id ? text(step.origins.length > 1 ? 'S-TR-ROOT-N' : 'S-TR-ROOT-1', {n: step.origins.length}) :
          step.move === 'these' ? text('S-TR-THESE', {k: step.origins.length}) :
            step.origins[0]?.title || step.origins[0]?.name || step.header;
        const kind = step.move === 'these' ? 'keeps' : step.move === 'seed' ? 'track' : step.move;
        const target = step.further ? 50 : 25;
        const status = step.status === 'failed' ? text('S-HDR-TAG-FAILED') : step.status === 'stopped' ? text('S-HDR-TAG-STOPPED') :
          row.picksComplete ? String(counts.picks) : `~ ${counts.picks}/${target}`;
        r.number.textContent = (row.depth > 4 ? '↳' : '') + step.number;
        r.label.textContent = label; r.kind.textContent = kind;
        r.changed.hidden = !step.rerun_of; r.further.hidden = !step.further; r.playing.hidden = !playing;
        r.keeps.textContent = counts.kept ? `★${counts.kept}` : '';
        r.passes.textContent = counts.passed ? `✕${counts.passed}` : '';
        r.status.textContent = status; r.status.dataset.status = step.status;
        r.rail.textContent = playing ? '▶' : !row.picksComplete ? '~' :
          step.status === 'failed' ? '!' : step.status === 'stopped' ? '‖' : '';
        node.href = formatRoute({page: 'dig', digId, stepId: step.id, view: s.ui.route.view});
        node.setAttribute('aria-label', `${step.number}. ${label} · ${kind}${step.rerun_of ? ' · changed' : ''}${
          step.further ? ' · one step further' : ''}${playing ? ' · holds the playing track' : ''} · ${
          counts.kept ? `${counts.kept} kept · ` : ''}${counts.passed ? `${counts.passed} passed · ` : ''}${status}`);
        if (row.selected) node.setAttribute('aria-current', 'true'); else node.removeAttribute('aria-current');
        if (children(step.id).length) node.setAttribute('aria-expanded', String(
          rows.some((item) => item.type === 'step' && item.step.parent_id === step.id)));
        else node.removeAttribute('aria-expanded');
        node.title = text('S-TR-TIP', {Sentence: step.header, c: counts.picks, k: counts.kept, p: counts.passed,
          'HH:MM': formatWhen(step.started_at)});
      }
      nodes.push(node);
    }
    if (!active || !nodes.some((node) => node.dataset.key === focusKey))
      focusKey = active ? nodes[Math.max(0, Math.min(nodes.length - 1, oldIndex))]?.dataset.key : `step:${s.ui.route.stepId}`;
    nodes.forEach((node) => { node.tabIndex = node.dataset.key === focusKey ? 0 : -1; });
    reconcile(tree, nodes);
    for (const [key, r] of records) if (!nodes.includes(r.node)) records.delete(key);
    if (active && !tree.contains(document.activeElement)) focusRow(records.get(focusKey)?.node);
  }
  function renderKept(s, digId) {
    const picks = selectors.keepsForDig(s, digId), open = !!s.ui.keptOpenByDig[digId];
    keptLabel.textContent = text('S-TR-KEPT-H', {k: picks.length});
    keptGlyph.textContent = open ? '▾' : '▸'; keptButton.setAttribute('aria-expanded', String(open));
    if (!open && keptList.contains(document.activeElement)) keptButton.focus({preventScroll: true});
    keptList.hidden = !open;
    const nodes = picks.map((pick) => {
      const marks = selectors.marks(s, pick.id), source = marks.judgment.step_id;
      const saved = s.steps.byId[source];
      const occurrence = saved ? {step_id: saved.id, dig_id: saved.dig_id, number: saved.number,
        dig_name: s.digs.byId[saved.dig_id]?.name} : marks.also_in.find((item) => item.step_id === source);
      const target = occurrence || {step_id: pick.step_id, dig_id: digId, number: s.steps.byId[pick.step_id].number};
      const provenance = marks.judgment.source === 'legacy' || !occurrence ? text('S-ROW-KEPT-PREVIOUSLY') :
        text('S-ROW-KEPT-IN', {n: target.number});
      if (!keptRecords.has(pick.track_key)) {
        const title = h('span', {className: 'trail-kept-name'}), step = h('span', {className: 'trail-kept-step'});
        const detail = h('span', {className: 'trail-kept-detail'});
        const node = h('a', {className: 'trail-kept-row', 'data-action': 'open-kept'},
          h('span', {className: 'trail-kept-line'}, h('span', {className: 'trail-kept-star', 'aria-hidden': 'true'}, '★ '), title, step), detail);
        keptRecords.set(pick.track_key, {node, title, step, detail});
      }
      const {node, title, step, detail} = keptRecords.get(pick.track_key);
      const label = text('S-TR-KEPT-ROW', {'Artist - Title': nameOf(pick), n: target.number});
      const full = `${label}${target.dig_id !== digId ? ` · ${target.dig_name}` : ''} · ${provenance}`;
      title.textContent = nameOf(pick);
      step.textContent = text('S-TR-KEPT-ROW', {'Artist - Title': '', n: target.number});
      detail.textContent = `${target.dig_id !== digId ? ` · ${target.dig_name}` : ''} · ${provenance}`;
      detail.hidden = marks.judgment.source !== 'legacy' && !!occurrence && target.dig_id === digId;
      node.title = full; node.setAttribute('aria-label', full);
      node.dataset.stepId = target.step_id; node.dataset.digId = target.dig_id; node.dataset.pointId = `track:${pick.track_key}`;
      node.href = formatRoute({page: 'dig', digId: target.dig_id, stepId: target.step_id, sel: node.dataset.pointId, view: s.ui.route.view});
      return node;
    });
    if (!nodes.length) nodes.push(h('p', {className: 'trail-kept-empty'}, text('S-TR-KEPT-EMPTY')));
    if (keptList.contains(document.activeElement) && !nodes.includes(document.activeElement)) keptButton.focus({preventScroll: true});
    reconcile(keptList, nodes);
    for (const [key, record] of keptRecords) if (!nodes.includes(record.node)) keptRecords.delete(key);
  }
  function renderSwitcher(s) {
    const options = [{id: 'new', label: text('S-TR-NEW'), action: 'new', href: formatRoute({page: 'dig'})},
      ...s.digs.order.slice(0, 10).map((id) => {
        const dig = s.digs.byId[id], loaded = !!s.steps.byDig[id];
        return {id, action: 'dig', href: formatRoute({page: 'dig', digId: id,
          stepId: s.ui.lastStepByDig[id] || dig.last_step_id || dig.root_step_id, view: s.ui.route.view}),
        label: text('S-TR-SWITCH-ROW', {digName: dig.name, when: formatWhen(dig.created_at),
          s: loaded ? s.steps.byDig[id].length : dig.step_count, k: loaded ? selectors.keepsForDig(s, id).length : dig.kept_count})};
      })];
    const nodes = options.map((item) => {
      if (!menuRecords.has(item.id)) menuRecords.set(item.id, h('a', {role: 'menuitem', className: 'trail-switcher-row'}));
      const node = menuRecords.get(item.id);
      node.textContent = item.label; node.title = item.label; node.href = item.href;
      node.dataset.action = item.action; node.dataset.digId = item.id;
      node.tabIndex = -1;
      return node;
    });
    reconcile(menu, nodes);
    for (const [id, node] of menuRecords) if (!nodes.includes(node)) menuRecords.delete(id);
    switchButton.setAttribute('aria-expanded', String(s.ui.switcher));
    menu.hidden = !s.ui.switcher;
    const opening = s.ui.switcher && !switcherOpen, closing = !s.ui.switcher && switcherOpen;
    switcherOpen = s.ui.switcher;
    if (opening) nodes[0]?.focus({preventScroll: true});
    else if (closing && menu.contains(document.activeElement)) switchButton.focus({preventScroll: true});
  }
  function renderYouTube(s, dig) {
    const sent = s.ui.digExports[dig.id], url = safeUrl(dig.youtube_playlist_url), login = youtubeLogin(s, dig.id);
    const sending = sent?.state === 'running', result = sent?.state === 'done' ? sent.result : null;
    const failure = sent?.state === 'error' ? String(sent.error || '') : null;
    // Update fits beside the status line; Send and Connect take the full width.
    youtube.dataset.layout = url && !login ? 'row' : 'stack';
    ytOpen.hidden = !url;
    if (url && ytOpen.href !== url) ytOpen.href = url;
    ytOpen.title = text('S-TR-YT-OPEN-TIP', {digName: dig.name});
    ytSend.textContent = text(sending ? 'S-TR-YT-SENDING' : login ? 'S-TR-YT-CONNECT' : url ? 'S-TR-YT-UPDATE' : 'S-TR-YT-SEND');
    ytSend.title = text(login ? 'S-TR-YT-CONNECT-TIP' : url ? 'S-TR-YT-UPDATE-TIP' : 'S-TR-YT-SEND-TIP');
    ytSend.classList.toggle('btn--yt', !url && !login);
    ytSend.disabled = sending;
    // Steps whose picks finished after the last send read the dig, or are still arriving, aren't in
    // the playlist. This tab's own send knows its cutoff; otherwise the server's.
    const sentAt = Date.parse(result?.sent_at || dig.youtube_sent_at), behind = !url || !sentAt ? 0 : (s.steps.byDig[dig.id] || [])
      .map((id) => s.steps.byId[id]).filter((step) => !(Date.parse(step.picks_done_at || step.finished_at) <= sentAt)).length;
    // What this session's send did, else when the dig was last sent.
    const last = result ? [text('S-TR-YT-DONE', {n: result.added}),
      result.passed && text('S-TR-YT-PASSED', {p: result.passed}),
      result.no_video && text('S-TR-YT-NOVIDEO', {v: result.no_video}),
      result.missing && text('S-TR-YT-MISSING', {m: result.missing}),
      !result.verified && text('S-TR-YT-UNVERIFIED')] :
      [sentAt && text('S-TR-YT-SENT-AT', {when: formatWhen(dig.youtube_sent_at)})];
    const status = sending ? [text(url ? 'S-TR-YT-UPDATING' : 'S-TR-YT-WORKING')] :
      failure !== null ? [login ? text(login === 'rejected' ? 'S-SET-YT-REJECTED' : 'S-FEEDS-LOGIN-MISSING') : failure] :
      [...last, behind && text('S-TR-YT-BEHIND', {n: behind})];
    ytStatus.textContent = status.filter(Boolean).join(' · ');
    ytStatus.hidden = !ytStatus.textContent;
    ytStatus.dataset.tone = sending ? '' : failure !== null ? 'error' : behind || (result && !result.verified) ? 'warn' : '';
  }
  function render(s) {
    followPlaylistTabs(s);
    const dig = selectors.viewedDig(s);
    if (!dig) { menu.hidden = true; switcherOpen = false; return; }
    if (narrow.matches && !s.ui.trailRail && root.contains(document.activeElement) && !tree.contains(document.activeElement)) {
      restoringFocus = true; focusRow(records.get(`step:${s.ui.route.stepId}`)?.node); restoringFocus = false;
    }
    root.dataset.rail = String(narrow.matches); root.dataset.expanded = String(!!s.ui.trailRail);
    name.textContent = dig.name; name.title = dig.name;
    meta.textContent = text('S-TR-META', {s: dig.step_count, k: dig.kept_count});
    renderYouTube(s, dig); renderTree(s, dig.id); renderKept(s, dig.id); renderSwitcher(s);
  }
  const unsubscribe = store.subscribe(render);
  render(store.getState());
  return {destroy() {
    if (destroyed) return;
    destroyed = true; unsubscribe(); disposers.forEach((dispose) => dispose());
    root.replaceChildren(); root.classList.remove('trail'); delete root.dataset.rail; delete root.dataset.expanded;
  }};
}
