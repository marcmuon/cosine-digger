import {selectors, formatRoute, selectionId} from './state.js';
import {formatWhen} from './dom.js';

const HIDDEN_PARTS = [['owned', 'S-HDR-HIDDEN-OWNED'], ['served', 'S-HDR-HIDDEN-SERVED'], ['kept', 'S-HDR-HIDDEN-KEPT'],
  ['passed', 'S-HDR-HIDDEN-PASSED'], ['muted', 'S-HDR-HIDDEN-MUTED'], ['known', 'S-HDR-HIDDEN-KNOWN']];

/** "Hidden from this step: 4 you own, 1 you kept." or '' when nothing was left out. */
export function hiddenText(text, params = {}) {
  const parts = HIDDEN_PARTS.filter(([key]) => Number(params?.[key]) > 0).map(([key, id]) => text(id, {n: params[key]}));
  return parts.length ? text('S-HDR-HIDDEN', {parts: parts.join(', ')}) : '';
}

const terminal = (step) => ['done', 'failed', 'stopped'].includes(step.status);
// Steps that failed for want of a key cosine.club accepts: Add key or Fix key first, then Retry.
const KEY_ERRORS = new Set(['cosine_key_missing', 'cosine_key_rejected']);
const DISCOGS_ERRORS = new Set(['discogs_missing', 'discogs_rejected']);
const nameOf = (pick) => pick.artist ? `${pick.artist} - ${pick.title}` : pick.title;
const pointOf = (pick) => ({point_id: `track:${pick.track_key}`, kind: 'track', name: nameOf(pick),
  track_key: pick.track_key, artist: pick.artist, title: pick.title, cosine_id: pick.cosine_id,
  video_id: pick.video_id, source_step_id: pick.step_id, source_rank: pick.rank});

export function mountStepList(root, ctx) { return mountList(root, ctx, false); }
export function mountSlimList(root, ctx) { return mountList(root, ctx, true); }

