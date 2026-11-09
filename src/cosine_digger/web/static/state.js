const terminal = (state) => ['done', 'failed', 'stopped', 'error'].includes(state);
const copy = (value) => structuredClone(value);
const encode = encodeURIComponent;
const baseRoute = () => ({page: 'dig', digId: null, stepId: null, sel: null, view: 'list'});
const unique = (items) => [...new Set(items)];
/** A YouTube Music login problem behind an export error: 'rejected', 'missing' or null. */
export const youtubeLoginProblem = (message) => /signed out/i.test(message || '') ? 'rejected' :
  /login missing/i.test(message || '') ? 'missing' : null;
const compareText = (a, b) => a < b ? -1 : a > b ? 1 : 0;
const nameOf = (pick) => pick.artist ? `${pick.artist} - ${pick.title}` : pick.title;
const factId = (kind, entity) => entity.id ? `${kind}:${entity.id}` :
  `${kind}:name:${entity.name.trim().toLowerCase().replace(/\s*\(\d+\)\s*$/, '').replace(/\s+/g, ' ').trim()}`;
const pickPoint = (pick) => ({point_id: `track:${pick.track_key}`, kind: 'track', name: nameOf(pick),
  track_key: pick.track_key, artist: pick.artist, title: pick.title, cosine_id: pick.cosine_id,
  video_id: pick.video_id, indexed: true, source_step_id: pick.step_id, source_rank: pick.rank});
const factPoint = (kind, entity, stepId, rank = null) => ({point_id: factId(kind, entity), kind,
  name: entity.name, discogs_id: entity.id ?? null, source_step_id: stepId, source_rank: rank});
const hubPoint = (hub, stepId) => ({point_id: hub.hub_key, kind: hub.kind, name: hub.name,
  discogs_id: hub.discogs_id, source_step_id: stepId, source_rank: null});
/** URL identity is distinct from the resource identity used for moves. */
export const selectionId = (point) => point?.is_origin ? `origin:${point.point_id}` :
  point && point.kind !== 'track' && point.source_rank != null ?
    `fact:${point.source_rank}:${point.point_id}` : point?.point_id || null;
const originPoint = (point, stepId) => ({...point, source_step_id: stepId, source_rank: null, is_origin: true});
const selectionPoint = (point) => ({...point, selection_id: selectionId(point), is_origin: !!point.is_origin});
const requestPoint = (point) => {
  // library_source only words a library result in the seed box: it's never saved with a step.
  const {resolved_tracks, resolution, selection_id, is_origin, library_source, ...requested} = point;
  return requested;
};

export function parseRoute(hash = '') {
  if (hash === '#feeds' || hash === '#playlists') return {...baseRoute(), page: 'feeds'};
  if (hash === '#setup') return {...baseRoute(), page: 'setup'};
  if (hash === '#connect') return {...baseRoute(), page: 'connect'};
  const match = /^#\/dig\/([^/?]+)(?:\/step\/([^/?]+))?(?:\?(.*))?$/.exec(hash);
  if (!match || match[1] === 'new') return baseRoute();
  try {
    const query = new URLSearchParams(match[3] || '');
    return {page: 'dig', digId: decodeURIComponent(match[1]), stepId: match[2] ? decodeURIComponent(match[2]) : null,
      sel: query.get('sel') || null, view: query.get('view') === 'map' ? 'map' : 'list'};
  } catch { return baseRoute(); }
}

export function formatRoute(route) {
  if (['feeds', 'setup', 'connect'].includes(route.page)) return `#${route.page}`;
  if (!route.digId || !route.stepId) return '#/dig/new';
  const query = new URLSearchParams();
  if (route.sel) query.set('sel', route.sel);
  query.set('view', route.view === 'map' ? 'map' : 'list');
  return `#/dig/${encode(route.digId)}/step/${encode(route.stepId)}?${query}`;
}

function pointInStep(s, stepId, pointId) {
  if (!pointId) return null;
  const step = s.steps.byId[stepId];
  if (!step) return null;
  if (pointId === 'origin' || pointId.startsWith('origin:')) {
    const origin = pointId === 'origin' ? step.origins?.[0] :
      step.origins?.find((item) => item.point_id === pointId.slice('origin:'.length));
    return origin ? selectionPoint(originPoint(origin, stepId)) : null;
  }
  const factToken = /^fact:(\d+):(.+)$/.exec(pointId);
  if (factToken) {
    const pick = selectors.orderedPicks(s, stepId).find((item) => item.rank === Number(factToken[1]));
    if (!pick) return null;
    for (const [kind, field] of [['label', 'labels'], ['artist', 'artists'], ['person', 'credits']]) {
      const entity = pick.facts?.[field]?.find((item) => factId(kind, item) === factToken[2]);
      if (entity) return selectionPoint(factPoint(kind, entity, stepId, pick.rank));
    }
    return factId('artist', {name: pick.artist}) === factToken[2] ?
      selectionPoint(factPoint('artist', {name: pick.artist}, stepId, pick.rank)) : null;
  }
  const pick = selectors.orderedPicks(s, stepId).find((item) => `track:${item.track_key}` === pointId || item.id === pointId)
    || step.diff?.gone_picks?.find((item) => `track:${item.track_key}` === pointId || item.id === pointId);
  if (pick) return selectionPoint(pickPoint(pick));
  const hub = step.hubs?.find((item) => item.hub_key === pointId);
  if (hub) return selectionPoint(hubPoint(hub, stepId));
  for (const item of selectors.orderedPicks(s, stepId)) {
    for (const [kind, field] of [['label', 'labels'], ['artist', 'artists'], ['person', 'credits']]) {
      const fact = item.facts?.[field]?.find((entity) => factId(kind, entity) === pointId);
      if (fact) return selectionPoint(factPoint(kind, fact, stepId, item.rank));
    }
    if (factId('artist', {name: item.artist}) === pointId)
      return selectionPoint(factPoint('artist', {name: item.artist}, stepId, item.rank));
  }
  // Pre-upgrade bare origin ids remain valid when no pick, hub or fact owns the key.
  const origin = step.origins?.find((item) => item.point_id === pointId);
  return origin ? selectionPoint(originPoint(origin, stepId)) : null;
}

function effective(s, trackKey) {
  const pending = s.judgments.pending[trackKey];
  return pending ? {verdict: pending.verdict, step_id: pending.stepId, source: 'gui', at: null} :
    s.judgments.byTrack[trackKey] || {verdict: null, step_id: null, source: 'none', at: null};
}

function keeps(s, picks) {
  const seen = new Set();
  return picks.filter((pick) => {
    if (effective(s, pick.track_key).verdict !== 'keep' || seen.has(pick.track_key)) return false;
    seen.add(pick.track_key);
    return true;
  }).sort((a, b) => compareText(effective(s, b.track_key).at || '', effective(s, a.track_key).at || '')
    || compareText(a.track_key, b.track_key));
}

