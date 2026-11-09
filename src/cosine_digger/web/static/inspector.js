import {selectors, formatRoute, selectionId} from './state.js';

const nameOf = pick => pick.artist ? `${pick.artist} - ${pick.title}` : pick.title;
const normalizedName = name => name.trim().toLowerCase().replace(/\s*\(\d+\)\s*$/, '').replace(/\s+/g, ' ').trim();
const entityId = (kind, entity) => entity.id ? `${kind}:${entity.id}` : `${kind}:name:${normalizedName(entity.name)}`;
const pointOf = pick => ({point_id: `track:${pick.track_key}`, kind: 'track', name: nameOf(pick),
  track_key: pick.track_key, artist: pick.artist, title: pick.title, cosine_id: pick.cosine_id,
  video_id: pick.video_id, source_step_id: pick.step_id, source_rank: pick.rank});
const scoreText = value => Number.isFinite(value) ? value.toFixed(2) : 'similarity unavailable';

/** One mount, shared by both views. Relationships are rendered from saved facts and routes. */
export function mountInspector(root, {store, h, text, safeUrl}) {
  let signature = null, selection = null, destroyed = false;
  const heading = h('h2', {id: 'inspector-heading', className: 'panel__title'});
  const body = h('div', {className: 'inspector-body'});
  root.classList.add('inspector');
  root.replaceChildren(h('header', {className: 'panel__bar'}, heading), body);

  function run(result) {
    Promise.resolve(result).catch(error => {
      if (!destroyed) store.actions.showToast('S-T-MOVE-ERR', {reason: error.message});
    });
  }
  function activate(handler) {
    return event => {
      if (event.currentTarget.tagName === 'A' && (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey)) return;
      event.preventDefault(); event.stopPropagation();
      if (event.detail > 1 || event.currentTarget.disabled || event.currentTarget.getAttribute('aria-disabled') === 'true') return;
      run(handler(event));
    };
  }
  function button(action, label, handler, props = {}) {
    return h('button', {type: 'button', className: 'btn btn--small', 'data-action': action,
      id: `inspector-${action}`, onclick: activate(handler), ...props}, label);
  }
  function title(label, thumb = null) {
    const block = h('div', {className: 'inspector-title-block'},
      h('h3', {id: 'inspector-title', tabindex: '-1', className: 'inspector-title'}, label));
    const url = providerUrl(thumb, ['i.discogs.com', 'img.discogs.com']);
    if (url) block.prepend(h('img', {className: 'inspector-thumb', width: 56, height: 56,
      alt: 'Discogs release artwork', src: url, onerror: event => { event.currentTarget.hidden = true; }}));
    return block;
  }
  function providerUrl(value, hosts) {
    const safe = safeUrl(value);
    if (!safe) return null;
    const url = new URL(safe);
    return hosts.includes(url.hostname) && ['', '80', '443'].includes(url.port) ? safe : null;
  }
  function section(id, label, ...children) {
    return h('section', {className: 'inspector-section', 'data-section': id, 'aria-labelledby': `inspector-${id}-heading`},
      h('h3', {id: `inspector-${id}-heading`, className: 'field__label'}, label), children);
  }
  function openLink(s, stepId, digId, sel, label, id) {
    const route = {page: 'dig', digId, stepId, sel, view: s.ui.route.view};
    return h('a', {id: `inspector-link-${id}`, className: 'inspector-link', href: formatRoute(route),
      'data-action': 'open-step', 'data-step-id': stepId, 'data-point-id': sel,
      onclick: activate(() => store.actions.openRoute({...route, view: store.getState().ui.route.view}))}, label);
  }
  function selectLink(s, step, point, label, id) {
    const sel = selectionId(point);
    return h('a', {id: `inspector-select-${id}`, className: 'inspector-link',
      href: formatRoute({page: 'dig', digId: step.dig_id, stepId: step.id, sel, view: s.ui.route.view}),
      'data-action': 'select', 'data-point-id': point.point_id, 'data-rank': point.source_rank,
      onclick: activate(() => store.actions.selectPoint(step.id, point))}, label);
  }
  function moveControl(s, point, move, label, further = false, id = `${move}-${point.point_id}-${point.source_rank}-${further}`) {
    const choice = selectors.moveChoices(s, point, {sourceStepId: point.source_step_id, further}).find(item => item.move === move);
    const reason = s.ui.server !== 'up' ? 'S-SRV-LOST' : choice?.disabled_reason || (!choice ? 'S-MENU-NONE' : null);
    const reasonId = `inspector-reason-${id}`;
    const target = {artist: 'S-INS-DIG-T-A', label: 'S-INS-DIG-T-L', person: 'S-INS-DIG-T-C'}[move];
    const tip = text(further ? 'S-INS-DIG-FAR-TIP' : 'S-INS-DIG-TIP',
      {target: target ? text(target, {name: point.name}) : point.name});
    return h('span', {className: 'inspector-move'}, button(`move-${id}`, label,
      event => store.actions.chooseMove(move, {point, further: further || event.shiftKey}), {
        disabled: !!reason, title: reason ? text(reason) : tip,
        'aria-label': `${label.replace(/ ▸$/, '')} ${point.name}`,
        'aria-describedby': reason ? reasonId : null, 'data-move': move,
        'data-further': String(further), 'data-point-id': point.point_id,
        'data-step-id': point.source_step_id, 'data-rank': point.source_rank}),
    reason && h('span', {id: reasonId, className: 'inspector-muted'}, text(reason)));
  }
  function progress(s, stepId) {
    const picks = selectors.orderedPicks(s, stepId);
    return {i: picks.filter(pick => pick.facts_state !== 'loading').length, n: picks.length,
      loading: picks.some(pick => pick.facts_state === 'loading')};
  }
  function why(pick, step, picks) {
    const route = pick.route, origins = step.origins || [];
    const names = Object.fromEntries(origins.map(point => [point.point_id, point.name]));
    const close = route.close_seed_ids || [], scores = route.seed_scores || {};
    const params = {r: pick.rank, c: picks.length || step.counts?.picks || pick.rank,
      '0.79': scoreText(pick.score), n: route.seed_count ?? origins.length, k: close.length,
      seed: names[route.nearest_seed || route.layout_seed] || origins[0]?.name || '',
      scores: close.map(id => `${names[id] || id} ${scoreText(scores[id])}`).join(', ')};
    let wording;
    if (route.hop === 1) {
      // The server describes the branch; via is not a release fact or a seed measurement.
      const subject = (route.why || '').replace(/^one step further: sounds like /i, '');
      wording = text('S-INS-WHY-FURTHER', {subject, k: route.parent_pick_ranks?.length || 0});
    } else if (!close.length) wording = text(route.nearest_seed == null ? 'S-INS-WHY-UNMEASURED' : 'S-INS-WHY-NEAREST', params);
    else if (origins.length === 1) wording = text('S-INS-WHY-1', {...params, Sentence: step.header.replace(/[.]$/, '')});
    else if (close.length === 1) wording = text('S-INS-WHY-SEED', {seed: names[close[0]] || close[0], '0.81': scoreText(scores[close[0]])});
    else wording = text(close.length === params.n ? 'S-INS-WHY-ALL' : 'S-INS-WHY-N', params);
    const result = section('why', text('S-INS-WHY-H'), h('p', {}, wording));
    const scoreId = route.hop === 1 ? 'S-INS-SCORE-FURTHER' : 'S-INS-SCORE';
    result.append(h('p', {className: 'inspector-muted inspector-nearness'}, text(scoreId, {'0.79': scoreText(pick.score)})));
    if (route.hop === 1 || (origins.length > 1 && !origins.every(origin => close.includes(origin.point_id)))) {
      const list = h('dl', {className: 'inspector-scores'});
      for (const origin of origins) list.append(h('dt', {}, origin.name), h('dd', {}, scoreText(scores[origin.point_id])));
      result.append(h('h4', {className: 'inspector-muted'},
        text(route.hop === 1 ? 'S-INS-SIMS-FURTHER-H' : 'S-INS-SIMS-H')), list);
    }
    if (route.hop === 1 && Object.keys(route.branch_seed_scores || {}).length) {
      const branchScores = h('dl', {className: 'inspector-scores'});
      for (const [id, score] of Object.entries(route.branch_seed_scores))
        branchScores.append(h('dt', {}, route.branch?.name || id), h('dd', {}, scoreText(score)));
      result.append(h('h4', {className: 'inspector-muted'}, text('S-INS-SIMS-BRANCH-H')), branchScores);
    }
    return result;
  }
  function alsoIn(s, pick, marks) {
    if (!marks.also_in.length && !marks.heard) return null;
    const rows = marks.also_in.map(item => openLink(s, item.step_id, item.dig_id, selectionId(pointOf(pick)),
      (item.dig_id !== s.ui.route.digId ? `${item.dig_name} · ` : '') +
      text('S-INS-ALSO-ROW', {n: item.number, sentence: item.header, r: item.rank}), item.step_id));
    if (marks.heard) {
      const source = s.steps.byId[marks.heard.first_step_id];
      const date = new Date(marks.heard.at);
      const time = Number.isFinite(date.getTime()) ? date.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', hour12: false}) : '';
      rows.push(h('p', {className: 'inspector-muted'}, source ? text('S-INS-HEARD', {n: source.number, 'HH:MM': time}) : `Heard earlier at ${time}`));
    }
    return section('also-in', text('S-INS-ALSO-H'), rows);
  }
  function facts(s, pick, sourceStep, viewedStep) {
    const result = section('facts', text('S-INS-FACTS-H'), h('p', {className: 'inspector-muted inspector-facts-source'}, text('S-INS-FACTS-ASIDE')));
    result.dataset.factsState = pick.facts_state;
    if (pick.facts_state !== 'ready' || pick.facts?.source !== 'discogs') {
      const codes = new Set((sourceStep.note || []).map(note => note.code));
      const unavailable = codes.has('discogs_unavailable') ? 'S-INS-FACTS-DOWN' :
        codes.has('discogs_budget_exhausted') ? 'S-INS-FACTS-BUDGET' :
        codes.has('facts_interrupted') ? 'S-INS-FACTS-STOPPED' : 'S-INS-FACTS-DOWN';
      const id = {loading: 'S-INS-FACTS-LOADING', missing: 'S-INS-FACTS-MISSING',
        unavailable, no_token: 'S-INS-FACTS-NOTOKEN'}[pick.facts_state] || 'S-INS-FACTS-MISSING';
      result.append(h('p', {className: id === 'S-INS-FACTS-DOWN' ? 'inspector-warning' : 'inspector-muted'}, text(id, progress(s, pick.step_id))));
      return result;
    }
    const data = pick.facts;
    for (const origin of sourceStep.origins.filter(point => point.kind === 'label')) {
      if (origin.discogs_id && data.labels.some(label => String(label.id) === String(origin.discogs_id)))
        result.append(h('p', {className: 'inspector-also-on'}, text('S-INS-ALSO-ON', {Label: origin.name})));
    }
    const list = h('dl', {className: 'inspector-facts'});
    const source = pointOf(pick);
    const choices = selectors.moveChoices(s, source, {sourceStepId: pick.step_id});
    function factRow(kind, entity, label, index) {
      const id = entityId(kind, entity);
      const choice = choices.find(item => item.move === kind);
      const point = choice?.picker_points.find(item => item.point_id === id) || choice?.point;
      const display = kind === 'person' ? `${entity.name} (${entity.roles.join(', ')})` : entity.name;
      const row = h('dd', {'data-fact-kind': kind, 'data-point-id': id},
        point && ['label', 'person'].includes(kind) ? pick.step_id === viewedStep.id ?
          selectLink(s, viewedStep, point, display, `${kind}-${index}`) :
          openLink(s, sourceStep.id, sourceStep.dig_id, selectionId(point), display, `fact-${kind}-${index}`) : h('span', {}, display));
      if (kind === 'person') row.append(h('span', {className: 'inspector-muted inspector-credit-scope'}, ` · ${entity.scope} credit`));
      if (point) row.append(moveControl(s, point, kind, text('S-INS-SOUNDS'), false, `fact-${kind}-${index}`));
      return [h('dt', {}, label), row];
    }
    data.artists.forEach((entity, i) => list.append(...factRow('artist', entity, 'Artist', i)));
    data.labels.forEach((entity, i) => list.append(...factRow('label', entity, 'Label', i)));
    list.append(h('dt', {}, 'Year'), h('dd', {'data-fact-kind': 'year'}, data.year ?? '—'));
    data.credits.slice(0, 6).forEach((entity, i) => list.append(...factRow('person', entity, 'Credits', i)));
    result.append(list);
    if (data.credits.length > 6) {
      const more = h('dl', {className: 'inspector-facts'});
      data.credits.slice(6).forEach((entity, i) => more.append(...factRow('person', entity, 'Credits', i + 6)));
      result.append(h('details', {id: 'inspector-credits'}, h('summary', {}, text('S-INS-CREDITS-MORE', {k: data.credits.length - 6})), more));
    }
    result.append(h('dl', {className: 'inspector-facts'}, h('dt', {}, 'Style'), h('dd', {'data-fact-kind': 'style'}, data.styles.join(', ') || '—')));
    const url = providerUrl(data.release_url, ['discogs.com', 'www.discogs.com', 'api.discogs.com']);
    if (url) result.append(h('a', {className: 'inspector-link', href: url, target: '_blank'}, text('S-INS-DISCOGS')));
    return result;
  }
  function track(s, step, pick) {
    const source = s.steps.byId[pick.step_id], marks = selectors.marks(s, pick.id);
    const current = s.player.current?.pickId === pick.id && s.player.state === 'playing';
    const youtube = pick.video_id ? `https://music.youtube.com/watch?v=${encodeURIComponent(pick.video_id)}` :
      `https://music.youtube.com/search?q=${encodeURIComponent(`${pick.artist} ${pick.title}`.trim())}`;
    const actions = h('div', {className: 'inspector-actions'},
      button('play', current ? '❚❚ Pause' : text('S-ROW-PLAY'), () => store.actions.playPick({pickId: pick.id}),
        {title: current ? '' : text('S-ROW-PLAY-TIP')}),
      ...['keep', 'pass'].map(verdict => button(verdict,
        text(verdict === 'keep' ? marks.kept ? 'S-ROW-KEEP-ON' : 'S-ROW-KEEP' : marks.passed ? 'S-ROW-PASS-ON' : 'S-ROW-PASS'),
        () => store.actions.judge({trackKey: pick.track_key, stepId: pick.step_id, verdict, mode: 'toggle'}),
        {disabled: s.ui.server !== 'up', 'aria-disabled': String(!!s.judgments.pending[pick.track_key]),
          title: text(verdict === 'keep' ? marks.kept ? 'S-ROW-KEEP-ON-TIP' : 'S-ROW-KEEP-TIP' :
            marks.passed ? 'S-ROW-PASS-ON-TIP' : 'S-ROW-PASS-TIP'),
          'aria-pressed': String(verdict === 'keep' ? marks.kept : marks.passed)})),
      h('a', {className: 'inspector-link', href: youtube, target: '_blank'}, pick.video_id ? 'Open in YouTube Music ↗' : 'Search YouTube Music ↗'));
    return [title(nameOf(pick), pick.facts?.source === 'discogs' ? pick.facts.thumb : null), actions,
      pick.step_id !== step.id && h('p', {className: 'inspector-muted'}, text('S-ROW-GONE-DIVIDER', {c: step.diff.gone_picks.length, n: source.number}),
        openLink(s, source.id, source.dig_id, selectionId(pointOf(pick)), text('S-INS-ORIGIN-FROM', {p: source.number}), 'gone-source')),
      why(pick, source, selectors.orderedPicks(s, pick.step_id)), alsoIn(s, pick, marks), facts(s, pick, source, step)];
  }
  function resource(s, step, point) {
    const hub = step.hubs.find(item => item.hub_key === point.point_id);
    const field = point.kind === 'label' ? 'labels' : point.kind === 'person' ? 'credits' : 'artists';
    const members = selectors.orderedPicks(s, step.id).filter(pick => hub ? hub.pick_ranks.includes(pick.rank) :
      pick.facts_state === 'ready' && pick.facts?.source === 'discogs' && pick.facts[field].some(entity => entityId(point.kind, entity) === point.point_id));
    const roles = [...new Set(members.flatMap(pick => pick.facts?.credits
      .filter(entity => entityId('person', entity) === point.point_id).flatMap(entity => entity.roles) || []))];
    const appearances = Object.values(s.steps.byId).filter(other => other.id !== step.id && other.dig_id === step.dig_id)
      .flatMap(other => (other.hubs || []).filter(item => item.hub_key === point.point_id).map(item =>
        openLink(s, other.id, other.dig_id, selectionId({point_id: item.hub_key, kind: item.kind}),
          text('S-INS-HUB-ALSO', {n: other.number, k: item.pick_ranks.length}), `hub-${other.id}`)));
    return [title(`${point.kind === 'label' ? '■ ' : point.kind === 'person' ? '◯ ' : ''}${point.name}`),
      h('p', {'data-members': members.length}, point.kind === 'artist' ? point.name : text(point.kind === 'label' ? 'S-INS-HUB-LABEL' : 'S-INS-HUB-PERSON', {k: members.length, roles: roles.join(', ')})),
      appearances, h('div', {className: 'inspector-members'}, members.map(pick => selectLink(s, step, pointOf(pick), nameOf(pick), `pick-${pick.rank}`))),
      h('div', {className: 'inspector-actions'}, moveControl(s, point, point.kind, text('S-INS-SOUNDS')),
        moveControl(s, point, point.kind, text('S-INS-SOUNDS-FAR'), true))];
  }
  function origin(s, step, picks) {
    const rows = step.origins.map(point => h('p', {}, step.move === 'seed' ? text('S-INS-ORIGIN-SEED', {
      'Artist - Title': point.name, k: picks.filter(pick => pick.route.layout_seed === point.point_id).length}) : point.name));
    if (step.parent_id) rows.push(openLink(s, step.parent_id, step.dig_id,
      step.context?.source_selection_id ?? selectionId(step.context?.source_point),
      text('S-INS-ORIGIN-FROM', {p: s.steps.byId[step.parent_id]?.number}), 'origin-parent'));
    if (step.origins.length === 1 && step.origins[0].kind === 'track') {
      const point = {...step.origins[0], source_step_id: step.id, source_rank: null, is_origin: true};
      rows.push(h('div', {className: 'inspector-actions'}, ['label', 'artist', 'person'].map(move =>
        moveControl(s, point, move, text(`S-E-EMPTY-BTN-${move.toUpperCase()}`)))));
    }
    return [title(text('S-INS-ORIGIN-H', {n: step.number})), rows];
  }
  function summary(s, step, picks) {
    const marks = picks.map(pick => selectors.marks(s, pick.id)), kept = selectors.keepsForStep(s, step.id);
    const digKept = selectors.keepsForDig(s, step.dig_id), factsProgress = progress(s, step.id);
    const rows = [title(step.header), step.origins.map(point => h('p', {className: 'inspector-muted'}, point.name)),
      h('p', {}, text('S-INS-SUM-COUNTS', {c: picks.length, k: kept.length, p: marks.filter(mark => mark.passed).length, h: marks.filter(mark => mark.heard).length}))];
    const hubs = step.hubs || [];
    rows.push(hubs.length ? h('div', {className: 'inspector-shared'}, 'Shared here: ', hubs.map((hub, i) => [i ? ' · ' : '',
      selectLink(s, step, {point_id: hub.hub_key, kind: hub.kind, name: hub.name, discogs_id: hub.discogs_id,
        source_step_id: step.id, source_rank: null}, `${hub.kind === 'label' ? '■' : '◯'} ${hub.name} (${hub.pick_ranks.length})`, `hub-${i}`)])) :
      h('p', {className: 'inspector-muted'}, text('S-MAP-NOHUB', {...factsProgress, loading: factsProgress.loading})));
    if (factsProgress.loading) rows.push(h('p', {className: 'inspector-muted'}, text('S-INS-SUM-FACTS', factsProgress)));
    // This step's keeps are a subset of the dig's: when they're all of them, the two moves are one.
    const moves = kept.length === digKept.length ? [['step', kept, 'S-INS-SUM-KEEPS']] :
      [['step', kept, 'S-INS-SUM-M'], ['dig', digKept, 'S-INS-SUM-SHIFTM']];
    rows.push(h('div', {className: 'inspector-actions'}, moves.filter(([, keeps]) => keeps.length).map(([scope, keeps, label]) =>
      button(`keeps-${scope}`, text(label, {k: keeps.length}),
        () => store.actions.soundsLikeKeeps({scope}), {disabled: s.ui.server !== 'up'}))),
    h('p', {className: 'inspector-muted'}, text('S-INS-SUM-HINT')));
    return rows;
  }
  function render(s) {
    const step = selectors.viewedStep(s);
    if (!step || s.ui.route.page !== 'dig') return;
    const point = selectors.selectedPoint(s), picks = selectors.orderedPicks(s, step.id);
    const pick = point?.kind === 'track' && point.source_rank != null ? s.picks.byId[`${point.source_step_id}:${point.source_rank}`] : null;
    const isOrigin = point?.is_origin;
    const kind = !point ? 'STEP' : isOrigin ? 'ORIGIN' : pick ? 'TRACK' : point.kind === 'label' ? 'LABEL' : point.kind === 'person' ? 'PERSON' : 'ORIGIN';
    const related = Object.values(s.steps.byId).map(item => [item.id, item.number, item.header, item.hubs]);
    const next = JSON.stringify([step, point, pick, picks, picks.map(item => selectors.marks(s, item.id)),
      isOrigin && selectors.moveChoices(s, point, {sourceStepId: step.id}),
      pick && selectors.marks(s, pick.id), related, selectors.keepsForDig(s, step.dig_id).map(item => item.id),
      s.player.current?.pickId, s.player.state, s.judgments.pending, s.ui.settings?.discogs_token?.set, s.ui.server, s.ui.route.view]);
    if (next === signature) return;
    signature = next;
    const key = `${step.id}:${s.ui.route.sel || ''}`, sameSelection = selection === key;
    const focused = root.contains(document.activeElement) ? document.activeElement.id : null;
    const scroll = sameSelection ? root.scrollTop : 0;
    const creditsOpen = sameSelection && body.querySelector('#inspector-credits')?.open;
    selection = key;
    heading.textContent = text(`S-INS-K-${kind}`, {n: step.number});
    root.dataset.stepId = step.id;
    root.dataset.pointId = point?.point_id || '';
    root.dataset.kind = kind.toLowerCase();
    body.replaceChildren(...(pick ? track(s, step, pick) : isOrigin ? origin(s, step, picks) : point ? resource(s, step, point) : summary(s, step, picks)).flat(Infinity).filter(Boolean));
    if (creditsOpen && body.querySelector('#inspector-credits')) body.querySelector('#inspector-credits').open = true;
    root.scrollTop = scroll;
    if (focused) {
      const target = document.getElementById(focused) || body.querySelector('#inspector-title');
      target?.focus({preventScroll: true});
      target?.scrollIntoView({block: 'nearest', inline: 'nearest'});
    }
  }
  function focusin(event) {
    if (store.getState().ui.focusRegion !== 'inspector') store.actions.focusRegion('inspector');
    event.target.scrollIntoView({block: 'nearest', inline: 'nearest'});
  }
  root.addEventListener('focusin', focusin);
  const unsubscribe = store.subscribe(render);
  render(store.getState());
  return {destroy() {
    destroyed = true; unsubscribe(); root.removeEventListener('focusin', focusin);
    root.replaceChildren(); root.classList.remove('inspector');
  }};
}