function mountList(root, {store, h, text, clock}, slim) {
  const rows = new Map(), disposers = [];
  let stepId = null, noteSignature = null, bodySignature = null, linkSignature = null;
  let scrollSerial = null, lastView = null, countdownTimer = null, destroyed = false, focusSerial = null;
  const button = (action, label, props = {}) => h('button', {type: 'button',
    className: 'btn btn--small', 'data-action': action, ...props}, label);
  const heading = h('h2', {className: 'steplist-heading'});
  const number = h('span', {className: 'steplist-number', 'aria-hidden': 'true'});
  const sentence = h('span', {className: 'steplist-sentence'});
  const tag = h('span', {className: 'steplist-tag'});
  const meta = h('span', {className: 'steplist-meta'});
  const play = button('play-step', '▶ Play step');
  const rerun = button('rerun', 'Re-run');
  const later = h('span', {className: 'steplist-later'});
  const from = h('a', {className: 'steplist-from', 'data-action': 'from'});
  const notes = h('div', {className: 'steplist-notes'});
  const progressText = h('span');
  const progressFill = h('span', {className: 'steplist-progress-fill'});
  const progressBar = h('div', {className: 'steplist-progress-bar', role: 'progressbar',
    'aria-valuemin': 0}, progressFill);
  const progress = h('div', {className: 'steplist-progress'}, progressText, progressBar);
  const comparison = h('p', {className: 'steplist-comparison'});
  const header = slim ? h('header', {className: 'steplist-header'}, heading) :
    h('header', {className: 'steplist-header'},
      h('div', {className: 'steplist-headline'}, heading, tag),
      h('div', {className: 'steplist-metadata'}, meta,
        h('span', {className: 'steplist-header-actions'}, play, rerun), later),
      from, notes, progress, comparison);
  if (!slim) heading.append(number, sentence);
  const list = h('div', {className: 'steplist-rows', role: 'listbox', 'aria-label': 'Tracks'});
  const furtherLabel = h('span', {}, text('S-ROW-DIVIDER'));
  const furtherCount = h('span');
  const further = h('div', {className: 'steplist-divider', role: 'presentation'}, furtherLabel, furtherCount);
  const goneLabel = h('div', {className: 'steplist-divider steplist-gone-divider', role: 'presentation'});
  const body = h('div', {className: 'steplist-body'});
  root.replaceChildren(header, list, body);
  root.classList.add('steplist');
  function measureHeader() {
    if (!destroyed) root.style.setProperty('--steplist-header-height', `${header.getBoundingClientRect().height}px`);
  }
  const headerResize = new ResizeObserver(measureHeader);
  headerResize.observe(header);
  disposers.push(() => {
    headerResize.disconnect();
    root.style.removeProperty('--steplist-header-height');
  });

  function listen(event, handler) {
    root.addEventListener(event, handler);
    disposers.push(() => root.removeEventListener(event, handler));
  }
  function report(error) {
    if (!destroyed) store.actions.showToast('S-T-MOVE-ERR', {reason: error.message});
  }
  function run(result) { Promise.resolve(result).catch(report); }
  function rowAt(target) { return target.closest('.steplist-row'); }
  function openStep(s, id, sel = null, digId = null) {
    run(store.actions.openRoute({page: 'dig', digId: digId || s.steps.byId[id]?.dig_id || s.ui.route.digId,
      stepId: id, sel, view: s.ui.route.view}));
  }
  listen('click', (event) => {
    const s = store.getState(), step = selectors.viewedStep(s);
    if (!step) return;
    const control = event.target.closest('[data-action]'), row = rowAt(event.target);
    if (control) {
      event.stopPropagation();
      event.preventDefault();
      if (event.detail > 1 || control.disabled) return;
      const pick = row && s.picks.byId[row.dataset.pickId];
      switch (control.dataset.action) {
        case 'play': store.actions.playPick({pickId: pick.id}); break;
        case 'keep': case 'pass':
          run(store.actions.judge({trackKey: pick.track_key, stepId: pick.step_id,
            verdict: control.dataset.action, mode: 'toggle'})); break;
        case 'sounds': {
          const r = control.getBoundingClientRect();
          store.actions.openMoveMenu(pointOf(pick), {sourceStepId: step.id,
            anchor: {left: r.left, top: r.top, bottom: r.bottom}, openerId: control.id});
          break;
        }
        case 'play-step': store.actions.playStep(step.id); break;
        case 'rerun': run(store.actions.rerun(step.id)); break;
        case 'retry': run(store.actions.retry(step.id)); break;
        case 'from': openStep(s, step.parent_id, step.context?.source_selection_id ?? selectionId(step.context?.source_point)); break;
        case 'open-step': openStep(s, control.dataset.stepId, control.dataset.pointId || null, control.dataset.digId); break;
        case 'seed': {
          const omitted = control.dataset.seed;
          const none = step.note.some((note) => note.code === 'S-N-NONE');
          run(store.actions.openNewDig({chips: step.origins.filter((origin) =>
            omitted ? origin.name !== omitted : !none || origin.resolution !== 'missing')}));
          break;
        }
        case 'further':
          run(store.actions.startMove({parentStepId: step.parent_id, origins: step.origins,
            move: step.move, further: true, scope: step.context?.these_scope,
            requested_count: step.context?.requested_count})); break;
        case 'alternative':
          run(store.actions.chooseMove(control.dataset.move, {point: emptySource(step, s)})); break;
        case 'setup': run(store.actions.openRoute({page: 'setup'})); break;
        case 'add-key': case 'fix-key': run(store.actions.openConnect()); break;
      }
      return;
    }
    if (row) {
      const pick = s.picks.byId[row.dataset.pickId];
      store.actions.selectPoint(step.id, pointOf(pick), {scroll: false});
      row.focus({preventScroll: true});
    }
  });
  listen('dblclick', (event) => {
    event.stopPropagation();
    if (event.target.closest('button, a')) { event.preventDefault(); return; }
    const row = rowAt(event.target);
    if (row) store.actions.playPick({pickId: row.dataset.pickId});
  });
  listen('pointerover', (event) => {
    const row = rowAt(event.target);
    if (!row || row.contains(event.relatedTarget)) return;
    const s = store.getState(), pick = s.picks.byId[row.dataset.pickId];
    store.actions.hoverPoint(s.ui.route.stepId, pointOf(pick));
  });
  listen('pointerout', (event) => {
    const row = rowAt(event.target);
    if (row && !row.contains(event.relatedTarget) && store.getState().ui.hover?.point?.point_id === row.dataset.pointId)
      store.actions.hoverPoint(stepId, null);
  });
  listen('focusin', () => {
    if (store.getState().ui.focusRegion !== 'list') store.actions.focusRegion('list');
  });
  listen('scroll', () => {
    const s = store.getState();
    if (stepId && s.ui.route.page === 'dig' && s.ui.route.view === (slim ? 'map' : 'list') &&
        s.ui.scrollByStep[stepId] !== root.scrollTop) store.actions.saveScroll(stepId, root.scrollTop);
  });

  function createRow(pick) {
    const rank = h('span', {className: 'steplist-rank', 'aria-hidden': 'true'});
    const fill = h('span', {className: 'steplist-nearness-fill'});
    const bar = h('span', {className: 'steplist-nearness', 'aria-hidden': 'true'}, fill);
    const title = h('span', {className: 'steplist-title'}, nameOf(pick));
    const why = h('span', {className: 'steplist-why'});
    const marks = h('span', {className: 'steplist-marks'});
    const actions = h('span', {className: 'steplist-actions'},
      button('play', slim ? '▶' : text('S-ROW-PLAY'), {tabindex: -1, 'aria-label': slim ? 'Play' : text('S-ROW-PLAY'),
        title: text('S-ROW-PLAY-TIP')}),
      button('keep', slim ? '★' : text('S-ROW-KEEP'), {tabindex: -1, 'aria-label': slim ? 'Keep' : text('S-ROW-KEEP'), 'aria-pressed': false}),
      button('pass', slim ? '✕' : text('S-ROW-PASS'), {tabindex: -1, 'aria-label': slim ? 'Pass' : text('S-ROW-PASS'), 'aria-pressed': false}));
    if (!slim) actions.append(button('sounds', text('S-ROW-SOUNDS'), {tabindex: -1,
      id: `steplist-sounds-${pick.id}`, title: text('S-ROW-DIG-TIP')}));
    const node = h('div', {className: 'steplist-row', role: 'option', tabindex: -1,
      'data-pick-id': pick.id, 'data-rank': pick.rank, 'data-step-id': pick.step_id,
      'data-point-id': `track:${pick.track_key}`}, rank, slim ? null : bar,
      h('span', {className: 'steplist-track'}, title, slim ? null : why),
      h('span', {className: 'steplist-row-end'}, marks, actions));
    return {node, rank, fill, title, why, marks, actions, markSignature: null};
  }
  function stepReference(s, id, marks) {
    const step = s.steps.byId[id], appearance = marks.also_in.find((item) => item.step_id === id);
    const digId = step?.dig_id || appearance?.dig_id;
    return {number: step?.number || appearance?.number, dig_id: digId,
      dig_name: s.digs.byId[digId]?.name || appearance?.dig_name};
  }
  function rowMarks(s, pick, marks) {
    const entries = [], add = (id, params = {}, props = {}) => entries.push({label: text(id, params), ...props});
    const digId = s.steps.byId[pick.step_id]?.dig_id;
    const provenanceLabel = (id, source) => {
      const label = text(id, {n: source.number});
      return source.dig_id !== digId && source.dig_name ?
        label.replace('in step ', () => `in ${source.dig_name}, step `) : label;
    };
    if (marks.refused) add(marks.unavailable ? 'S-ROW-UNAVAIL' : 'S-ROW-REFUSED');
    else if (marks.noVideo) add('S-ROW-NOVIDEO');
    if (marks.kept || marks.passed) {
      const prefix = marks.kept ? 'S-ROW-KEPT' : 'S-ROW-PASSED';
      const source = stepReference(s, marks.judgment.step_id, marks);
      const id = marks.judgment.source === 'legacy' || !source.number ? prefix + '-PREVIOUSLY' :
        marks.judgment.step_id === pick.step_id ? prefix : prefix + '-IN';
      entries.push({label: provenanceLabel(id, source),
        className: marks.kept ? 'steplist-mark-kept' : 'steplist-mark-passed'});
    }
    if (marks.new) add('S-ROW-NEW', {}, {className: 'steplist-mark-new'});
    if (marks.heard) {
      const heardStep = marks.heard.first_step_id;
      const source = stepReference(s, heardStep, marks);
      entries.push({label: provenanceLabel(heardStep === pick.step_id || !source.number ? 'S-ROW-HEARD' : 'S-ROW-HEARD-IN', source)});
    }
    if (marks.also_in.length) {
      const [first, ...rest] = marks.also_in;
      if (marks.also_in.every((item) => item.dig_id === digId))
        add(rest.length ? 'S-ROW-ALSO-MANY' : 'S-ROW-ALSO', {n: first.number, m: rest.map((item) => item.number).join(', ')});
      else entries.push({label: marks.also_in.map((item) => provenanceLabel('S-ROW-ALSO', item)).join('; ')});
    }
    const label = selectors.viewedStep(s).move === 'label' && selectors.viewedStep(s).origins[0];
    if (label?.discogs_id && pick.facts?.labels?.some((item) => item.id === label.discogs_id))
      add('S-ROW-ALSOON', {Label: label.name}, {className: 'steplist-mark-label'});
    return entries;
  }
  function updateRow(record, pick, s, gone, tabId) {
    const {node, rank, fill, title, why, marks: marksNode, actions} = record;
    const marks = selectors.marks(s, pick.id), selected = s.ui.route.sel === node.dataset.pointId;
    const current = s.player.current?.pickId === pick.id && s.player.state !== 'idle';
    node.setAttribute('aria-selected', String(selected));
    node.setAttribute('tabindex', pick.id === tabId ? '0' : '-1');
    node.classList.toggle('steplist-row-gone', gone);
    node.classList.toggle('steplist-row-passed', marks.passed);
    node.classList.toggle('steplist-row-heard', !!marks.heard);
    node.classList.toggle('steplist-row-hover', s.ui.hover?.stepId === stepId && s.ui.hover.point?.point_id === node.dataset.pointId);
    const withWhy = !slim && ((selectors.viewedStep(s).origins.length > 1) || pick.batch === 1);
    node.classList.toggle('steplist-row-with-why', withWhy);
    node.dataset.playerState = current ? s.player.state : '';
    rank.textContent = marks.refused ? '!' : current ?
      ({playing: '▶', paused: '❚❚', loading: '~', refused: '!', waiting: '~'}[s.player.state] || pick.rank) : pick.rank;
    const score = Number.isFinite(pick.score) ? pick.score.toFixed(2) : '—';
    node.title = text('S-ROW-TITLE', {'Artist - Title': nameOf(pick), '0.79': score});
    title.title = nameOf(pick);
    fill.style.width = `${Math.max(0, Math.min(1, pick.score || 0)) * 40}px`;
    why.textContent = pick.route.why || '';
    why.title = why.textContent;
    why.hidden = !withWhy;
    const entries = rowMarks(s, pick, marks), signature = JSON.stringify(entries);
    if (signature !== record.markSignature) {
      record.markSignature = signature;
      marksNode.replaceChildren(...entries.slice(0, slim ? 1 : 3).flatMap((entry, i) => {
        const node = h('span', {className: entry.className}, entry.label);
        return i ? [' · ', node] : [node];
      }));
      const overflow = entries.length - (slim ? 1 : 3);
      if (overflow > 0 && !slim) marksNode.append(` · +${overflow}`);
      marksNode.title = entries.map((entry) => entry.label).join(' · ');
    }
    node.setAttribute('aria-label', `${pick.rank}. ${nameOf(pick)}${entries.length ? ' · ' + entries.map((entry) => entry.label).join(' · ') : ''}`);
    for (const action of ['keep', 'pass']) {
      const control = actions.querySelector(`[data-action="${action}"]`);
      const on = action === 'keep' ? marks.kept : marks.passed;
      const label = text(action === 'keep' ? on ? 'S-ROW-KEEP-ON' : 'S-ROW-KEEP' : on ? 'S-ROW-PASS-ON' : 'S-ROW-PASS');
      if (!slim) control.textContent = label;
      control.setAttribute('aria-label', slim ? action === 'keep' ? 'Keep' : 'Pass' : label);
      control.setAttribute('aria-pressed', String(on));
      control.disabled = s.ui.server !== 'up' || !!s.judgments.pending[pick.track_key];
      control.title = s.ui.server !== 'up' ? text('S-SRV-LOST') :
        text(action === 'keep' ? on ? 'S-ROW-KEEP-ON-TIP' : 'S-ROW-KEEP-TIP' : on ? 'S-ROW-PASS-ON-TIP' : 'S-ROW-PASS-TIP');
    }
    const sounds = actions.querySelector('[data-action="sounds"]');
    if (sounds) sounds.disabled = s.ui.server !== 'up';
  }

  function originLine(step, s) {
    const parent = s.steps.byId[step.parent_id], source = step.context?.source_point;
    if (!parent || !source) return '';
    if (step.context?.source_selection_id?.startsWith('origin:'))
      return text('S-HDR-FROM-ORIGIN', {p: parent.number, name: source.name});
    if (step.move === 'these') {
      const names = step.origins.map((point) => point.name);
      return `from step ${parent.number}: ${names.length} ${names.length === 1 ? 'keep' : 'keeps'}: ${names.slice(0, 3).join(', ')}${
        names.length > 3 ? ` (and ${names.length - 3} more)` : ''}`;
    }
    const sourcePick = s.picks.byId[`${source.source_step_id}:${source.source_rank}`];
    const params = {p: parent.number, 'Artist - Title': sourcePick ? nameOf(sourcePick) : source.name};
    if (['label', 'person'].includes(step.move) && source.source_rank == null) {
      params.k = parent.hubs?.find((hub) => hub.hub_key === source.point_id)?.pick_ranks.length || 0;
      return text(`S-HDR-FROM-HUB-${step.move.toUpperCase()}`, params);
    }
    if (step.move === 'person') params.role = sourcePick?.facts?.credits?.find((credit) =>
      credit.id === source.discogs_id || credit.name === source.name)?.roles.join(', ') || 'credited';
    return text(`S-HDR-FROM-${{seed: 'TRACK', track: 'TRACK', label: 'LABEL', artist: 'ARTIST', person: 'PERSON'}[step.move]}`, params);
  }
  function retrySeconds(step) {
    if (!step.error || step.error.code === 'server_restarted') return 0;
    if (step.error.retry_after_s == null && step.error.retry_at == null) return 0;
    const deadline = step.error.retry_at ? Date.parse(step.error.retry_at) :
      Date.parse(step.finished_at) + (step.error.retry_after_s ?? 30) * 1000;
    return Number.isFinite(deadline) ? Math.max(0, Math.ceil((deadline - Date.now()) / 1000)) : 0;
  }
  function renderHeader(step, picks, s, complete, remaining) {
    if (slim) { heading.textContent = text('S-TERR-LIST-H', {n: step.number, c: picks.length}); return; }
    number.textContent = step.number;
    sentence.textContent = step.header;
    heading.setAttribute('aria-label', `${step.number}. ${step.header}`);
    const waiting = s.steps.jobs[step.current_job_id]?.progress?.phase === 'waiting';
    const tagId = step.status === 'failed' ? 'S-HDR-TAG-FAILED' : step.status === 'stopped' ? 'S-HDR-TAG-STOPPED' :
      step.rerun_of ? 'S-HDR-TAG-CHANGED' : !complete ? waiting ? 'S-HDR-TAG-WAITING' : 'S-HDR-TAG-RUNNING' : null;
    tag.textContent = tagId ? text(tagId) : '';
    tag.dataset.status = tagId === 'S-HDR-TAG-CHANGED' ? 'changed' : step.status;
    meta.textContent = text(s.ui.openReason === 'return' ? 'S-HDR-META-RETURN' : 'S-HDR-META', {
      'HH:MM': formatWhen(step.started_at), c: picks.length, k: selectors.keepsForStep(s, step.id).length,
      p: picks.filter((pick) => selectors.marks(s, pick.id).passed).length});
    play.disabled = !picks.length;
    rerun.hidden = !terminal(step) || ['failed', 'stopped'].includes(step.status) && !picks.length ||
      KEY_ERRORS.has(step.error?.code) && !selectors.cosineKey(s).usable;
    rerun.textContent = remaining ? text('S-E-RERUN-WAIT', {s: remaining}) : 'Re-run';
    rerun.disabled = s.ui.server !== 'up' || !!remaining;
    from.textContent = originLine(step, s);
    from.hidden = !from.textContent;
    if (step.parent_id) from.href = formatRoute({page: 'dig', digId: step.dig_id, stepId: step.parent_id,
      sel: step.context?.source_selection_id ?? selectionId(step.context?.source_point), view: s.ui.route.view});
    const links = step.rerun_steps || [];
    const signature = JSON.stringify([links, s.ui.route.view]);
    if (signature !== linkSignature) {
      linkSignature = signature;
      later.replaceChildren(...links.map((item) => h('a', {'data-action': 'open-step', 'data-step-id': item.step_id,
        href: formatRoute({page: 'dig', digId: step.dig_id, stepId: item.step_id, view: s.ui.route.view})},
      text('S-HDR-RERUN-LATER', {m: item.number}))));
    }
    const factsNotes = {discogs_budget_exhausted: 'S-N-FACTS-BUDGET',
      discogs_unavailable: 'S-N-FACTS-DOWN', facts_interrupted: 'S-N-FACTS-STOPPED'};
    const seenFactsNotes = new Set();
    const warningNotes = (step.note || []).filter((note) => {
      if (Object.hasOwn(factsNotes, note.code)) {
        if (seenFactsNotes.has(note.code)) return false;
        seenFactsNotes.add(note.code);
        return true;
      }
      return note.code?.startsWith('S-N-') || note.message && !note.code?.startsWith('S-') && !note.progress;
    });
    // Informational, after any warnings: which tracks the step left out, and why.
    const hiddenNote = (step.note || []).findLast((note) => note.code === 'S-HDR-HIDDEN');
    const warnings = JSON.stringify([warningNotes, hiddenNote?.params]);
    if (warnings !== noteSignature) {
      noteSignature = warnings;
      const hiddenLine = hiddenText(text, hiddenNote?.params);
      notes.replaceChildren(...warningNotes.map((note) => {
        let message;
        const params = {...note.params};
        if (note.code?.startsWith('S-N-SWAP') && params.n > 3)
          params.titles = `${[params.t1, params.t2, params.t3].join(', ')} and ${params.n - 3} more`;
        if (Object.hasOwn(factsNotes, note.code)) message = text(factsNotes[note.code]);
        else try { message = text(note.code, params) || note.message; } catch { message = note.message; }
        const line = h('p', {className: 'steplist-note'}, '! ', message);
        if (['S-N-SWAP-FILE', 'S-N-NONE'].includes(note.code)) line.append(' ',
          button('seed', text('S-E-EMPTY-BTN-SEED'), {'data-seed': note.params?.seed}));
        return line;
      }), ...(hiddenLine ? [h('p', {className: 'steplist-hidden'}, hiddenLine)] : []));
    }
    progress.hidden = complete;
    const nearTarget = step.batches.find((batch) => batch.hop === 0)?.target_count || 25;
    const inFurther = step.further && (step.batches.some((batch) => batch.hop === 1) || picks.length >= nearTarget);
    const resolving = !complete && ['label', 'artist', 'person'].includes(step.move) &&
      !step.note.some((note) => note.code === 'S-HDR-PROGRESS');
    progressText.textContent = text(waiting ? 'S-HDR-WAITING' : resolving ? 'S-HDR-RESOLVING' : 'S-HDR-PROGRESS',
      {name: step.origins[0].name, i: picks.length, n: nearTarget, further: inFurther});
    const target = step.further ? nearTarget + 25 : nearTarget;
    const received = resolving ? 0 : picks.length;
    progressBar.setAttribute('aria-label', progressText.textContent);
    progressBar.setAttribute('aria-valuemax', String(target));
    progressBar.setAttribute('aria-valuenow', String(received));
    progressFill.style.width = `${Math.min(100, received / target * 100)}%`;
    comparison.hidden = !step.rerun_of;
    if (step.rerun_of) {
      const original = s.steps.byId[step.rerun_of], diff = step.diff;
      comparison.textContent = text(terminal(step) ? 'S-HDR-RERUN' : 'S-HDR-RERUN-RUN', {
        n: original?.number || '', 'HH:MM': formatWhen(original?.started_at),
        a: diff?.same ?? picks.length - (diff?.new_track_keys.length || 0),
        b: diff?.new ?? diff?.new_track_keys.length ?? 0, c: diff?.gone ?? diff?.gone_picks.length ?? 0});
    }
  }
  function emptySource(step, s) {
    const source = step.context?.source_point;
    const pick = source && s.picks.byId[`${source.source_step_id}:${source.source_rank}`];
    return pick ? pointOf(pick) : source;
  }
  function renderBody(step, picks, s, remaining) {
    const key = selectors.cosineKey(s);
    const signature = JSON.stringify([step.status, step.error, picks.length, remaining, s.ui.server, step.note,
      s.ui.settings?.discogs_token, key.state]);
    if (signature === bodySignature || slim) return;
    bodySignature = signature;
    body.replaceChildren();
    if (step.status === 'failed' || step.status === 'stopped') {
      const failed = step.status === 'failed', code = failed ? step.error?.code : null, n = step.further ? 50 : 25;
      // A key refused after some picks were saved: the step stopped early, and Re-run (not Retry) runs it again.
      const line = !failed ? text('S-E-STOPPED', {i: picks.length, n}) : code === 'cosine_key_rejected' && picks.length ?
        '! ' + text('S-E-KEY-PARTIAL', {i: picks.length, n}) : '! ' + step.error.message;
      body.append(h('p', {className: failed ? 'steplist-error' : ''}, line));
      if (failed) body.append(h('p', {}, text('S-E-FAIL-L2')));
      if (KEY_ERRORS.has(code) && !key.usable) {
        body.append(button(key.set ? 'fix-key' : 'add-key', text(key.set ? 'S-KEY-FIX' : 'S-KEY-ADD'), {disabled: s.ui.server !== 'up'}));
        return;
      }
      if (DISCOGS_ERRORS.has(code)) body.append(button('setup', text('S-E-CONNECT-DISCOGS')), ' ');
      const retry = !picks.length;
      body.append(button(retry ? 'retry' : 'rerun', remaining ? text(retry ? 'S-E-RETRY-WAIT' : 'S-E-RERUN-WAIT', {s: remaining}) :
        retry ? text('S-E-RETRY') : 'Re-run', {disabled: !!remaining || s.ui.server !== 'up'}));
      return;
    }
    if (step.status !== 'done' || picks.length || step.note.some((note) => note.code === 'S-N-NONE')) return;
    const source = emptySource(step, s);
    const choices = source ? selectors.moveChoices(s, source, {sourceStepId: source.source_step_id || step.id})
      .filter((choice) => !choice.disabled_reason && choice.move !== step.move) : [];
    const alternatives = [];
    if (!step.further && source) alternatives.push({action: 'further', label: text('S-E-EMPTY-BTN-FURTHER')});
    for (const move of ['artist', 'track', 'label', 'person']) {
      if (alternatives.length === 2) break;
      if (choices.some((choice) => choice.move === move)) alternatives.push({action: 'alternative', move,
        label: text(`S-E-EMPTY-BTN-${move.toUpperCase()}`)});
    }
    if (!source && !step.parent_id) alternatives.push({action: 'seed', label: text('S-E-EMPTY-BTN-SEED')});
    if (alternatives.length === 2) body.append(h('p', {}, text('S-E-EMPTY', {
      'alt 1': alternatives[0].label.toLowerCase(), 'alt 2': alternatives[1].label.toLowerCase()})));
    else body.append(h('p', {}, 'Nothing came back for this move.'));
    for (const option of alternatives) body.append(button(option.action, option.label,
      {'data-move': option.move, disabled: option.action !== 'seed' && s.ui.server !== 'up'}));
  }
  function render(s) {
    const step = selectors.viewedStep(s);
    if (!step) return;
    const changed = stepId !== step.id;
    const viewChanged = lastView !== s.ui.route.view;
    lastView = s.ui.route.view;
    if (changed) {
      stepId = step.id; rows.clear(); list.replaceChildren();
      noteSignature = bodySignature = linkSignature = null;
    }
    const picks = selectors.orderedPicks(s, step.id), complete = selectors.isPicksComplete(s, step.id);
    const gone = terminal(step) ? step.diff?.gone_picks || [] : [];
    const tabId = [...picks, ...gone].find((pick) => `track:${pick.track_key}` === s.ui.route.sel)?.id || picks[0]?.id || gone[0]?.id;
    const desired = [];
    const furtherBatch = !slim && step.batches.find((batch) => batch.hop === 1);
    let divided = false;
    for (const pick of [...picks, ...gone]) {
      if (!divided && furtherBatch && (pick.rank >= furtherBatch.start_rank || gone[0]?.id === pick.id)) {
        desired.push(further); divided = true;
      }
      if (gone[0]?.id === pick.id) desired.push(goneLabel);
      if (!rows.has(pick.id)) rows.set(pick.id, createRow(pick));
      const record = rows.get(pick.id);
      updateRow(record, pick, s, gone.includes(pick), tabId);
      desired.push(record.node);
    }
    if (furtherBatch && !divided) desired.push(further);
    // Keep each existing node in place: live overlays and streaming never replace a row.
    desired.forEach((node, i) => { if (list.children[i] !== node) list.insertBefore(node, list.children[i] || null); });
    const retained = new Set(desired);
    for (const node of [...list.children]) if (!retained.has(node)) node.remove();
    furtherCount.textContent = !complete ? text('S-ROW-DIVIDER-RUN', {i: picks.filter((pick) => pick.batch === 1).length}) : '';
    goneLabel.textContent = text('S-ROW-GONE-DIVIDER', {c: gone.length, n: s.steps.byId[step.rerun_of]?.number || ''});
    const remaining = retrySeconds(step);
    renderHeader(step, picks, s, complete, remaining);
    renderBody(step, picks, s, remaining);
    // Back from the key screen: Retry (or Re-run) is ready under the cursor.
    const focus = s.ui.connect?.focus;
    if (!slim && focus?.stepId === step.id && focus.serial !== focusSerial) {
      focusSerial = focus.serial;
      body.querySelector('[data-action="retry"], [data-action="rerun"]')?.focus();
    }
    measureHeader();
    clock.clearTimeout(countdownTimer);
    if (remaining && !slim) countdownTimer = clock.setTimeout(() => render(store.getState()), 1000);
    const visible = s.ui.route.page === 'dig' && s.ui.route.view === (slim ? 'map' : 'list');
    if ((changed || viewChanged) && visible) root.scrollTop = s.ui.scrollByStep[step.id] || 0;
    const target = s.ui.scrollTarget;
    if (target && target.serial !== scrollSerial) {
      scrollSerial = target.serial;
      // Hidden mounts consume the request too; a later view switch restores scroll.
      const row = visible && target.stepId === step.id && rows.get(`${step.id}:${target.rank}`)?.node;
      if (row) row.scrollIntoView({block: 'nearest', behavior: s.ui.reducedMotion || matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'});
    }
    if (visible && document.activeElement?.matches('.steplist-row') && root.contains(document.activeElement))
      rows.get(tabId)?.node.focus({preventScroll: true});
  }
  const unsubscribe = store.subscribe(render);
  render(store.getState());
  return {destroy() {
    if (destroyed) return;
    destroyed = true; unsubscribe(); clock.clearTimeout(countdownTimer);
    disposers.forEach((dispose) => dispose()); root.replaceChildren(); root.classList.remove('steplist');
  }};
}
