import {selectors} from './state.js';
import {formatWhen} from './dom.js';

const kinds = [
  {kind: 'daily', title: 'Digger Daily', sizeKey: 'daily_size'},
  {kind: 'weekly', title: 'Digger Weekly', sizeKey: 'weekly_size'},
];
const badges = {ok: ['ok', 'OK'], success: ['ok', 'OK'], error: ['fail', 'Error'],
  failed: ['fail', 'Failed'], running: ['warn', 'Running']};
const dateOf = (value) => {
  if (!value) return null;
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date : null;
};
const timeOf = (date) => String(date.getHours()).padStart(2, '0') + ':' + String(date.getMinutes()).padStart(2, '0');
const nextWhen = (value) => {
  const date = dateOf(value);
  return date ? ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT'][date.getDay()] + ' ' + timeOf(date) : '--- --:--';
};
function relative(value) {
  const date = dateOf(value);
  if (!date) return '';
  const seconds = Math.round((date.getTime() - Date.now()) / 1000), minutes = Math.floor(Math.abs(seconds) / 60);
  const days = Math.floor(minutes / 1440), hours = Math.floor(minutes % 1440 / 60);
  const label = days ? `${days}d ${hours}h` : hours ? `${hours}h ${minutes % 60}m` : `${Math.max(1, minutes)}m`;
  return seconds >= 0 ? `in ${label}` : `${label} ago`;
}
const lastWhen = (value) => (formatWhen(value) || '—') + (dateOf(value) ? ` (${relative(value)})` : '');
// A missing or refused YouTube Music login sends the user to its Setup card, never to a Terminal command (P14).
const loginProblem = (message) => /signed out/i.test(message || '') ? 'rejected' :
  /login missing/i.test(message || '') ? 'missing' : null;
function exportCounts(report) {
  return [['missing', 'missing'], ['failed', 'failed'], ['omitted', 'left out']]
    .filter(([key]) => Array.isArray(report[key]) && report[key].length)
    .map(([key, label]) => `${report[key].length} ${label}`);
}
function outcome(result, dryRun) {
  const report = result.export && typeof result.export === 'object' ? result.export : null;
  if (dryRun) return {tone: 'warn', label: 'Dry run'};
  if (result.export_error) return {tone: 'fail', label: 'Export failed', error: result.export_error};
  if (!result.tracks?.length && !report) return {tone: 'warn', label: 'Nothing written',
    empty: 'No tracks came back, so nothing was written to YouTube Music.'};
  if (!report) return {tone: 'fail', label: 'Not written',
    warning: 'The run returned no export report; writing the playlist could not be confirmed.'};
  if (report.verified === false) return {tone: 'warn', label: 'Not verified',
    warning: `Written, but reading the playlist back didn't confirm every track${exportCounts(report).length ?
      ' (' + exportCounts(report).join(', ') + ')' : ''}. Open it to check.`};
  if (report.verified === true) return {tone: 'ok', label: 'Verified'};
  return {tone: 'warn', label: 'Written', warning: "The export report doesn't say whether the playlist was checked."};
}