export const selectors = {
  viewedDig(s) {
    const dig = s.digs.byId[s.ui.route.digId];
    return dig ? {...dig, step_count: (s.steps.byDig[dig.id] || []).length, kept_count: selectors.keepsForDig(s, dig.id).length} : null;
  },
  viewedStep: (s) => s.steps.byId[s.ui.route.stepId] || null,
  orderedPicks: (s, stepId) => (s.picks.byStep[stepId] || []).map((id) => s.picks.byId[id]),
  selectedPoint: (s, stepId = s.ui.route.stepId) => pointInStep(s, stepId, s.ui.selectionByStep[stepId]),
  playingPick: (s) => s.picks.byId[s.player.current?.pickId] || null,
  judgingTarget(s) {
    if (s.player.state !== 'idle' && s.player.current) return {...s.player.current};
    const point = selectors.selectedPoint(s);
    if (!point?.track_key || point.source_rank == null) return null;
    const pickId = `${point.source_step_id}:${point.source_rank}`;
    return {pickId, trackKey: point.track_key, stepId: point.source_step_id,
      digId: s.steps.byId[point.source_step_id]?.dig_id, rank: point.source_rank, videoId: point.video_id};
  },
  marks(s, pickId) {
    const pick = s.picks.byId[pickId];
    if (!pick) return null;
    const step = s.steps.byId[pick.step_id], judgment = effective(s, pick.track_key);
    const refusal = s.player.refusedByVideo[pick.video_id];
    const appearances = [...(step?.also_in?.[pick.track_key] || [])];
    for (const id of s.picks.byTrack[pick.track_key] || []) {
      const other = s.picks.byId[id], otherStep = s.steps.byId[other.step_id];
      if (other.step_id === pick.step_id || other.step_id === step?.rerun_of || appearances.some((item) => item.step_id === other.step_id)) continue;
      appearances.push({dig_id: otherStep.dig_id, dig_name: s.digs.byId[otherStep.dig_id]?.name || '',
        step_id: other.step_id, number: otherStep.number, rank: other.rank, header: otherStep.header});
    }
    return {judgment, kept: judgment.verdict === 'keep', passed: judgment.verdict === 'pass',
      heard: s.heard.byTrack[pick.track_key] || null, also_in: appearances.filter((item) => item.step_id !== step?.rerun_of),
      new: (step?.diff?.new_track_keys || []).includes(pick.track_key),
      gone: !!selectors.viewedStep(s)?.diff?.gone_picks?.some((item) => item.id === pickId),
      refused: !!refusal, unavailable: !!refusal && ![101, 150].includes(refusal.code), noVideo: !pick.video_id};
  },
  keepsForStep: (s, id) => keeps(s, selectors.orderedPicks(s, id)),
  keepsForDig: (s, id) => keeps(s, (s.steps.byDig[id] || []).flatMap((step) => selectors.orderedPicks(s, step))),
  isPicksComplete(s, stepId) {
    const step = s.steps.byId[stepId];
    return !!step && (!!step.picks_done_at || terminal(s.steps.jobs[step.current_job_id]?.state || step.status));
  },
  queuePlayable(s, pickId) {
    const pick = s.picks.byId[pickId];
    return !!pick?.video_id && effective(s, pick.track_key).verdict !== 'pass' && !s.player.refusedByVideo[pick.video_id];
  },
  seedResults(s) {
    const seen = new Set();
    return [...s.ui.search.cosine, ...s.ui.search.library].filter((point) => {
      if (seen.has(point.point_id)) return false;
      seen.add(point.point_id);
      return true;
    });
  },
  trailRows(s, digId) {
    const steps = (s.steps.byDig[digId] || []).map((id) => s.steps.byId[id]);
    const path = new Set();
    let id = s.ui.route.stepId;
    while (id && !path.has(id)) { path.add(id); id = s.steps.byId[id]?.parent_id; }
    const rows = [];
    function visit(step, depth) {
      const children = steps.filter((item) => item.parent_id === step.id);
      const count = selectors.orderedPicks(s, step.id).length;
      const loaded = !!s.picks.byStep[step.id];
      rows.push({type: 'step', stepId: step.id, step, depth, selected: step.id === s.ui.route.stepId,
        playing: s.player.current?.stepId === step.id, picksComplete: selectors.isPicksComplete(s, step.id),
        counts: loaded ? {picks: count, kept: selectors.keepsForStep(s, step.id).length,
          passed: selectors.orderedPicks(s, step.id).filter((pick) => effective(s, pick.track_key).verdict === 'pass').length} : step.counts});
      if (path.has(step.id) || s.ui.expandedBranches.includes(step.id)) children.forEach((child) => visit(child, depth + 1));
      else if (children.length) rows.push({type: 'stub', stepId: step.id, depth: depth + 1, count: children.length});
    }
    steps.filter((step) => !step.parent_id).forEach((step) => visit(step, 0));
    return rows;
  },
  hubScene(s, stepId) {
    const step = s.steps.byId[stepId];
    return {step, picks: selectors.orderedPicks(s, stepId), hubs: step?.hubs || [],
      originPoints: (step?.origins || []).map((point) => originPoint(point, stepId)),
      selection: selectors.selectedPoint(s, stepId), hover: s.ui.hover?.stepId === stepId ? s.ui.hover.point : null,
      playing: selectors.playingPick(s), marks: Object.fromEntries(selectors.orderedPicks(s, stepId)
        .map((pick) => [pick.id, selectors.marks(s, pick.id)])), reducedMotion: s.ui.reducedMotion};
  },
  moveChoices(s, point, {sourceStepId = s.ui.route.stepId, further = false} = {}) {
    if (!point) return [];
    const source = pointInStep(s, sourceStepId, selectionId(point)) || point;
    const row = (move, label_id, candidate, picker_points = [], disabled_reason = null) => ({id: move, move,
      label_id, params: {Label: candidate?.kind === 'label' ? candidate.name : '',
        Artist: candidate?.kind === 'artist' ? candidate.name : source.artist || '', Person: candidate?.kind === 'person' ? candidate.name : ''},
      point: candidate, disabled_reason, picker_points});
    // A token with a space or line break is never sent: Discogs moves treat it as no token.
    const noToken = !s.ui.settings?.discogs_token?.set || !!s.ui.settings.discogs_token.malformed;
    if (source.kind !== 'track') {
      if (!['label', 'person', 'artist'].includes(source.kind)) return [];
      return [row(source.kind, source.kind === 'label' ? 'S-MENU-HUB-L' : source.kind === 'person' ? 'S-MENU-HUB-C' : 'S-MENU-A',
        source, [], noToken ? 'S-MENU-NOTOKEN' : null)];
    }
    const step = s.steps.byId[sourceStepId];
    const origin = source.is_origin && step?.origins.find((item) => item.point_id === source.point_id);
    const provenance = source.is_origin && step?.context?.source_point?.source_rank != null ?
      step.context.source_point : origin || source;
    const pick = s.picks.byId[`${provenance.source_step_id}:${provenance.source_rank}`];
    const factStepId = pick?.step_id || source.source_step_id || sourceStepId;
    const factRank = pick?.rank ?? source.source_rank;
    const ownMove = step?.move === 'track' && step.further === further &&
      step.origins.length === 1 && step.origins[0].point_id === source.point_id && source.source_rank == null;
    const rows = [row('track', 'S-MENU-T', source, [], ownMove ? 'S-T-SAMEMOVE' : null)];
    for (const [kind, field, label] of [['label', 'labels', 'S-MENU-L'], ['artist', 'artists', 'S-MENU-A'], ['person', 'credits', 'S-MENU-C']]) {
      let candidates = (pick?.facts?.[field] || []).map((entity) => factPoint(kind, entity, factStepId, factRank));
      if (kind === 'artist' && !candidates.length && source.artist)
        candidates = [factPoint(kind, {name: source.artist}, factStepId, factRank)];
      candidates = candidates.filter((candidate, index) => candidates.findIndex((item) => item.point_id === candidate.point_id) === index);
      const disabled = noToken ? 'S-MENU-NOTOKEN' : candidates.length ? null :
        pick?.facts_state === 'loading' ? 'S-MENU-LOADING' : kind === 'label' ? 'S-MENU-NOLABEL' : 'S-MENU-NONE';
      rows.push(row(kind, label, candidates.length === 1 ? candidates[0] : null, candidates, disabled));
    }
    return rows;
  },
  whereNext(s, sourceStepId) {
    const step = s.steps.byId[sourceStepId];
    if (!step) return [];
    const hubs = [...step.hubs].sort((a, b) => b.pick_ranks.length - a.pick_ranks.length ||
      a.kind.localeCompare(b.kind) || a.name.localeCompare(b.name) || a.hub_key.localeCompare(b.hub_key));
    const choices = hubs.flatMap((hub) => selectors.moveChoices(s, hubPoint(hub, sourceStepId), {sourceStepId})).slice(0, 3);
    const last = s.player.current?.stepId === sourceStepId ? selectors.playingPick(s) : selectors.orderedPicks(s, sourceStepId).at(-1);
    if (last && choices.length < 3) choices.push(...selectors.moveChoices(s, pickPoint(last), {sourceStepId})
      .filter((item) => ['track', 'artist'].includes(item.move)).slice(0, 3 - choices.length));
    return choices;
  },
  keyHints(s, region = s.ui.focusRegion) {
    const names = {search: 'SEARCH', list: 'LIST', trail: 'TRAIL', map: 'MAP', inspector: 'INSPECTOR',
      territory: 'TERR', menu: 'MENU', picker: 'MENU', iframe: 'IFRAME'};
    return {id: `S-KS-${names[region] || 'LIST'}`, params: {}, judging: s.player.state === 'idle' ? 'selected' : 'playing'};
  },
  setupState: (s) => ({...s.ui.setup, settings: s.ui.settings, enrichment: s.ui.enrichment}),
  feedState: (s, kind) => s.ui.feeds[kind] || null,
  /** The cosine.club key as search, digs and the key screen see it. Usable: set, sendable, and
   * not refused by cosine.club (an unchecked key counts: Digger finds out on the first call). */
  cosineKey(s) {
    const key = s.ui.settings?.cosine_token;
    if (!key) return {known: false, set: false, usable: false, state: 'unknown', source: null};
    // keyDoubt: a refusal Digger couldn't read the key's state after (missing or rejected).
    const doubt = s.ui.keyDoubt, set = key.set && doubt !== 'missing';
    const state = !set ? 'missing' : key.malformed ? 'malformed' :
      key.checked === 'rejected' || doubt === 'rejected' ? 'rejected' : 'ok';
    return {...key, set, known: true, usable: state === 'ok', state};
  },
  /** A4: the key screen gates only a home with no usable key and no saved digs; unknown never gates. */
  keyGate(s) {
    const key = selectors.cosineKey(s);
    return key.known && !key.usable && s.ui.settings?.has_digs === false && !s.digs.order.length;
  },
  hasLibrary: (s) => (s.ui.enrichment?.overview?.total_tracks || 0) > 0,
};

function initialState() {
  return {digs: {byId: {}, order: []}, steps: {byId: {}, byDig: {}, jobs: {}, lastSeq: {}},
    picks: {byId: {}, byStep: {}, byTrack: {}}, judgments: {byTrack: {}, revisions: {}, pending: {}, undo: []},
    heard: {byTrack: {}, pending: {}}, player: {state: 'idle', phase: 'empty', current: null,
      queue: {digId: null, stepId: null, startRank: 1, pickIds: [], index: -1}, nextQueue: null,
      position: 0, duration: 0, gesture: false, heardSeconds: {}, refusedByVideo: {}, intent: null, generation: 0},
    ui: {route: baseRoute(), selectionByStep: {}, scrollByStep: {}, lastStepByDig: {}, lastDigRoute: null,
      hover: null, focusRegion: 'search', menu: null, sheet: false, toast: null, announcement: null,
      expandedBranches: [], keptOpenByDig: {}, trailRail: false, switcher: false,
      search: {query: '', chips: [], generation: 0, library: [], cosine: [], pending: {library: false, cosine: false},
        highlight: -1, highlightMoved: false, resultsOpen: false, hint: null, note: null, blocked: null, submitting: false},
      // Where the key screen returns once cosine.club accepts a key, and the query to run again then.
      connect: {origin: null, query: '', focus: null}, server: 'connecting', routeGeneration: 0,
      // keyDoubt: a request was refused and the settings read after it failed: the refusal stands
      // until a read succeeds. doctorAsked/keyCheckedAt order Health's check and the key's own.
      // digExports: each dig's latest send to YouTube Music this session (a legacy job).
      openReason: 'initial', settings: null, keyDoubt: null, feeds: {}, digExports: {},
      setup: {health: null, doctor: null, login: null, scan: null, doctorAsked: 0, keyCheckedAt: 0},
      enrichment: null, reducedMotion: false}};
}

function freeze(value) {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
}

const browserClock = {now: () => performance.now(), setTimeout: (...args) => globalThis.setTimeout(...args),
  clearTimeout: (id) => globalThis.clearTimeout(id), setInterval: (...args) => globalThis.setInterval(...args),
  clearInterval: (id) => globalThis.clearInterval(id)};

export function createStore({api, storage = globalThis.localStorage, sessionStorage = globalThis.sessionStorage,
  history = globalThis.history, location = globalThis.location, clock = browserClock}) {
  let state = freeze(initialState()), destroyed = false, intentNumber = 0, userIntent = 0, announcementNumber = 0;
  let settingsRevision = -Infinity, keyEvents = 0;  // the server's settings revision shown; Health and key checks in order
  let searchTimer = null, searchResolve = null, persistTimer = null, lastPersist = -Infinity, refusalTimer = null, lastPlayingAt = null;
  let rerunTimer = null;
  let rootPending = null, undoPending = Promise.resolve(), reconnecting = null, hydrated = false;
  const listeners = new Set(), follows = new Map(), judgmentChains = new Map(), completedJobs = new Set(), operationKeys = new Map();
  const acceptedOperations = new Map();
  const lifetime = new AbortController();
  const PREFIX = 'slice:v1:';
  function readStorage(target, key, fallback) {
    try { return JSON.parse(target?.getItem(PREFIX + key)) ?? fallback; } catch { return fallback; }
  }
  function writeStorage(target, key, value) {
    try { target?.setItem(PREFIX + key, JSON.stringify(value)); } catch { /* Storage may be disabled or full. */ }
  }
  function persist() {
    clock.clearTimeout(persistTimer); persistTimer = null; lastPersist = clock.now();
    if (!hydrated) return;
    writeStorage(storage, 'route', state.ui.lastDigRoute);
    writeStorage(storage, 'kept', state.ui.keptOpenByDig);
    writeStorage(sessionStorage, 'session', {selectionByStep: state.ui.selectionByStep, scrollByStep: state.ui.scrollByStep,
      lastStepByDig: state.ui.lastStepByDig, player: {queue: state.player.queue, nextQueue: state.player.nextQueue, current: state.player.current,
        position: state.player.position, refusedByVideo: state.player.refusedByVideo},
      // A reload on the key screen still returns where it came from. A route and a search query: never the key.
      connect: {origin: state.ui.connect.origin, query: state.ui.connect.query}});
  }
  function commit(change, paths = ['ui']) {
    if (destroyed) return;
    // Copy the declared write paths; frozen branches outside them are shared.
    // High-frequency UI changes never traverse the loaded step/pick snapshots.
    const previous = state, draft = {...state}, cloned = new Set();
    for (const path of paths) {
      const keys = path.split('.');
      let target = draft, source = state, prefix = '';
      keys.forEach((key, index) => {
        prefix += '.' + key;
        if (!cloned.has(prefix)) {
          target[key] = index === keys.length - 1 ? copy(source[key]) :
            Array.isArray(source[key]) ? [...source[key]] : {...source[key]};
          cloned.add(prefix);
        }
        target = target[key]; source = source[key];
      });
    }
    change(draft);
    state = freeze(draft);
    if (state.steps.jobs !== previous.steps.jobs || state.ui.route.stepId !== previous.ui.route.stepId ||
        state.ui.route.page !== previous.ui.route.page || state.player.current?.stepId !== previous.player.current?.stepId ||
        (state.player.state === 'idle') !== (previous.player.state === 'idle')) syncFollows();
    if (state.player.current?.pickId !== previous.player.current?.pickId)
      followPlayback(previous.player.current, state.player.current);
    if (hydrated && persistTimer == null) persistTimer = clock.setTimeout(persist, Math.max(0, 1000 - (clock.now() - lastPersist)));
    listeners.forEach((listener) => listener(state));
  }
  // While a step's selection is the track that was playing, the next track to
  // play in that step takes it, so the inspector shows what plays. Selecting
  // anything else stops it following (product-ux §5.1).
  function followPlayback(before, now) {
    if (!before || !now || before.stepId !== now.stepId) return;
    const was = `track:${before.trackKey}`, next = `track:${now.trackKey}`;
    const viewing = state.ui.route.stepId === now.stepId;
    if ((viewing ? state.ui.route.sel : state.ui.selectionByStep[now.stepId]) !== was || was === next) return;
    commit((draft) => {
      draft.ui.selectionByStep[now.stepId] = next;
      if (viewing) { draft.ui.route.sel = next; draft.ui.lastDigRoute = copy(draft.ui.route); }
    }, ['ui.selectionByStep', 'ui.route', 'ui.lastDigRoute']);
    if (viewing) historyRoute(state.ui.route, 'replace');
  }
  function announce(draft, id, params) { draft.ui.announcement = {id, params, serial: ++announcementNumber}; }
  function toast(draft, id, params = {}, extra = {}) {
    draft.ui.toast = {id, params, serial: ++announcementNumber, ...extra};
    announce(draft, id, params);
  }
  function historyRoute(route, method) {
    const hash = formatRoute(route);
    if (method === 'none' && location?.hash === hash) { lastHash = hash; return; }
    if (method === 'push' && location?.hash === hash) return;
    history?.[method === 'push' ? 'pushState' : 'replaceState']?.(null, '', hash);
    lastHash = hash;
  }
  async function request(method, path, body, options) {
    try { return await api.request(method, path, body, {signal: lifetime.signal, ...options}); }
    catch (error) {
      if (error.status === 0) commit((draft) => { draft.ui.server = 'down'; });
      throw error;
    }
  }
  function mutable() {
    if (destroyed) return false;
    if (state.ui.server === 'up') return true;
    actions.showToast('S-SRV-LOST');
    return false;
  }
  function reindex(draft) {
    draft.picks.byTrack = {};
    for (const id of Object.values(draft.picks.byStep).flat()) {
      const key = draft.picks.byId[id].track_key;
      (draft.picks.byTrack[key] ??= []).push(id);
    }
  }
  function putPick(draft, pick) {
    const ids = draft.picks.byStep[pick.step_id] ??= [];
    const existing = ids.map((id) => draft.picks.byId[id]).find((item) => item.rank === pick.rank);
    if (existing && (existing.id !== pick.id || existing.track_key !== pick.track_key || existing.cosine_id !== pick.cosine_id))
      throw new Error(`Conflicting pick identity at ${pick.step_id}:${pick.rank}`);
    draft.picks.byId[pick.id] = copy(pick);
    if (!ids.includes(pick.id)) ids.push(pick.id);
    ids.sort((a, b) => draft.picks.byId[a].rank - draft.picks.byId[b].rank);
  }
  function currentFor(draft, pick) {
    return {pickId: pick.id, trackKey: pick.track_key, stepId: pick.step_id,
      digId: draft.steps.byId[pick.step_id].dig_id, rank: pick.rank, videoId: pick.video_id};
  }
  function playerIntent(draft, type, params = {}, newGeneration = false) {
    if (newGeneration) {
      draft.player.generation++;
      clock.clearTimeout(refusalTimer); refusalTimer = null; lastPlayingAt = null;
    }
    draft.player.intent = {id: ++intentNumber, type, generation: draft.player.generation, ...params};
  }
  function loadPick(draft, pick, {autoplay = true, position = 0} = {}) {
    draft.player.current = currentFor(draft, pick);
    draft.player.state = autoplay ? 'loading' : 'paused';
    draft.player.phase = autoplay ? 'buffering' : 'cued';
    draft.player.position = position; draft.player.duration = 0;
    playerIntent(draft, autoplay ? 'load' : 'cue', {pickId: pick.id, videoId: pick.video_id, position, autoplay}, true);
  }
  function makeQueue(s, stepId, startRank = 1) {
    return {digId: s.steps.byId[stepId]?.dig_id || null, stepId, startRank,
      pickIds: selectors.orderedPicks(s, stepId).filter((pick) => pick.rank >= startRank).map((pick) => pick.id), index: -1};
  }
  function advance(draft) {
    if (draft.player.nextQueue) {
      draft.player.queue = draft.player.nextQueue; draft.player.nextQueue = null;
    }
    const queue = draft.player.queue;
    const nextIndex = queue.pickIds.findIndex((id, index) => index > queue.index && selectors.queuePlayable(draft, id));
    if (nextIndex !== -1) {
      queue.index = nextIndex;
      loadPick(draft, draft.picks.byId[queue.pickIds[nextIndex]]);
    } else {
      const complete = queue.single || selectors.isPicksComplete(draft, queue.stepId);
      draft.player.state = complete ? 'idle' : 'loading'; draft.player.phase = complete ? 'ended' : 'waiting';
      playerIntent(draft, 'stop', {}, true);
    }
  }
  function extendQueue(draft, stepId) {
    for (const queue of [draft.player.queue, draft.player.nextQueue]) {
      if (queue?.stepId !== stepId || queue.single) continue;
      queue.pickIds = selectors.orderedPicks(draft, stepId).filter((pick) => pick.rank >= queue.startRank).map((pick) => pick.id);
    }
    if (draft.player.queue.stepId === stepId && draft.player.phase === 'waiting') advance(draft);
  }
  function completeAnnouncement(draft, jobId, failure = null) {
    if (completedJobs.has(jobId)) return;
    completedJobs.add(jobId);
    const job = draft.steps.jobs[jobId], step = draft.steps.byId[job?.step_id];
    if (step && draft.ui.route.stepId !== step.id) announce(draft, failure ? 'S-AN-STEP-FAIL' : 'S-AN-STEP-DONE',
      {n: step.number, c: selectors.orderedPicks(draft, step.id).length, reason: failure?.message || ''});
  }
  function legacyStreamCount() {
    return [...follows.values()].filter((entry) => !entry.slice && !entry.pollOnly).length;
  }
  function streamJobs() {
    return unique([
      state.ui.route.page === 'dig' ? state.steps.byId[state.ui.route.stepId]?.current_job_id : null,
      state.player.state !== 'idle' ? state.steps.byId[state.player.current?.stepId]?.current_job_id : null,
      state.ui.route.page === 'setup' ? state.ui.enrichment?.id : null,
    ].filter((id) => id && !terminal(state.steps.jobs[id]?.state))).slice(0, Math.max(0, 2 - legacyStreamCount()));
  }
  function syncFollows() {
    const streaming = streamJobs();
    for (const [id, entry] of follows) {
      if (entry.slice && (terminal(state.steps.jobs[id]?.state) || entry.pollOnly !== !streaming.includes(id))) {
        entry.close?.(); follows.delete(id);
      }
    }
    for (const job of Object.values(state.steps.jobs)) if (!terminal(job.state)) follow(job.id);
  }
  function follow(jobId) {
    if (destroyed || terminal(state.steps.jobs[jobId]?.state)) return;
    const pollOnly = !streamJobs().includes(jobId), previous = follows.get(jobId);
    if (previous && previous.pollOnly === pollOnly) return;
    previous?.close?.();
    const entry = {slice: true, pollOnly, close: null};
    follows.set(jobId, entry);
    entry.close = api.followStepJob(jobId, {after: state.steps.lastSeq[jobId] || 0, pollOnly,
      onEvent: (event) => {
        if (destroyed) return;
        if (state.ui.server === 'down') actions.setServer('up');
        if (event.event === 'snapshot') actions.mergeSnapshot(event.envelope);
        else if (event.event === 'job') {
          commit((draft) => {
            draft.steps.jobs[jobId] = copy(event.job); draft.steps.lastSeq[jobId] = event.job.last_seq;
            if (event.job.kind === 'label_enrichment') draft.ui.enrichment = copy(event.job);
          }, ['steps', 'ui']);
        } else actions.applyJobEvent(event);
        if (terminal(state.steps.jobs[jobId]?.state)) {
          follows.get(jobId)?.close?.(); follows.delete(jobId);
          const stepId = state.steps.jobs[jobId]?.step_id;
          if (stepId) loadStep(stepId).catch(() => {});
        }
      },
      onDisconnect: (error) => {
        if (error?.status !== 404) { actions.setServer('down'); return; }
        follows.get(jobId)?.close?.(); follows.delete(jobId);
        commit((draft) => {
          const job = draft.steps.jobs[jobId];
          job.state = 'stopped'; job.error = {code: 'job_missing', message: error.message, retry_after_s: null, retry_at: null};
          const step = draft.steps.byId[job.step_id];
          if (step) { step.status = 'stopped'; step.error = job.error; completeAnnouncement(draft, jobId, job.error); }
          if (job.kind === 'label_enrichment') draft.ui.enrichment = {...draft.ui.enrichment, ...copy(job)};
          toast(draft, null, {}, {message: error.message, error: true});
        }, ['steps', 'ui']);
      },
    });
  }
  async function loadStep(stepId) {
    const envelope = await request('GET', `/api/steps/${encode(stepId)}`);
    actions.mergeSnapshot(envelope);
    if (!destroyed) follow(envelope.job.id);
    return envelope;
  }
  function mergeDig(dig) {
    commit((draft) => {
      const {steps = [], ...metadata} = dig;
      if (!draft.digs.order.includes(dig.id)) draft.digs.order.push(dig.id);
      for (const summary of steps) {
        const previous = draft.steps.byId[summary.id];
        if (previous?.current_job_id === summary.current_job_id) draft.steps.byId[summary.id] = {...copy(summary), ...previous};
        else if (!previous) draft.steps.byId[summary.id] = {...copy(summary), hubs: [], batches: [], diff: null, also_in: {}, rerun_steps: []};
      }
      draft.steps.byDig[dig.id] = unique([...(draft.steps.byDig[dig.id] || []), ...steps.map((step) => step.id)])
        .sort((a, b) => draft.steps.byId[a].number - draft.steps.byId[b].number);
      draft.digs.byId[dig.id] = {...keepPlaylistLink(draft.digs.byId[dig.id], metadata), step_count: draft.steps.byDig[dig.id].length,
        last_step_id: draft.steps.byDig[dig.id].at(-1) || null};
    }, ['digs', 'steps']);
  }
  // A dig read that left before a send finished never takes back that send's playlist link. Any read
  // that has a link wins: during a first send the server's sent time is briefly the creation time.
  function keepPlaylistLink(previous, dig) {
    return previous?.youtube_playlist_url && !dig.youtube_playlist_url ?
      {...dig, youtube_playlist_url: previous.youtube_playlist_url, youtube_sent_at: previous.youtube_sent_at} : dig;
  }
  async function loadDig(digId) {
    const dig = await request('GET', `/api/digs/${encode(digId)}`);
    mergeDig(dig);
    await Promise.all(dig.steps.map((step) => loadStep(step.id)));
    return dig;
  }
  async function loadRecent() {
    const result = await request('GET', '/api/digs?limit=10');
    commit((draft) => {
      draft.digs.order = result.digs.map((dig) => dig.id);
      for (const dig of result.digs) draft.digs.byId[dig.id] = keepPlaylistLink(draft.digs.byId[dig.id], copy(dig));
    }, ['digs']);
    return result.digs;
  }
  async function resolveRoute(route, reason = 'return') {
    let target = {...baseRoute(), ...route}, notice = null;
    if (route.page === 'dig' && !Object.hasOwn(route, 'digId') && !Object.hasOwn(route, 'stepId'))
      target = copy(state.ui.lastDigRoute || baseRoute());
    if (target.page === 'dig' && target.digId) {
      try {
        const dig = await loadDig(target.digId);
        if (!dig.steps.some((step) => step.id === target.stepId)) {
          if (target.stepId) notice = 'That step is not in this dig. Opened a saved step.';
          const remembered = state.ui.lastStepByDig[dig.id];
          target.stepId = dig.steps.some((step) => step.id === remembered) ? remembered : dig.last_step_id || dig.root_step_id;
          target.sel = null;
        }
      } catch (error) {
        if (![404, 422].includes(error.status)) throw error;
        const recent = await loadRecent();
        const fallback = recent.find((dig) => dig.id !== target.digId);
        target = fallback ? {...baseRoute(), digId: fallback.id, stepId: fallback.root_step_id} : baseRoute();
        if (fallback) await loadDig(fallback.id);
        notice = 'That dig is no longer available. Opened your recent digs.';
      }
    }
    if (target.stepId) {
      const selected = target.sel ?? (notice || reason === 'created' ? null : state.ui.selectionByStep[target.stepId]);
      const point = pointInStep(state, target.stepId, selected);
      if (selected && !point) notice = 'That selection is no longer in this step.';
      target.sel = selectionId(point);
    }
    return {target, notice};
  }
  async function openRoute(route, {history: method = 'push', reason = 'return', token, origin} = {}) {
    const intent = token ?? ++userIntent;
    const generation = state.ui.routeGeneration + 1;
    commit((draft) => { draft.ui.routeGeneration = generation; }, ['ui.routeGeneration']);
    let {target, notice} = await resolveRoute(route, reason);
    if (destroyed || intent !== userIntent || generation !== state.ui.routeGeneration) return;
    // A4: with no usable cosine.club key and no saved digs, Start a dig is the key screen.
    if (target.page === 'dig' && !target.digId && selectors.keyGate(state)) target = {...baseRoute(), page: 'connect'};
    commit((draft) => {
      if (target.page === 'connect' && (origin !== undefined || draft.ui.route.page !== 'connect')) {
        // The key screen returns here once a key is accepted, with the query typed before it.
        draft.ui.connect.origin = copy(origin ?? draft.ui.route);
        if (origin === undefined) draft.ui.connect.query = draft.ui.search.query;
      }
      draft.ui.route = target; draft.ui.openReason = reason === 'created' ? 'created' : 'return';
      draft.ui.menu = null; draft.ui.switcher = false; draft.ui.hover = null;
      if (target.page === 'dig') {
        draft.ui.lastDigRoute = target;
        if (target.stepId) {
          draft.ui.selectionByStep[target.stepId] = target.sel;
          draft.ui.lastStepByDig[target.digId] = target.stepId;
          let parent = draft.steps.byId[target.stepId]?.parent_id;
          while (parent) { draft.ui.expandedBranches = unique([...draft.ui.expandedBranches, parent]); parent = draft.steps.byId[parent]?.parent_id; }
        }
        draft.ui.focusRegion = target.stepId ? 'list' : 'search';
      }
      if (notice) toast(draft, null, {}, {message: notice});
    });
    historyRoute(target, notice && method === 'none' ? 'replace' : method);
    if (target.page === 'feeds') await actions.loadFeeds();
    if (target.page === 'setup') await actions.loadSetup();
    return target;
  }
  async function submit(path, body, {root = false} = {}) {
    if (!mutable()) return null;
    const intent = ++userIntent;
    const operation = JSON.stringify({path, body});
    const key = operationKeys.get(operation) || globalThis.crypto.randomUUID();
    const headers = {'Idempotency-Key': key};
    try {
      let result = acceptedOperations.get(operation);
      try { result ??= await request('POST', path, body, {headers}); }
      catch (error) {
        if (error.status !== 0) throw error;
        operationKeys.set(operation, key);
        result = await request('POST', path, body, {headers});
        await actions.setServer('up');
      }
      acceptedOperations.set(operation, result);
      if (operationKeys.get(operation) === key) operationKeys.delete(operation);
      // Load every created job even when a newer user request owns navigation.
      await loadDig(result.dig_id);
      if (intent === userIntent && !destroyed) {
        await openRoute({page: 'dig', digId: result.dig_id, stepId: result.step_id, sel: null, view: state.ui.route.view},
          {token: intent, history: 'push', reason: result.created || body.retry_step_id ? 'created' : 'return'});
        commit((draft) => {
          const step = draft.steps.byId[result.step_id];
          if (!result.created && !body.retry_step_id) toast(draft, 'S-T-DUPMOVE', {n: step.number});
          else announce(draft, 'S-AN-STEP-START', {n: step.number, sentence: step.header});
          if (root) draft.ui.search.chips = [];
        });
      }
      acceptedOperations.delete(operation);
      return result;
    } catch (error) {
      if (error.status !== 0 && operationKeys.get(operation) === key) operationKeys.delete(operation);
      const accepted = acceptedOperations.get(operation);
      if (intent === userIntent) {
        if (accepted) actions.showToast(null, {}, {message: error.message, error: true});
        else actions.showToast(root ? 'S-SB-START-ERR' : 'S-T-MOVE-ERR', {reason: error.message});
      }
      return accepted || null;
    }
  }
  function accountPlaying() {
    if (destroyed || lastPlayingAt == null || state.player.state !== 'playing' || !state.player.current) return;
    const now = clock.now(), key = state.player.current.trackKey, stepId = state.player.current.stepId;
    const delta = Math.max(0, now - lastPlayingAt) / 1000; lastPlayingAt = now;
    commit((draft) => { draft.player.heardSeconds[key] = (draft.player.heardSeconds[key] || 0) + delta; }, ['player.heardSeconds']);
    if (state.player.heardSeconds[key] >= 30 && !state.heard.byTrack[key] && !state.heard.pending[key])
      actions.markHeard({trackKey: key, stepId});
  }
  function serializeJudgment(trackKey, action) {
    const previous = judgmentChains.get(trackKey) || Promise.resolve();
    const next = previous.then(action, action);
    judgmentChains.set(trackKey, next);
    next.finally(() => { if (judgmentChains.get(trackKey) === next) judgmentChains.delete(trackKey); });
    return next;
  }
  function acknowledgeJudgment(draft, result) {
    if (result.revision < (draft.judgments.revisions[result.track_key] || 0)) return;
    draft.judgments.byTrack[result.track_key] = copy(result.effective);
    draft.judgments.revisions[result.track_key] = result.revision;
  }
  function legacyJob(jobId, update) {
    follows.get(jobId)?.close?.(); follows.delete(jobId);
    const entry = {slice: false, pollOnly: legacyStreamCount() >= 2, close: null};
    follows.set(jobId, entry);
    // Reserve the legacy slot and close overflow slice streams before opening it.
    syncFollows();
    function finish(value) {
      if (destroyed || follows.get(jobId) !== entry) return;
      entry.close?.(); follows.delete(jobId);
      commit((draft) => update(draft, {job_id: jobId, ...value}));
      syncFollows();
    }
    const handlers = {progress: (progress) => commit((draft) => update(draft, {job_id: jobId, state: 'running', progress})),
      branch: (branch) => commit((draft) => update(draft, {job_id: jobId, state: 'running', branch})),
      done: (result) => finish({state: 'done', result}), error: (error) => finish({state: 'error', error})};
    if (!entry.pollOnly) { entry.close = api.followLegacyJob(jobId, handlers); return; }
    // The legacy adapter has no poll-only option; keep its snapshot fallback
    // semantics here for jobs that exceed the shared two-connection budget.
    const controller = new AbortController();
    let closed = false, timer = null;
    entry.close = () => { closed = true; clock.clearTimeout(timer); controller.abort(); };
    async function poll(remaining = 600) {
      if (closed) return;
      try {
        const job = await api.request('GET', `/api/jobs/${encode(jobId)}`, undefined, {signal: controller.signal});
        if (closed) return;
        if (job.state === 'done') return handlers.done(job.result || {});
        if (job.state === 'error') return handlers.error(job.error || 'The job failed without a message');
      } catch (error) {
        if (closed) return;
        if (error.status === 404) return handlers.error('The server no longer knows this job (it may have restarted).');
      }
      if (remaining <= 1) return handlers.error('Gave up waiting for the job to finish.');
      timer = clock.setTimeout(() => poll(remaining - 1), 1500);
    }
    poll();
  }

  const actions = {
    async hydrate() {
      const saved = readStorage(sessionStorage, 'session', {});
      commit((draft) => {
        for (const field of ['selectionByStep', 'scrollByStep', 'lastStepByDig']) {
          if (saved[field] && typeof saved[field] === 'object' && !Array.isArray(saved[field])) draft.ui[field] = copy(saved[field]);
        }
        const kept = readStorage(storage, 'kept', {});
        if (kept && typeof kept === 'object' && !Array.isArray(kept)) draft.ui.keptOpenByDig = kept;
        const connect = saved.connect;
        if (connect?.origin && typeof connect.origin === 'object' && !Array.isArray(connect.origin)) {
          const origin = parseRoute(formatRoute({...baseRoute(), ...connect.origin}));
          draft.ui.connect.origin = origin.page === 'connect' ? null : origin;
          draft.ui.connect.query = typeof connect.query === 'string' ? connect.query.slice(0, 200) : '';
        }
      });
      try {
        const [recent, settings, health] = await Promise.all([loadRecent(), request('GET', '/api/settings'), request('GET', '/api/health')]);
        const show = settingsFresh(settings);
        commit((draft) => { if (show) setSettings(draft, settings); draft.ui.setup.health = health; });
        const remembered = readStorage(storage, 'route', null);
        const rememberedRoute = remembered?.page === 'dig' ? parseRoute(formatRoute(remembered)) : baseRoute();
        const fallback = recent.length ? {...baseRoute(), digId: recent[0].id, stepId: recent[0].root_step_id} : baseRoute();
        const {target: lastDigRoute} = await resolveRoute(rememberedRoute.digId ? rememberedRoute : fallback);
        commit((draft) => { draft.ui.lastDigRoute = lastDigRoute; });
        let route = parseRoute(location?.hash);
        const redirect = !location?.hash || location.hash === '#map' || location.hash === '#playlists';
        if (!location?.hash || location.hash === '#map') {
          route = lastDigRoute;
        }
        await openRoute(route, {history: redirect ? 'replace' : 'none', reason: 'return',
          origin: route.page === 'connect' && state.ui.connect.origin ? state.ui.connect.origin : undefined});
        const storedPlayer = saved.player;
        if (storedPlayer?.queue?.stepId) {
          try {
            await loadStep(storedPlayer.queue.stepId);
            const source = state.steps.byId[storedPlayer.queue.stepId];
            const queue = makeQueue(state, source.id, Math.max(1, storedPlayer.queue.startRank || 1));
            const currentId = storedPlayer.current?.pickId;
            if (storedPlayer.queue.single && state.picks.byId[currentId]?.step_id === source.id) {
              queue.pickIds = [currentId]; queue.single = true;
            }
            queue.index = queue.pickIds.indexOf(currentId);
            if (queue.index >= 0) commit((draft) => {
              draft.player.queue = queue;
              const refused = storedPlayer.refusedByVideo;
              if (refused && typeof refused === 'object' && !Array.isArray(refused)) draft.player.refusedByVideo = refused;
              draft.player.gesture = false;
              loadPick(draft, draft.picks.byId[currentId], {autoplay: false,
                position: Number.isFinite(storedPlayer.position) ? Math.max(0, storedPlayer.position) : 0});
            }, ['player']);
          } catch (error) { if (![404, 422].includes(error.status)) throw error; }
        }
        if (state.player.current && storedPlayer?.nextQueue?.stepId) {
          try {
            const next = storedPlayer.nextQueue;
            await loadStep(next.stepId);
            const queue = makeQueue(state, next.stepId, Number.isSafeInteger(next.startRank) ? Math.max(1, next.startRank) : 1);
            commit((draft) => { draft.player.nextQueue = queue; }, ['player.nextQueue']);
          } catch (error) { if (![404, 422].includes(error.status)) throw error; }
        }
        hydrated = true;
        commit((draft) => { draft.ui.server = 'up'; });
      } catch (error) {
        commit((draft) => { draft.ui.server = 'down'; });
        throw error;
      }
    },
    openRoute,
    mergeSnapshot(envelope) {
      const {snapshot, live = {}, job} = envelope, stepId = snapshot.step.id;
      const previous = state.steps.byId[stepId], previousJob = state.steps.jobs[previous?.current_job_id];
      if (previousJob && job.id !== previousJob.id && job.attempt <= previousJob.attempt) return false;
      const stale = job.cursor < (state.steps.lastSeq[job.id] || 0);
      commit((draft) => {
        for (const [key, value] of Object.entries(live.judgments || {})) {
          if (value.revision >= (draft.judgments.revisions[key] || 0)) {
            draft.judgments.byTrack[key] = copy(value); draft.judgments.revisions[key] = value.revision;
          }
        }
        for (const [key, value] of Object.entries(live.heard || {})) draft.heard.byTrack[key] ??= copy(value);
        if (stale) return;
        const current = draft.steps.byId[stepId];
        draft.steps.byId[stepId] = {...copy(snapshot.step), current_job_id: job.id, hubs: copy(snapshot.hubs),
          batches: copy(snapshot.batches), diff: copy(snapshot.diff), also_in: copy(live.also_in || {}),
          rerun_steps: copy(live.rerun_steps || [])};
        draft.steps.jobs[job.id] = {...draft.steps.jobs[job.id], ...copy(job), step_id: stepId, last_seq: job.cursor};
        draft.steps.lastSeq[job.id] = job.cursor;
        (draft.steps.byDig[snapshot.step.dig_id] ??= []);
        if (!draft.steps.byDig[snapshot.step.dig_id].includes(stepId)) {
          draft.steps.byDig[snapshot.step.dig_id].push(stepId);
          draft.steps.byDig[snapshot.step.dig_id].sort((a, b) => draft.steps.byId[a].number - draft.steps.byId[b].number);
        }
        // Verify rank identities before replacing the ordered snapshot list.
        snapshot.picks.forEach((pick) => putPick(draft, pick));
        const oldIds = draft.picks.byStep[stepId] || [];
        draft.picks.byStep[stepId] = snapshot.picks.map((pick) => pick.id).sort((a, b) => draft.picks.byId[a].rank - draft.picks.byId[b].rank);
        for (const id of oldIds) if (!draft.picks.byStep[stepId].includes(id)) delete draft.picks.byId[id];
        for (const pick of snapshot.diff?.gone_picks || []) {
          if (!draft.picks.byId[pick.id]) draft.picks.byId[pick.id] = copy(pick);
        }
        reindex(draft);
        if (selectors.isPicksComplete(draft, stepId)) {
          if (current && previousJob && !current.picks_done_at && !terminal(previousJob.state)) completeAnnouncement(draft, job.id, snapshot.step.error);
          else completedJobs.add(job.id);
        }
        extendQueue(draft, stepId);
      }, ['steps', 'picks', 'judgments', 'heard', 'player', 'ui']);
      // cosine.club refused the key this step used: if the store still takes the key for usable
      // (the step may have failed before the page first read it), read the key's state again.
      if (!stale && snapshot.step.error?.code === 'cosine_key_rejected' && selectors.cosineKey(state).usable)
        recheckKey('rejected');
      // A dig seeded from a pasted link takes the name of the track it found (P12). When the
      // snapshot already holds that resolve note, the live note won't come: read the dig again.
      const resolvedLink = (step) => !!step?.note?.some((note) => note.phase === 'resolve') &&
        !!step.origins?.some((origin) => origin.kind === 'link');
      if (!stale && !snapshot.step.parent_id && resolvedLink(snapshot.step) && !resolvedLink(previous))
        request('GET', `/api/digs/${encode(snapshot.step.dig_id)}`).then(mergeDig).catch(() => {});
      return !stale;
    },
    applyJobEvent(event) {
      if (event.event === 'snapshot') return actions.mergeSnapshot(event.envelope);
      const {job_id: jobId, step_id: stepId, seq} = event;
      const job = state.steps.jobs[jobId], step = state.steps.byId[stepId];
      if (!job || (stepId && step?.current_job_id !== jobId) || terminal(job.state) || seq <= (state.steps.lastSeq[jobId] || 0)) return false;
      if (!Number.isSafeInteger(seq) || seq !== (state.steps.lastSeq[jobId] || 0) + 1) throw new Error('Gap in the saved job sequence.');
      commit((draft) => {
        const current = draft.steps.byId[stepId], currentJob = draft.steps.jobs[jobId];
        if (['pick', 'batch'].includes(event.event) && current?.picks_done_at) throw new Error('Picks arrived after picks_done.');
        switch (event.event) {
          case 'pick':
            if (event.pick.step_id !== stepId) throw new Error('Pick belongs to another step.');
            putPick(draft, event.pick); reindex(draft);
            if (current.rerun_of) {
              const old = new Set(selectors.orderedPicks(draft, current.rerun_of).map((pick) => pick.track_key));
              current.diff ??= {new_track_keys: [], gone_picks: []};
              if (!old.has(event.pick.track_key)) current.diff.new_track_keys = unique([...current.diff.new_track_keys, event.pick.track_key]);
            }
            break;
          case 'batch':
            if (!current.batches.some((batch) => batch.hop === event.hop)) current.batches.push({hop: event.hop,
              start_rank: event.start_rank, target_count: event.target_count, label: event.label, count: 0});
            break;
          case 'facts': {
            const pick = selectors.orderedPicks(draft, stepId).find((item) => item.rank === event.rank);
            if (!pick) throw new Error('Facts arrived without their saved pick.');
            if (pick.facts_state === 'loading') { pick.facts_state = event.facts_state; pick.facts = copy(event.facts); }
            break;
          }
          case 'hub': {
            const previousHub = current.hubs.find((hub) => hub.hub_key === event.hub.hub_key);
            if (previousHub) previousHub.pick_ranks = copy(event.hub.pick_ranks);
            else current.hubs.push(copy(event.hub));
            break;
          }
          case 'note': {
            const {event: name, job_id, step_id, seq, at, ...note} = event;
            if (current) current.note.push(copy(note));
            currentJob.progress = {...currentJob.progress,
              work: {...currentJob.progress?.work, ...copy(event.progress || {})}};
            if (event.phase === 'resolve' && stepId) {
              loadStep(stepId).catch(() => {});
              // A dig seeded from a pasted link takes the name of the track the link found (P12).
              if (current && !current.parent_id)
                request('GET', `/api/digs/${encode(current.dig_id)}`).then(mergeDig).catch(() => {});
            }
            break;
          }
          case 'picks_done':
            current.picks_done_at = event.at;
            current.batches = copy(event.batches || current.batches);
            completeAnnouncement(draft, jobId);
            break;
          case 'done': case 'error':
            currentJob.state = event.status === 'stopped' ? 'stopped' : event.event === 'done' ? 'done' : 'error';
            currentJob.result = event.event === 'done' ? copy(event) : null;
            currentJob.error = event.error || null;
            if (current) {
              current.status = event.status; current.finished_at = event.at; current.error = event.error || null;
              if (event.batches) current.batches = copy(event.batches);
              completeAnnouncement(draft, jobId, event.error);
            }
            break;
          default: throw new Error(`Unknown job event: ${event.event}`);
        }
        draft.steps.lastSeq[jobId] = seq; currentJob.last_seq = seq; currentJob.cursor = seq;
        if (current) {
          current.batches.forEach((batch) => { batch.count = selectors.orderedPicks(draft, stepId).filter((pick) => pick.batch === batch.hop).length; });
          extendQueue(draft, stepId);
        } else if (currentJob.kind === 'label_enrichment') draft.ui.enrichment = copy(currentJob);
      }, ['steps', 'picks', 'player', 'ui']);
      // cosine.club refused the key this step used: the server marked it, so Fix key shows everywhere.
      if (event.event === 'error' && event.error?.code === 'cosine_key_rejected') recheckKey('rejected', event.key_revision);
      return true;
    },
    selectPoint(stepId, point, {scroll = true} = {}) {
      // Callers may pass the saved origin record itself as well as a scene point.
      const origin = state.steps.byId[stepId]?.origins?.includes(point);
      const selected = pointInStep(state, stepId, typeof point === 'string' ? point :
        selectionId(origin ? originPoint(point, stepId) : point));
      commit((draft) => {
        draft.ui.selectionByStep[stepId] = selectionId(selected);
        if (draft.ui.route.stepId === stepId) {
          draft.ui.route.sel = selectionId(selected); draft.ui.lastDigRoute = copy(draft.ui.route);
        }
        if (scroll && selected?.source_rank) draft.ui.scrollTarget = {stepId, rank: selected.source_rank, serial: ++announcementNumber};
      }, ['ui.selectionByStep', 'ui.route', 'ui.lastDigRoute', 'ui.scrollTarget']);
      if (state.ui.route.stepId === stepId) historyRoute(state.ui.route, 'replace');
    },
    hoverPoint(stepId, point) { commit((draft) => { draft.ui.hover = point ? {stepId, point: copy(point)} : null; }, ['ui.hover']); },
    focusRegion(name) { commit((draft) => { draft.ui.focusRegion = name; }, ['ui.focusRegion']); },
    saveScroll(stepId, y) { commit((draft) => { draft.ui.scrollByStep[stepId] = Number.isFinite(y) ? Math.max(0, y) : 0; }, ['ui.scrollByStep']); },
    setReducedMotion(value) { commit((draft) => { draft.ui.reducedMotion = !!value; }, ['ui.reducedMotion']); },
    setView(view) {
      if (!state.ui.route.stepId || !['list', 'map'].includes(view)) return;
      commit((draft) => { draft.ui.route.view = view; draft.ui.lastDigRoute = copy(draft.ui.route); }, ['ui.route', 'ui.lastDigRoute']);
      historyRoute(state.ui.route, 'replace');
    },
    goParent() {
      const step = selectors.viewedStep(state);
      if (!step?.parent_id) return actions.showToast('S-T-ROOT');
      return openRoute({page: 'dig', digId: step.dig_id, stepId: step.parent_id, view: state.ui.route.view});
    },
    goPlaying() {
      const current = state.player.current;
      if (!current) return actions.showToast('S-T-NOTHINGPLAYING');
      return openRoute({page: 'dig', digId: current.digId, stepId: current.stepId,
        sel: `track:${current.trackKey}`, view: state.ui.route.view});
    },
    openNewDig({chips} = {}) {
      if (chips) commit((draft) => { draft.ui.search.chips = copy(chips.slice(0, 10)); });
      return openRoute(baseRoute());
    },
    searchSeeds(query, {auto = false, recheck = false} = {}) {
      clock.clearTimeout(searchTimer);
      clock.clearTimeout(rerunTimer); rerunTimer = null;
      searchResolve?.(); searchResolve = null;
      if (destroyed) return Promise.resolve();
      const generation = state.ui.search.generation + 1;
      const trimmed = query.trim(), valid = trimmed.length >= 2;
      commit((draft) => { draft.ui.search = {...draft.ui.search, query, generation, library: [], cosine: [],
        pending: {library: valid, cosine: valid}, highlight: -1, highlightMoved: false, resultsOpen: valid, hint: null, note: null}; });
      if (!valid) return Promise.resolve();
      return new Promise((resolve) => {
        searchResolve = resolve;
        searchTimer = clock.setTimeout(async () => {
          await Promise.all(['library', 'cosine'].map(async (source) => {
            try {
              const reply = await request('GET', `/api/search?q=${encode(trimmed)}&source=${source}`);
              if (state.ui.search.generation !== generation) return;
              commit((draft) => {
                const highlightedId = selectors.seedResults(draft)[draft.ui.search.highlight]?.point_id;
                draft.ui.search[source] = copy(reply.items); draft.ui.search.pending[source] = false;
                for (const point of draft.ui.search.library) {
                  if (point.indexed === true) continue;
                  const match = draft.ui.search.cosine.find((item) => item.track_key === point.track_key);
                  if (!match) continue;
                  point.indexed = true; point.cosine_id = match.cosine_id;
                  if (!point.video_id) point.video_id = match.video_id;
                }
                if (source === 'cosine') {
                  // One note per cause (missing, rejected, busy, server_error, unreachable). A wait
                  // counts down on the store's clock; the seed box shows it and runs the query again.
                  const note = reply.notes[0];
                  draft.ui.search.note = note ? {...copy(note), auto,
                    until: Number.isFinite(note.params?.s) ? clock.now() + note.params.s * 1000 : null} : null;
                }
                const results = selectors.seedResults(draft);
                const chosen = draft.ui.search.highlightMoved ? results.findIndex((point) => point.point_id === highlightedId) : -1;
                draft.ui.search.highlight = chosen >= 0 ? chosen : results.findIndex((point) =>
                  !draft.ui.search.chips.some((chip) => chip.point_id === point.point_id));
                endEnterWait(draft);
              });
              if (source === 'cosine') {
                const note = state.ui.search.note;
                if (note?.until != null && !auto) scheduleRerun(generation, query, note.until);
                // The server marks the key from this answer; refresh what Start a dig knows when they disagree.
                const refused = ['missing', 'rejected'].includes(note?.state);
                if (refused === selectors.cosineKey(state).usable)
                  recheckKey(refused ? note.state : null, note?.key_revision).then(() => {
                    // Refused, yet the key in use is usable: the server marks only the key in use, so the
                    // refusal was about a key saved over meanwhile (another tab). Search once more with it.
                    const search = state.ui.search;
                    if (!destroyed && !recheck && search.generation === generation && ['missing', 'rejected'].includes(search.note?.state) &&
                        selectors.cosineKey(state).usable) actions.searchSeeds(query, {recheck: true});
                  });
              }
            } catch (error) {
              if (state.ui.search.generation !== generation) return;
              commit((draft) => { draft.ui.search.pending[source] = false;
                // Digger's own failure, not cosine.club's: a lost server shows the banner instead.
                if (source === 'cosine' && error.status !== 0)
                  draft.ui.search.note = {state: 'digger', code: 'S-SB-DIGGER-ERR', params: {reason: error.message}};
                endEnterWait(draft); });
            }
          }));
          if (searchResolve === resolve) searchResolve = null;
          resolve();
        }, 250);
      });
    },
    addChip(point) {
      if (state.ui.search.chips.some((chip) => chip.point_id === point.point_id)) {
        commit((draft) => { draft.ui.search.hint = {id: 'S-SB-DUP', params: {}}; }); return;
      }
      if (state.ui.search.chips.length >= 10) {
        commit((draft) => { draft.ui.search.hint = {id: 'S-SB-MAX', params: {}}; }); return;
      }
      commit((draft) => { draft.ui.search.chips.push(copy(requestPoint(point))); });
      return actions.searchSeeds('');
    },
    removeChip(pointId) { commit((draft) => {
      draft.ui.search.chips = draft.ui.search.chips.filter((point) => point.point_id !== pointId);
      if (!draft.ui.search.chips.length) draft.ui.search.blocked = null;
    }); },
    startDig() {
      if (rootPending) return rootPending;
      if (!state.ui.search.chips.length) return Promise.resolve(null);
      const key = selectors.cosineKey(state);
      if (key.known && !key.usable) {
        // P11: no dig starts without a key cosine.club can take; the seed box offers Add key or Fix key.
        commit((draft) => { draft.ui.search.blocked = key.set ? 'rejected' : 'missing'; });
        return Promise.resolve(null);
      }
      const chips = copy(state.ui.search.chips);
      commit((draft) => { draft.ui.search.blocked = null; draft.ui.search.submitting = true; });
      rootPending = submit('/api/digs', {seeds: chips}, {root: true}).finally(() => {
        rootPending = null; commit((draft) => { draft.ui.search.submitting = false; });
      });
      return rootPending;
    },
    highlightSeed(pointId) {
      const search = state.ui.search;
      const index = selectors.seedResults(state).findIndex((point) => point.point_id === pointId);
      if (index < 0 || search.chips.some((chip) => chip.point_id === pointId)) return;
      if (search.highlightMoved && search.highlight === index) return;
      commit((draft) => { draft.ui.search.highlight = index; draft.ui.search.highlightMoved = true; });
    },
    seedInputKey(key) {
      commit((draft) => { draft.ui.search.hint = null; });
      const search = state.ui.search, results = selectors.seedResults(state);
      if (key === 'Enter') {
        const link = seedLink(search.query.trim());
        if (link) return actions.addChip(link);
        if (search.query.trim()) {
          const point = results[search.highlight] || (results.length === 1 ? results[0] : null);
          if (point) return actions.addChip(point);
          // Only while a search runs; after it found nothing or failed, its note and button stay as they are.
          if (search.pending.library || search.pending.cosine)
            commit((draft) => { draft.ui.search.hint = {id: 'S-SB-ENTER-WAIT', params: {}}; });
        } else if (search.chips.length) return actions.startDig();
        else commit((draft) => { draft.ui.search.hint = {id: 'S-SB-EMPTY-ENTER', params: {}}; });
      } else if (['ArrowUp', 'ArrowDown'].includes(key)) {
        const enabled = results.map((point, index) => ({point, index})).filter(({point}) => !search.chips.some((chip) => chip.point_id === point.point_id));
        const index = enabled.findIndex((item) => item.index === search.highlight);
        const next = enabled[Math.max(0, Math.min(enabled.length - 1, index < 0 ? 0 : index + (key === 'ArrowDown' ? 1 : -1)))];
        commit((draft) => { draft.ui.search.highlight = next?.index ?? -1; draft.ui.search.highlightMoved = !!next; });
      } else if (key === 'Backspace' && !search.query && search.chips.length) {
        const point = search.chips.at(-1); actions.removeChip(point.point_id);
        commit((draft) => { announce(draft, 'S-SB-REMOVED', {'chip text': point.name}); });
      } else if (key === 'Escape') {
        if (search.resultsOpen) commit((draft) => { draft.ui.search.resultsOpen = false; });
        else if (search.query) return actions.searchSeeds('');
        else actions.focusRegion('page');
      }
    },
    selectAdjacent(delta) {
      const stepId = state.ui.route.stepId, picks = selectors.orderedPicks(state, stepId);
      if (!picks.length) return;
      const point = selectors.selectedPoint(state, stepId), index = picks.findIndex((pick) => pick.rank === point?.source_rank && pick.step_id === point?.source_step_id);
      const next = picks[index < 0 ? 0 : Math.max(0, Math.min(picks.length - 1, index + delta))];
      actions.selectPoint(stepId, pickPoint(next));
    },
    setTrailRail(expanded) { commit((draft) => { draft.ui.trailRail = !!expanded; }); },
    toggleBranch(stepId) { commit((draft) => { draft.ui.expandedBranches = draft.ui.expandedBranches.includes(stepId) ?
      draft.ui.expandedBranches.filter((id) => id !== stepId) : [...draft.ui.expandedBranches, stepId]; }); },
    toggleKeptList(digId) { commit((draft) => { draft.ui.keptOpenByDig[digId] = !draft.ui.keptOpenByDig[digId]; }); },
    setSwitcher(open) { commit((draft) => { draft.ui.switcher = !!open; }); },
    openMoveMenu(point, {sourceStepId = state.ui.route.stepId, anchor = null, further = false, openerId = null} = {}) {
      const choices = selectors.moveChoices(state, point, {sourceStepId, further});
      if (!choices.length) return actions.showToast('S-T-NOSEL');
      commit((draft) => { draft.ui.menu = {level: 'menu', sourceStepId, point: copy(point), anchor: copy(anchor),
        choices: copy(choices), highlight: choices.findIndex((choice) => !choice.disabled_reason), further, openerId}; });
    },
    chooseMove(move, {further = false, point} = {}) {
      const menu = state.ui.menu;
      const sourceStepId = menu?.sourceStepId || point?.source_step_id || state.ui.route.stepId;
      const source = point || menu?.point || selectors.selectedPoint(state, sourceStepId);
      if (!source) return actions.showToast('S-T-NOSEL');
      const capturedFurther = further || menu?.further || false;
      const choice = menu?.choices.find((item) => item.move === move) ||
        selectors.moveChoices(state, source, {sourceStepId, further: capturedFurther}).find((item) => item.move === move);
      if (!choice) return actions.showToast(source.kind === 'label' ? 'S-T-WRONGHUB-LABEL' : 'S-T-WRONGHUB-PERSON');
      if (choice.disabled_reason) {
        const reason = choice.disabled_reason === 'S-MENU-NOTOKEN' ? `S-T-NOTOKEN-${{label: 'L', artist: 'A', person: 'C'}[move]}` :
          choice.disabled_reason === 'S-MENU-LOADING' ? move === 'person' ? 'S-T-FACTS-LOADING-C' : 'S-T-FACTS-LOADING' :
          choice.disabled_reason === 'S-MENU-NOLABEL' ? 'S-T-NOLABEL' : choice.disabled_reason === 'S-MENU-NONE' ? 'S-T-NOPERSON' : choice.disabled_reason;
        return actions.showToast(reason);
      }
      if (choice.picker_points.length > 1 && point?.kind !== move) {
        if (!menu) actions.openMoveMenu(source, {sourceStepId, further: capturedFurther});
        return actions.openPicker(move, {further: capturedFurther});
      }
      const origin = point && point.kind === move ? point : choice.point;
      if (!origin) return;
      const parentStepId = (menu?.point || source).is_origin ? origin.source_step_id || sourceStepId : sourceStepId;
      commit((draft) => { draft.ui.menu = null; });
      return actions.startMove({parentStepId, origins: [origin], move, further: capturedFurther});
    },
    menuHighlight(delta) {
      if (!state.ui.menu) return;
      commit((draft) => {
        const menu = draft.ui.menu, enabled = menu.choices.map((choice, index) => ({choice, index})).filter(({choice}) => !choice.disabled_reason);
        const current = enabled.findIndex((item) => item.index === menu.highlight);
        menu.highlight = enabled[Math.max(0, Math.min(enabled.length - 1, current < 0 ? 0 : current + delta))]?.index ?? -1;
      });
    },
    openPicker(move, {further = false} = {}) {
      const menu = state.ui.menu, choice = menu?.choices.find((item) => item.move === move);
      if (!choice?.picker_points.length || choice.disabled_reason) return;
      commit((draft) => {
        draft.ui.menu.level = 'picker'; draft.ui.menu.further ||= further;
        draft.ui.menu.parentChoices = copy(menu.choices);
        draft.ui.menu.choices = choice.picker_points.map((point) => ({...copy(choice), id: point.point_id, point: copy(point), picker_points: []}));
        draft.ui.menu.highlight = 0;
      });
    },
    menuEnter({further = false} = {}) {
      const menu = state.ui.menu, choice = menu?.choices[menu.highlight];
      if (!choice || choice.disabled_reason) return;
      if (choice.picker_points.length > 1) return actions.openPicker(choice.move, {further});
      return actions.chooseMove(choice.move, {further, point: choice.point});
    },
    menuBack() {
      commit((draft) => {
        if (draft.ui.menu?.level === 'picker') {
          const move = draft.ui.menu.choices[draft.ui.menu.highlight]?.move;
          draft.ui.menu.level = 'menu'; draft.ui.menu.choices = draft.ui.menu.parentChoices;
          delete draft.ui.menu.parentChoices;
          draft.ui.menu.highlight = draft.ui.menu.choices.findIndex((choice) => choice.move === move);
        } else draft.ui.menu = null;
      });
    },
    startMove({parentStepId, origins, move, further = false, scope, requested_count, rerun_of, retry_step_id}) {
      const body = {parent_step_id: parentStepId, origins: origins.map(requestPoint), move, further};
      if (move !== 'these' && !rerun_of && !retry_step_id) body.source_selection_id = selectionId(origins[0]);
      for (const [key, value] of Object.entries({scope, requested_count, rerun_of, retry_step_id})) if (value != null) body[key] = value;
      return submit('/api/steps', body);
    },
    soundsLikeKeeps({scope = 'step'} = {}) {
      const parent = selectors.viewedStep(state);
      if (!parent) return;
      const eligible = scope === 'dig' ? selectors.keepsForDig(state, parent.dig_id) : selectors.keepsForStep(state, parent.id);
      if (!eligible.length) return actions.showToast(scope === 'dig' ? 'S-T-NOKEEPS-DIG' : 'S-T-NOKEEPS-STEP');
      return actions.startMove({parentStepId: parent.id, origins: eligible.slice(0, 10).map(pickPoint), move: 'these',
        further: false, scope, requested_count: eligible.length});
    },
    rerun(stepId) {
      const step = state.steps.byId[stepId];
      if (!step || !terminal(step.status) || retryBlocked(step)) return;
      return actions.startMove({parentStepId: step.parent_id, origins: step.origins, move: step.move,
        further: step.further, rerun_of: stepId, scope: step.context?.these_scope, requested_count: step.context?.requested_count});
    },
    retry(stepId) {
      const step = state.steps.byId[stepId];
      if (!step || !['failed', 'stopped'].includes(step.status) || selectors.orderedPicks(state, stepId).length || retryBlocked(step)) return;
      return actions.startMove({parentStepId: step.parent_id, origins: step.origins, move: step.move, further: step.further,
        retry_step_id: stepId, scope: step.context?.these_scope, requested_count: step.context?.requested_count});
    },
    judge({trackKey, stepId, verdict, mode = 'key'}) {
      if (!mutable()) return Promise.resolve(null);
      const before = effective(state, trackKey);
      if (mode === 'key' && before.verdict === verdict) { actions.showToast(verdict === 'keep' ? 'S-T-ALREADY-KEPT' : 'S-T-ALREADY-PASSED'); return Promise.resolve(null); }
      const desired = mode === 'toggle' && before.verdict === verdict ? null : verdict;
      const serial = ++intentNumber;
      commit((draft) => { draft.judgments.pending[trackKey] = {id: serial, verdict: desired, stepId}; }, ['judgments.pending']);
      if (mode === 'key' && verdict === 'pass' && state.player.state !== 'idle' && state.player.current?.trackKey === trackKey) actions.next();
      return serializeJudgment(trackKey, async () => {
        const previous = state.judgments.byTrack[trackKey] || {verdict: null};
        try {
          const result = await request(desired ? 'PUT' : 'DELETE', `/api/judgments/${encode(trackKey)}`,
            {step_id: stepId, expected_revision: state.judgments.revisions[trackKey] || 0, ...(desired ? {verdict: desired} : {})});
          commit((draft) => {
            acknowledgeJudgment(draft, result);
            if (draft.judgments.pending[trackKey]?.id === serial) delete draft.judgments.pending[trackKey];
            if (result.change_id) draft.judgments.undo.push({trackKey, stepId, changeId: result.change_id, before: copy(previous), verdict: desired});
            const pick = Object.values(draft.picks.byId).find((item) => item.track_key === trackKey);
            if (desired) toast(draft, desired === 'keep' ? previous.verdict === 'pass' ? 'S-T-KEPT-WAS' : 'S-T-KEPT' : 'S-T-PASSED',
              {'Artist - Title': pick ? nameOf(pick) : trackKey}, {undo: true});
          }, ['judgments', 'ui']);
          return result;
        } catch (error) {
          if (error.status === 409) await loadStep(stepId).catch(() => {});
          commit((draft) => {
            if (draft.judgments.pending[trackKey]?.id === serial) delete draft.judgments.pending[trackKey];
            toast(draft, error.status === 409 ? null : 'S-T-JUDGE-ERR', {reason: error.message, 'keep | pass': verdict},
              error.status === 409 ? {message: 'This judgment changed elsewhere; try again.'} : {});
          }, ['judgments.pending', 'ui']);
          return null;
        }
      });
    },
    undoJudgment() {
      if (!mutable()) return Promise.resolve(null);
      undoPending = undoPending.then(async () => {
        const change = state.judgments.undo.at(-1);
        if (!change) return actions.showToast('S-T-NOUNDO');
        return serializeJudgment(change.trackKey, async () => {
          try {
            const result = await request('DELETE', `/api/judgments/${encode(change.trackKey)}`, {step_id: change.stepId,
              expected_revision: state.judgments.revisions[change.trackKey] || 0, undo_change_id: change.changeId});
            commit((draft) => {
              acknowledgeJudgment(draft, result);
              draft.judgments.undo = draft.judgments.undo.filter((entry) => entry.changeId !== change.changeId);
              const pick = Object.values(draft.picks.byId).find((item) => item.track_key === change.trackKey);
              toast(draft, change.verdict === 'pass' ? 'S-T-UNDO-PASS' : 'S-T-UNDO-KEEP', {'Artist - Title': pick ? nameOf(pick) : change.trackKey});
            }, ['judgments', 'ui']);
            return result;
          } catch (error) {
            if (error.status === 409) await loadStep(change.stepId).catch(() => {});
            actions.showToast(null, {}, {message: error.message}); return null;
          }
        });
      });
      return undoPending;
    },
    async markHeard({trackKey, stepId}) {
      if (destroyed || state.heard.byTrack[trackKey] || state.heard.pending[trackKey]?.sending) return;
      commit((draft) => { draft.heard.pending[trackKey] = {stepId, sending: state.ui.server === 'up'}; }, ['heard.pending']);
      if (state.ui.server !== 'up') return;
      try {
        const result = await request('POST', '/api/heard', {track_key: trackKey, step_id: stepId});
        commit((draft) => { draft.heard.byTrack[trackKey] = result; delete draft.heard.pending[trackKey]; }, ['heard']);
      } catch { commit((draft) => { if (draft.heard.pending[trackKey]) draft.heard.pending[trackKey].sending = false; }, ['heard.pending']); }
    },
    playPick({pickId}) {
      const pick = state.picks.byId[pickId];
      if (!pick) return actions.showToast('S-T-SPACE-NOSEL');
      if (state.player.current?.pickId === pickId && state.player.state !== 'idle') return actions.togglePlayback();
      accountPlaying();
      commit((draft) => {
        const inViewedStep = (draft.picks.byStep[draft.ui.route.stepId] || []).includes(pickId);
        const gone = draft.steps.byId[draft.ui.route.stepId]?.diff?.gone_picks?.some((item) => item.id === pickId);
        draft.player.queue = makeQueue(draft, pick.step_id, pick.rank);
        if (!inViewedStep && gone) { draft.player.queue.pickIds = [pickId]; draft.player.queue.single = true; }
        draft.player.queue.index = 0; draft.player.nextQueue = null; draft.player.gesture = true;
        loadPick(draft, pick);
      }, ['player']);
      if (!pick.video_id) actions.playerEvent({generation: state.player.generation, type: 'error', code: null});
    },
    playStep(stepId, {afterCurrent = false} = {}) {
      if (!state.steps.byId[stepId]) return;
      if (!afterCurrent) accountPlaying();
      commit((draft) => {
        const queue = makeQueue(draft, stepId);
        if (afterCurrent && draft.player.current && draft.player.state !== 'idle') draft.player.nextQueue = queue;
        else {
          draft.player.queue = queue; draft.player.nextQueue = null; draft.player.gesture = true;
          advance(draft);
        }
      }, ['player']);
    },
    togglePlayback() {
      if (!state.player.current || state.player.state === 'idle') {
        const target = selectors.judgingTarget(state);
        return target ? actions.playPick({pickId: target.pickId}) : actions.showToast('S-T-SPACE-NOSEL');
      }
      if (state.player.state === 'refused' && !state.player.current.videoId) return;
      accountPlaying();
      commit((draft) => {
        const pause = draft.player.state === 'playing';
        draft.player.state = pause ? 'paused' : 'loading'; draft.player.phase = pause ? 'cued' : 'buffering';
        draft.player.gesture = true;
        playerIntent(draft, pause ? 'pause' : 'play');
      }, ['player']);
      lastPlayingAt = null;
    },
    previous() {
      if (!state.player.current) return;
      if (state.player.position > 3) return actions.seek(-state.player.position);
      accountPlaying();
      commit((draft) => {
        const queue = draft.player.queue;
        let index = queue.index - 1;
        while (index >= 0 && !selectors.queuePlayable(draft, queue.pickIds[index])) index--;
        if (index < 0) {
          draft.player.position = 0;
          playerIntent(draft, 'seek', {position: 0});
          return;
        }
        queue.index = index;
        loadPick(draft, draft.picks.byId[queue.pickIds[index]]);
      }, ['player']);
    },
    next() {
      if (!state.player.queue.stepId) return;
      accountPlaying(); commit(advance, ['player']);
    },
    seek(delta) {
      if (!state.player.current) return;
      commit((draft) => {
        draft.player.position = Math.max(0, Math.min(draft.player.duration, draft.player.position + delta));
        playerIntent(draft, 'seek', {position: draft.player.position});
      }, ['player.position', 'player.intent']);
    },
    playerEvent(event) {
      if (event.generation !== state.player.generation) return false;
      accountPlaying();
      commit((draft) => {
        if (event.position != null) draft.player.position = Math.max(0, event.position);
        if (event.duration != null) draft.player.duration = Math.max(0, event.duration);
        switch (event.type) {
          case 'playing':
            draft.player.state = 'playing'; draft.player.phase = 'cued'; draft.player.gesture = true;
            lastPlayingAt = clock.now(); break;
          case 'paused': case 'cued': draft.player.state = 'paused'; draft.player.phase = 'cued'; lastPlayingAt = null; break;
          case 'buffering': draft.player.state = 'loading'; draft.player.phase = 'buffering'; lastPlayingAt = null; break;
          case 'ended': advance(draft); break;
          case 'offline': draft.player.state = 'offline'; lastPlayingAt = null; break;
          case 'gesture': draft.player.state = 'paused'; draft.player.phase = 'gesture'; lastPlayingAt = null; break;
          case 'error':
            draft.player.state = 'refused'; draft.player.phase = [101, 150].includes(event.code) ? 'cued' : 'unavailable'; lastPlayingAt = null;
            if (draft.player.current?.videoId) draft.player.refusedByVideo[draft.player.current.videoId] = {code: event.code};
            clock.clearTimeout(refusalTimer);
            refusalTimer = clock.setTimeout(() => {
              if (event.generation === state.player.generation && state.player.state === 'refused') actions.next();
            }, 2000);
            break;
          case 'time': break;
          default: throw new Error(`Unknown player event: ${event.type}`);
        }
      }, ['player']);
      return true;
    },
    showToast(id, params = {}, extra = {}) { commit((draft) => { toast(draft, id, params, extra); }); },
    closeOverlay() {
      if (state.ui.menu?.level === 'picker') return actions.menuBack();
      if (state.ui.menu) { commit((draft) => { draft.ui.menu = null; }); return; }
      if (state.ui.switcher) { actions.setSwitcher(false); return; }
      if (state.ui.sheet) { actions.toggleSheet(); return; }
      if (state.ui.trailRail) { actions.setTrailRail(false); return; }
      if (state.ui.route.stepId) actions.selectPoint(state.ui.route.stepId, null);
    },
    toggleSheet() { commit((draft) => { draft.ui.sheet = !draft.ui.sheet; }); },
    async setServer(status) {
      const value = status === true || ['up', 'ok', 'connected'].includes(status) ? 'up' : status === 'connecting' ? 'connecting' : 'down';
      if (value !== 'up' || state.ui.server === 'up') { commit((draft) => { draft.ui.server = value; }); return; }
      if (reconnecting) return reconnecting;
      reconnecting = (async () => {
        try {
          // Settings too: Digger may be back with another key (stop, change the variable, start).
          const [health, settings] = await Promise.all([request('GET', '/api/health'), request('GET', '/api/settings')]);
          await Promise.all(Object.keys(state.steps.byId).map(loadStep));
          const show = settingsFresh(settings);
          commit((draft) => { draft.ui.server = 'up'; draft.ui.setup.health = health; if (show) setSettings(draft, settings);
            toast(draft, 'S-T-RECONNECTED'); });
          await Promise.all(Object.entries(state.heard.pending).map(([trackKey, pending]) => actions.markHeard({trackKey, stepId: pending.stepId})));
        } catch { commit((draft) => { draft.ui.server = 'down'; }); }
        finally { reconnecting = null; }
      })();
      return reconnecting;
    },
    async loadFeeds() {
      const feeds = await request('GET', '/api/playlists');
      commit((draft) => { for (const [kind, feed] of Object.entries(feeds)) draft.ui.feeds[kind] = {...draft.ui.feeds[kind], ...copy(feed)}; });
      return feeds;
    },
    async runFeed(kind, {dryRun = false} = {}) {
      if (!mutable() || !['daily', 'weekly'].includes(kind)) return;
      const result = await request('POST', `/api/playlists/${kind}/run`, {dry_run: dryRun});
      commit((draft) => { draft.ui.feeds[kind] = {...draft.ui.feeds[kind], job_id: result.job_id, state: 'running'}; });
      legacyJob(result.job_id, (draft, update) => { draft.ui.feeds[kind] = {...draft.ui.feeds[kind], ...copy(update)}; });
      return result;
    },
    /** Send the dig to its own private YouTube Music playlist, to listen on in another tab. */
    async exportDig(digId) {
      if (!mutable() || !state.digs.byId[digId] || state.ui.digExports[digId]?.state === 'running') return;
      commit((draft) => { draft.ui.digExports[digId] = {state: 'running'}; }, ['ui.digExports']);
      let result;
      try {
        result = await request('POST', `/api/digs/${encode(digId)}/export`, {});
      } catch (error) {
        commit((draft) => { draft.ui.digExports[digId] = {state: 'error', error: error.message}; }, ['ui.digExports']);
        throw error;
      }
      commit((draft) => { draft.ui.digExports[digId] = {job_id: result.job_id, state: 'running'}; }, ['ui.digExports']);
      legacyJob(result.job_id, (draft, update) => {
        draft.ui.digExports[digId] = {...draft.ui.digExports[digId], ...copy(update)};
        const dig = draft.digs.byId[digId], params = {digName: dig?.name || ''};
        if (update.state === 'done') {
          // legacyJob commits copy only ui; the dig (frozen) is replaced, not edited.
          if (dig) draft.digs = {...draft.digs, byId: {...draft.digs.byId, [digId]: {...dig,
            youtube_playlist_url: update.result?.playlist_url || null, youtube_sent_at: update.result?.sent_at || null}}};
          toast(draft, 'S-T-YT-SENT', {...params, n: update.result?.added ?? 0});
        } else if (update.state === 'error') {
          const login = youtubeLoginProblem(update.error);
          toast(draft, login === 'rejected' ? 'S-T-YT-REJECTED' : login ? 'S-T-YT-LOGIN' : 'S-T-YT-ERR',
            {...params, reason: update.error}, {error: true});
        }
      });
      return result;
    },
    /** Forget a dig's last send, e.g. a login error once its fix is under way in Setup. */
    clearDigExport(digId) {
      if (state.ui.digExports[digId]?.state === 'running') return;
      commit((draft) => { delete draft.ui.digExports[digId]; }, ['ui.digExports']);
    },
    async loadSetup() {
      const [settings, health, labels] = await Promise.all([request('GET', '/api/settings'), request('GET', '/api/health'), request('GET', '/api/library/labels')]);
      const show = settingsFresh(settings);
      commit((draft) => { if (show) setSettings(draft, settings); draft.ui.setup.health = health;
        draft.ui.enrichment = {...draft.ui.enrichment, overview: labels}; });
      if (labels.active_job_id) {
        const job = await request('GET', `/api/jobs/${encode(labels.active_job_id)}`);
        commit((draft) => { draft.steps.jobs[job.id] = job; draft.steps.lastSeq[job.id] = job.last_seq;
          draft.ui.enrichment = {...job, overview: labels}; }, ['steps', 'ui']);
        follow(job.id);
      }
    },
    async saveSettings(body) {
      if (!mutable()) return;
      const settings = await request('PUT', '/api/settings', body);
      if (settingsFresh(settings)) commit((draft) => { setSettings(draft, settings); });
      return settings;
    },
    async scanLibrary(path) {
      if (!mutable()) return;
      const result = await request('POST', '/api/library/scan', {path});
      commit((draft) => { draft.ui.setup.scan = {job_id: result.job_id, state: 'running'}; });
      legacyJob(result.job_id, (draft, update) => { draft.ui.setup.scan = {...draft.ui.setup.scan, ...copy(update)}; }); return result;
    },
    async startEnrichment(opts = {}) {
      if (!mutable()) return;
      const result = await request('POST', '/api/library/labels/enrich', opts);
      if (result.skipped) {
        commit((draft) => {
          draft.ui.enrichment = {...copy(result), overview: draft.ui.enrichment?.overview};
          toast(draft, result.note.code, result.note.params || {});
        });
        return result;
      }
      const job = await request('GET', `/api/jobs/${encode(result.job_id)}`);
      commit((draft) => { draft.steps.jobs[job.id] = job; draft.steps.lastSeq[job.id] = job.last_seq;
        draft.ui.enrichment = {...job, overview: draft.ui.enrichment?.overview}; }, ['steps', 'ui']);
      follow(job.id); return result;
    },
    async cancelEnrichment(jobId) {
      if (!mutable()) return;
      return request('POST', `/api/library/labels/jobs/${encode(jobId)}/cancel`, {});
    },
    async refreshDoctor() {
      const asked = ++keyEvents;
      const doctor = await request('GET', '/api/doctor');
      commit((draft) => { draft.ui.setup.doctor = doctor; draft.ui.setup.doctorAsked = asked; });
      // Its cosine.club check is a verdict the server records for the key it sent: read it.
      const check = doctor?.checks?.cosine_token;
      await recheckKey(check?.state === 'rejected' ? 'rejected' : null, check?.key_revision);
      return doctor;
    },
    // From pasted headers, or ({chrome: true}) read from Chrome on this Mac after macOS asks.
    async loginYouTube(headers, {chrome = false} = {}) {
      if (!mutable()) return;
      const result = chrome ? await request('POST', '/api/youtube/login/chrome', {}) :
        await request('POST', '/api/youtube/login', {headers});
      // state: connected | rejected | unreachable | missing (no login in the file) | saved (unchecked);
      // settings: the server's, where a login saved meanwhile (another tab) keeps its own verdict.
      const show = settingsFresh(result.settings);
      commit((draft) => { draft.ui.setup.login = {ok: !!result.ok, logged_in: !!result.logged_in, state: result.state};
        if (show) setSettings(draft, result.settings); });
      return result;
    },
    /** Check a key with its provider; a typed key is saved only when the provider accepts it (A5).
     * Without a key, checks the one in use. Resolves {state, retry_after_s, reason}; HTTP success
     * doesn't mean connected: only state === 'connected' does. */
    async checkConnection(name, key) {
      if (!mutable()) return null;
      const result = await request('POST', `/api/connections/${encode(name)}`, key == null ? {} : {key});
      const show = settingsFresh(result.settings);
      // A verdict on the cosine.club key is newer than any Health check asked before it.
      const verdict = name === 'cosine_token' && ['connected', 'rejected', 'malformed'].includes(result.state) ? ++keyEvents : 0;
      commit((draft) => { if (show) setSettings(draft, result.settings); if (verdict) draft.ui.setup.keyCheckedAt = verdict; });
      return {state: result.state, retry_after_s: result.retry_after_s, reason: result.reason};
    },
    async loadSettings() {
      const settings = await request('GET', '/api/settings');
      if (settingsFresh(settings)) commit((draft) => { setSettings(draft, settings); });
      return settings;
    },
    /** ADD KEY and FIX KEY: the key screen remembers the page it was opened from. */
    openConnect() { return openRoute({page: 'connect'}); },
    /** Check a pasted cosine.club key (or, with none, the key in use); go back once it's accepted. */
    async connectKey(key) {
      const intent = userIntent;
      const result = await actions.checkConnection('cosine_token', key);
      if (result?.state === 'connected') await actions.keyConnected({intent});
      return result;
    },
    async keyConnected({intent = userIntent} = {}) {
      const {origin, query} = state.ui.connect;
      if (intent !== userIntent || state.ui.route.page !== 'connect') {
        // Accepted after the user went elsewhere: the key is saved, and the page they chose stays.
        commit((draft) => { draft.ui.connect = {origin: null, query: '', focus: null};
          draft.ui.search.note = null; draft.ui.search.blocked = null; draft.ui.search.hint = null; });
        actions.showToast('S-KEY-OK');
        return;
      }
      const target = origin?.page && origin.page !== 'connect' ? origin : baseRoute();
      const newDig = target.page === 'dig' && !target.stepId;
      commit((draft) => {
        // Stale key notes go; a failed step comes back with Retry focused.
        draft.ui.connect = {origin: null, query: '', focus: target.stepId ? {stepId: target.stepId, serial: ++announcementNumber} : null};
        draft.ui.search.note = null; draft.ui.search.blocked = null; draft.ui.search.hint = null;
      });
      await openRoute(target, {history: 'replace'});
      actions.showToast('S-KEY-OK');
      if (newDig && query.trim().length >= 2) await actions.searchSeeds(query);
    },
  };
  /** Settings answers can arrive out of order (a read, a key connected, settings saved, a login):
   * each carries the server's settings revision, and one older than what the page shows never
   * shows, whichever request it answers. */
  function settingsFresh(settings) {
    const revision = settings?.revision;
    if (!Number.isSafeInteger(revision) || revision < settingsRevision) return false;
    settingsRevision = revision;
    return true;
  }
  function setSettings(draft, settings) {
    draft.ui.settings = settings;
    draft.ui.keyDoubt = null;  // what the server says now settles any doubt
  }
  /** A cosine.club answer said something about the key: read its state again (the server marked
   * the key the request sent, if it's still in use). When that read fails, a refusal ('missing'
   * or 'rejected') of the key in use stands until a read succeeds, instead of the old usable
   * verdict. `revision`: from which settings revision the server holds the refusal; none means it
   * was about a key replaced meanwhile, or can't be placed, so it raises no doubt. */
  function recheckKey(refusal = null, revision = null) {
    return actions.loadSettings().catch(() => {
      // An answer shown at or after that revision already says what the server holds since; and
      // none older may show over the refusal now.
      if (!refusal || destroyed || !Number.isSafeInteger(revision) || revision <= settingsRevision) return;
      settingsRevision = revision;
      commit((draft) => { draft.ui.keyDoubt = refusal; });
    });
  }
  /** Enter pressed while both sources searched: once both answered, "Still searching" is over. */
  function endEnterWait(draft) {
    const search = draft.ui.search;
    if (!search.pending.library && !search.pending.cosine && search.hint?.id === 'S-SB-ENTER-WAIT') search.hint = null;
  }
  /** When cosine.club's wait ends, run the same search once more, unless the query, the key or
   * the page changed meanwhile (whatever the user did since wins). */
  /** Which cosine.club key the page knows is in use (its hint and source, never the key): enough to
   * skip a wait's rerun after another key was connected, never to tell two keys apart. */
  function keyIdentity() {
    return JSON.stringify(['set', 'hint', 'source'].map((field) => state.ui.settings?.cosine_token?.[field] ?? null));
  }
  function scheduleRerun(generation, query, until) {
    const keyOf = keyIdentity;
    const key = keyOf(), routeGeneration = state.ui.routeGeneration;
    clock.clearTimeout(rerunTimer);
    rerunTimer = clock.setTimeout(() => {
      rerunTimer = null;
      const search = state.ui.search;
      if (destroyed || search.generation !== generation || search.query !== query || keyOf() !== key ||
          state.ui.routeGeneration !== routeGeneration || state.ui.route.page !== 'dig' || state.ui.route.stepId) return;
      actions.searchSeeds(query, {auto: true});
    }, Math.max(0, until - clock.now()));
  }
  function seedLink(query) {
    try {
      const url = new URL(query);
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.port) return null;
      const host = url.hostname.replace(/^www\./, '');
      if (!['cosine.club', 'discogs.com', 'youtube.com', 'music.youtube.com', 'youtu.be'].includes(host)) return null;
      return {point_id: `link:${query}`, kind: 'link', name: query, url: query};
    } catch { return null; }
  }
  function retryBlocked(step) {
    if (!step.error || step.error.code === 'server_restarted') return false;
    if (step.error.retry_at == null && step.error.retry_after_s == null) return false;
    const deadline = step.error.retry_at ? Date.parse(step.error.retry_at) : Date.parse(step.finished_at) + step.error.retry_after_s * 1000;
    return Number.isFinite(deadline) && Date.now() < deadline;
  }
  let lastHash = location?.hash;
  function routeEvent() {
    if (location?.hash === lastHash) return;
    lastHash = location?.hash;
    if (formatRoute(state.ui.route) === location?.hash) {
      ++userIntent;
      commit((draft) => { draft.ui.routeGeneration++; }, ['ui.routeGeneration']);
      return;
    }
    const route = location.hash === '#map' ? state.ui.lastDigRoute || baseRoute() : parseRoute(location.hash);
    openRoute(route, {history: ['#map', '#playlists'].includes(location.hash) ? 'replace' : 'none'}).catch(() => {});
  }
  const events = typeof location?.addEventListener === 'function' ? location : globalThis;
  events.addEventListener?.('hashchange', routeEvent);
  events.addEventListener?.('popstate', routeEvent);
  events.addEventListener?.('pagehide', persist);
  return {getState: () => state, subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); }, actions,
    destroy() {
      if (destroyed) return;
      persist(); destroyed = true; lifetime.abort(); follows.forEach((entry) => entry.close?.()); follows.clear(); listeners.clear();
      searchResolve?.(); searchResolve = null; lastPlayingAt = null;
      clock.clearTimeout(searchTimer); clock.clearTimeout(persistTimer); clock.clearTimeout(refusalTimer); clock.clearTimeout(rerunTimer);
      events.removeEventListener?.('hashchange', routeEvent); events.removeEventListener?.('popstate', routeEvent);
      events.removeEventListener?.('pagehide', persist);
    }};
}