/** Cards are persistent render caches; the store/legacy adapter owns every job. */
export function mountFeeds(root, {store, h, text, safeUrl, clock, mounts}) {
  const wrap = root.querySelector('#pl-cards'), error = root.querySelector('#pl-error');
  const lede = root.querySelector('#feeds-lede'), how = root.querySelector('#feeds-how');
  const lastRun = root.querySelector('#feeds-last');
  function suppressRepeatedClick(event) {
    // A fast run can put a different control under the second click. Capture
    // it before button handlers or a newly inserted link's default action.
    if (event.detail > 1) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }
  wrap.addEventListener('click', suppressRepeatedClick, true);
  root.querySelector('#feeds-heading').textContent = text('S-FEEDS-TITLE');
  how.textContent = text('S-FEEDS-HOW-START');
  const localTime = h('div', {id: 'feeds-clock', class: 'lcd__clock'});
  const nextDaily = h('div', {id: 'feeds-next-daily'}), nextWeekly = h('div', {id: 'feeds-next-weekly'});
  const schedule = h('section', {class: 'lcd feeds-clock', 'aria-label': 'Local time and next feed runs'},
    localTime, h('div', {class: 'lcd__readout'}, nextDaily, nextWeekly));
  root.querySelector('.page__head').after(schedule);
  let destroyed = false, refreshing = false;

  function alert(node, message) {
    node.textContent = message || '';
    node.hidden = !message;
  }
  async function refresh() {
    if (destroyed || refreshing) return;
    refreshing = true;
    try {
      await store.actions.loadFeeds();
      if (!destroyed) { alert(error, ''); retryWrap.hidden = true; }
    } catch (err) {
      if (!destroyed) { alert(error, `Couldn't load feeds: ${err.message}`); retryWrap.hidden = false; }
    } finally { refreshing = false; }
  }
  const retry = h('button', {type: 'button', class: 'btn btn--small', 'data-action': 'refresh-feeds',
    onclick: refresh}, 'Retry');
  const retryWrap = h('div', {class: 'feeds-retry', hidden: true}, retry);
  error.after(retryWrap);

  function loginText(message, card) {
    const problem = loginProblem(message);
    if (card) card.loginProblem = problem;
    return problem ? text(problem === 'rejected' ? 'S-SET-YT-REJECTED' : 'S-FEEDS-LOGIN-MISSING') : message;
  }
  function connectYouTube(card) {
    // Opens Setup's YouTube Music card with its headers field ready.
    store.actions.openRoute({page: 'setup'}).then(() => {
      if (mounts.ytForm.hidden) mounts.ytConnect.click();
      else mounts.ytHeaders.focus();
    }).catch((err) => { if (!destroyed) alert(card.error, err.message); });
  }
  function externalLink(url, label, attrs = {}) {
    const href = safeUrl(url);
    return href ? h('a', {class: 'btn btn--yt', href, target: '_blank', rel: 'noopener', ...attrs}, label) : null;
  }
  function trackRow(track, index) {
    const actions = h('div', {class: 'pick__actions'});
    if (track.source) actions.append(h('span', {class: 'src-tag src-tag--' + track.source}, track.source));
    actions.append(externalLink(track.youtube_url, '▶ YT Music', {class: 'btn btn--small btn--yt',
      'aria-label': `Open ${track.artist} - ${track.title} in YouTube Music`}) ||
      h('span', {class: 'pick__noyt', title: 'No YouTube Music match for this track'}, 'No YT match'));
    return h('li', {class: 'pick', 'data-track-id': track.id},
      h('span', {class: 'pick__n', 'aria-hidden': 'true'}, String(index + 1).padStart(2, '0')),
      h('div', {class: 'pick__main'},
        h('p', {class: 'pick__title'}, h('b', {}, track.artist || 'Unknown artist'), ' - ', track.title || 'Untitled'),
        track.why && h('p', {class: 'pick__why'}, track.why),
        (track.hop > 0 || track.label) && h('p', {class: 'pick__meta'},
          [track.hop > 0 && `HOP ${track.hop}`, track.label && `LABEL ${track.label}`].filter(Boolean).join(' · '))), actions);
  }
  function renderResult(card, result) {
    const dryRun = card.el.dataset.dryRun === 'true', status = outcome(result, dryRun);
    const tracks = Array.isArray(result.tracks) ? result.tracks : [], report = result.export;
    const counts = !dryRun && report ? exportCounts(report) : [];
    if (!dryRun && Array.isArray(report?.added)) counts.unshift(
      `${report.added.length}${Array.isArray(report.requested) ? '/' + report.requested.length : ''} tracks in the playlist`);
    card.result.replaceChildren(...[
      h('div', {class: 'plcard__summary'}, h('span', {class: 'badge badge--' + status.tone}, status.label),
        h('span', {}, `${tracks.length} track${tracks.length === 1 ? '' : 's'}${result.date ? ' · ' + result.date : ''}`),
        counts.length ? h('span', {}, counts.join(' · ')) : null,
        !dryRun && externalLink(result.playlist_url || report?.playlist_url, '▶ Open', {class: 'btn btn--small btn--yt'})),
      status.error && h('div', {class: 'alert', role: 'alert'}, loginText(status.error, card)),
      status.warning && h('div', {class: 'alert alert--warn', role: 'status'}, status.warning),
      ...(result.warnings || []).map((warning) => h('div', {class: 'alert alert--warn'}, warning)),
      tracks.length ? h('ol', {class: 'picks plcard__tracks', 'aria-label': `${card.spec.title} tracks`}, tracks.map(trackRow)) :
        h('p', {class: 'empty-note'}, status.empty || 'No tracks came back.')].filter(Boolean));
    card.result.hidden = false;
    return status;
  }
  function logLine(card, message, isError = false) {
    card.log.hidden = false;
    card.log.append(h('li', {class: isError ? 'is-err' : ''}, message));
    card.log.scrollTop = card.log.scrollHeight;
  }
  async function run(card, dryRun) {
    const s = store.getState(), feed = selectors.feedState(s, card.spec.kind);
    if (s.ui.server !== 'up' || card.el.dataset.submitting === 'true' ||
        feed?.state === 'running' || feed?.last_run?.status === 'running') return;
    card.el.dataset.submitting = 'true';
    card.el.dataset.dryRun = String(dryRun);
    card.log.replaceChildren();
    card.progress = null;
    card.result.hidden = true;
    card.loginProblem = null;
    alert(card.error, '');
    logLine(card, dryRun ? 'Dry run: planning without writing to YouTube Music' : 'Run: planning and writing the playlist');
    render(s);
    try {
      await store.actions.runFeed(card.spec.kind, {dryRun});
    } catch (err) {
      if (!destroyed) { const shown = loginText(err.message, card); alert(card.error, shown); logLine(card, shown, true); }
    } finally {
      if (!destroyed) { delete card.el.dataset.submitting; render(store.getState()); }
    }
  }
  function buildCard(spec) {
    const card = {spec, jobId: null, progress: null, terminal: null, linkUrl: null, loginProblem: null};
    card.badge = h('span', {class: 'badge', 'data-slot': 'badge'}, 'Loading…');
    card.size = h('div', {class: 'plcard__size', 'data-slot': 'size'});
    card.next = h('div', {class: 'plcard__next', 'data-slot': 'next'});
    card.when = h('div', {class: 'plcard__when', 'data-slot': 'when'});
    card.time = h('span', {class: 'plcard__time', 'data-slot': 'time'});
    card.detail = h('p', {class: 'plcard__detail', 'data-slot': 'detail'});
    card.link = h('span', {'data-slot': 'link'}, 'No playlist yet');
    card.run = h('button', {class: 'btn btn--go', type: 'button', 'data-action': 'run-feed', 'data-slot': 'run',
      onclick: () => run(card, false)}, 'Run now');
    card.connect = h('button', {class: 'btn btn--go', type: 'button', 'data-action': 'connect-youtube', 'data-slot': 'connect',
      hidden: true, onclick: () => connectYouTube(card)}, text('S-FEEDS-CONNECT-YT'));
    card.dry = h('button', {class: 'btn', type: 'button', 'data-action': 'dry-run-feed', 'data-slot': 'dry',
      onclick: () => run(card, true)}, 'Dry run');
    card.log = h('ol', {class: 'log log--compact', 'data-slot': 'log', 'aria-label': `${spec.title} progress`, hidden: true});
    card.error = h('div', {class: 'alert', role: 'alert', 'data-slot': 'error', hidden: true});
    card.result = h('div', {class: 'plcard__result', 'data-slot': 'result', hidden: true});
    card.el = h('section', {class: 'panel plcard', 'data-feed-kind': spec.kind, 'aria-labelledby': `pl-${spec.kind}-title`},
      h('header', {class: 'panel__bar'}, h('h3', {class: 'panel__title', id: `pl-${spec.kind}-title`}, spec.title), card.badge),
      h('div', {class: 'panel__body'},
        h('div', {class: 'plcard__screen'}, card.size, card.next, card.when),
        h('div', {class: 'plcard__last'}, h('div', {class: 'plcard__last-top'},
          h('span', {class: 'field__label'}, 'Last run'), card.time), card.detail),
        h('div', {class: 'plcard__actions'}, card.link, card.connect, card.run, card.dry), card.log, card.error, card.result));
    return card;
  }
  const cards = kinds.map(buildCard);
  wrap.replaceChildren(...cards.map((card) => card.el));

  function render(s) {
    if (destroyed) return;
    if (s.ui.route.page === 'feeds' && !selectors.feedState(s, 'daily') && error.hidden &&
        (s.ui.server === 'down' || s.ui.toast?.error)) {
      alert(error, s.ui.toast?.message || "Couldn't load feeds. Reconnect to the local server, then retry.");
    }
    const feeds = kinds.map(({kind}) => selectors.feedState(s, kind));
    // Under start nothing inside Digger runs the feeds on a clock, so no next-run time shows (P14).
    const scheduled = feeds.some((feed) => feed?.scheduled === true);
    const latest = feeds.map((feed) => feed?.last_run?.finished_at).filter(dateOf)
      .sort((a, b) => dateOf(b) - dateOf(a))[0];
    lede.textContent = text(scheduled ? 'S-FEEDS-LEDE' : 'S-FEEDS-LEDE-START');
    how.hidden = scheduled;
    lastRun.hidden = scheduled || !feeds.every(Boolean);
    lastRun.textContent = text('S-FEEDS-LAST', {when: latest ? lastWhen(latest) : text('S-FEEDS-LAST-NONE')});
    localTime.textContent = text('S-FEEDS-CLOCK', {'HH:MM': timeOf(new Date())});
    schedule.setAttribute('aria-label', scheduled ? 'Local time and next feed runs' : 'Local time');
    nextDaily.hidden = nextWeekly.hidden = !scheduled;
    nextDaily.textContent = scheduled ? text('S-FEEDS-NEXT-DAILY', {'DDD HH:MM': nextWhen(feeds[0]?.next_run)}) : '';
    nextWeekly.textContent = scheduled ? text('S-FEEDS-NEXT-WEEKLY', {'DDD HH:MM': nextWhen(feeds[1]?.next_run)}) : '';
    retryWrap.hidden = error.hidden;
    // Until a YouTube Music login is saved (or after one is refused), Connect replaces Run now.
    const youtube = s.ui.settings?.youtube;
    const needsLogin = !!youtube && (!youtube.saved || youtube.checked === 'rejected');
    for (const card of cards) {
      const feed = selectors.feedState(s, card.spec.kind), last = feed?.last_run;
      const size = s.ui.settings?.[card.spec.sizeKey] ?? '—';
      if (card.size.dataset.value !== String(size)) {
        card.size.dataset.value = String(size); card.size.replaceChildren(String(size), h('small', {}, 'TRK'));
      }
      card.next.hidden = card.when.hidden = !scheduled;
      card.next.textContent = !scheduled ? '' : dateOf(feed?.next_run) ? 'NEXT ' + nextWhen(feed.next_run) : 'NEXT —';
      card.when.textContent = !scheduled ? '' : dateOf(feed?.next_run) ? relative(feed.next_run).toUpperCase() :
        feed ? 'NOT SCHEDULED' : 'SCHEDULE UNAVAILABLE';
      const [tone, label] = last ? badges[last.status] || ['', last.status || 'unknown'] :
        ['', feed ? 'Never run' : error.hidden ? 'Loading…' : 'Unavailable'];
      card.badge.className = 'badge' + (tone ? ' badge--' + tone : ''); card.badge.textContent = label;
      card.time.textContent = last ? lastWhen(last.finished_at) : feed ? 'never' : '—';
      card.detail.textContent = last?.detail ? loginText(last.detail) :
        feed && !last ? scheduled ? 'No run yet. Run now, or wait for the schedule.' : text('S-FEEDS-NO-RUN') : '';
      const url = safeUrl(feed?.playlist_url);
      if (url !== card.linkUrl) {
        card.linkUrl = url;
        card.link.replaceChildren(externalLink(url, '▶ Open in YT Music') || h('span', {class: 'plcard__time'}, 'No playlist yet'));
      }
      const pending = card.el.dataset.submitting === 'true' || feed?.state === 'running' || last?.status === 'running';
      const disabled = pending || s.ui.server !== 'up';
      for (const button of [card.run, card.dry]) {
        button.disabled = disabled;
        button.title = disabled ? pending ? 'A run is already going; it will finish on its own.' : 'Reconnect to the local server to run a feed.' : '';
      }
      const refused = card.loginProblem || (last?.status === 'error' ? loginProblem(last.detail) : null);
      card.run.hidden = needsLogin;
      card.connect.hidden = !needsLogin && !refused;
      card.connect.className = 'btn' + (needsLogin ? ' btn--go' : '');
      card.el.setAttribute('aria-busy', String(!!pending));
      if (!feed?.job_id) continue;
      if (card.jobId !== feed.job_id) {
        card.jobId = feed.job_id; card.terminal = null; card.progress = feed.progress?.message ?? null;
        card.result.hidden = true; alert(card.error, '');
      }
      const message = feed.progress?.message;
      if (message && message !== card.progress) { card.progress = message; logLine(card, message); }
      if (['done', 'error'].includes(feed.state) && card.terminal !== feed.job_id) {
        card.terminal = feed.job_id;
        if (feed.state === 'error') { const shown = loginText(feed.error, card); alert(card.error, shown); logLine(card, shown, true); }
        else {
          const status = renderResult(card, feed.result || {});
          logLine(card, `${card.spec.title}: ${status.label}, ${feed.result?.tracks?.length || 0} tracks`);
        }
        // The run ledger is separate from the legacy job's result, including failures.
        refresh();
      }
    }
  }
  const unsubscribe = store.subscribe(render);
  render(store.getState());
  const clockTimer = clock.setInterval(() => render(store.getState()), 1000);
  const refreshTimer = clock.setInterval(() => {
    if (store.getState().ui.route.page === 'feeds') refresh();
  }, 15000);
  return {destroy() {
    destroyed = true; unsubscribe(); clock.clearInterval(clockTimer); clock.clearInterval(refreshTimer);
    wrap.removeEventListener('click', suppressRepeatedClick, true);
    schedule.remove(); retryWrap.remove(); wrap.replaceChildren();
  }};
}
