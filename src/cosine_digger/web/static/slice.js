import {createApi} from './api.js';
import {createStore, formatRoute} from './state.js';
import {h, text, safeUrl, clampPopover} from './dom.js';

// Every component is wired here before the parallel component waves begin.
// A missing file is a staged gap; a bad export or mount is an implementation error.
export const registry = Object.freeze([
  ['player', 'mountPlayer', 'player-region'],
  ['seedbox', 'mountSeedbox', 'new-dig-region'],
  ['steplist', 'mountStepList', 'step-region'],
  ['steplist', 'mountSlimList', 'slim-list-region'],
  ['trail', 'mountTrail', 'trail-region'],
  ['inspector', 'mountInspector', 'inspector-region'],
  ['moves', 'mountMoves', 'moves-region'],
  ['stepmap', 'mountStepMap', 'stepmap-region'],
  ['territory', 'mountTerritory', 'territory-region'],
  ['keys', 'mountKeys', 'sheet-region'],
  ['feeds', 'mountFeeds', 'feeds-region'],
  ['setup', 'mountSetup', 'setup-region'],
  ['connect', 'mountConnect', 'connect-region'],
].map(([name, entry, root]) => Object.freeze({
  name, path: '/static/' + name + '.js', stylesheet: '/static/' + name + '.css', entry, root,
  exports: Object.freeze(name === 'stepmap' ? ['mountStepMap', 'drawStep', 'hitStep'] :
    name === 'steplist' ? ['mountStepList', 'mountSlimList'] : [entry]),
})));

const browserClock = Object.freeze({
  now: () => performance.now(),
  setTimeout: (...args) => window.setTimeout(...args),
  clearTimeout: (id) => window.clearTimeout(id),
  setInterval: (...args) => window.setInterval(...args),
  clearInterval: (id) => window.clearInterval(id),
});

let activeBoot = null;
// The lost-server banner keeps today's "reconnecting" line this long, then says how to start Digger again.
const LOST_GRACE_MS = 10000;

/** Boot once per document. Routes toggle mounts; only teardown destroys them. */
export function boot(options = {}) {
  if (!activeBoot) activeBoot = buildShell(options).catch((error) => {
    document.getElementById('device').dataset.shellState = 'error';
    activeBoot = null;
    throw error;
  });
  return activeBoot;
}

async function buildShell({api = createApi(), clock = browserClock, store = createStore({api, clock})} = {}) {
  const elements = [...document.querySelectorAll('[id]')];
  const mounts = Object.freeze(Object.fromEntries(elements.flatMap((element) => [
    [element.id, element],
    [element.id.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase()), element],
  ])));
  const ctx = Object.freeze({store, api, text, h, safeUrl, clampPopover, mounts, clock});
  const disposers = [], components = [], omittedModules = [];
  const device = mounts.device;
  device.dataset.shellState = 'booting';
  let destroyed = false, healthPending = false, hydrated = false, toastSerial = null, announcedSerial = null;
  let toastTimer = null, toastRemaining = 0, toastStarted = 0, toastPaused = false;
  let lostTimer = null, lostLong = false;

  function listen(target, event, handler) {
    target.addEventListener(event, handler);
    disposers.push(() => target.removeEventListener(event, handler));
  }
  function report(error) {
    if (!destroyed) store.actions.showToast(null, {}, {message: error.message, error: true});
  }
  function applyCrt(on) {
    document.body.classList.toggle('no-crt', !on);
    mounts.crtToggle.setAttribute('aria-pressed', String(on));
  }
  try { applyCrt(localStorage.getItem('digger.crt') !== 'off'); } catch { applyCrt(true); }
  listen(mounts.crtToggle, 'click', () => {
    const on = document.body.classList.contains('no-crt');
    applyCrt(on);
    try { localStorage.setItem('digger.crt', on ? 'on' : 'off'); } catch { /* Private mode. */ }
  });
  listen(document.querySelector('.skip-link'), 'click', (event) => {
    event.preventDefault();
    const route = store.getState().ui.route;
    const target = route.page === 'dig' && route.stepId ?
      route.view === 'map' ? mounts.territoryRegion : mounts.stepRegion : mounts.main;
    target.focus();
    target.scrollIntoView({block: 'nearest', behavior: 'auto'});
  });
  for (const page of ['dig', 'feeds', 'setup']) {
    listen(mounts['mode-' + page], 'click', (event) => {
      if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      store.actions.openRoute({page}).catch(report);
    });
  }
  for (const view of ['list', 'map']) {
    listen(mounts['view-' + view], 'click', () => store.actions.setView(view));
  }

  function clearToastTimer() { clock.clearTimeout(toastTimer); toastTimer = null; }
  function scheduleToast() {
    clearToastTimer();
    if (toastPaused || toastRemaining <= 0) return;
    toastStarted = clock.now();
    const serial = toastSerial;
    toastTimer = clock.setTimeout(() => {
      toastTimer = null;
      if (!destroyed && store.getState().ui.toast?.serial === serial) store.actions.showToast(null);
    }, toastRemaining);
  }
  function syncPause() {
    const tray = store.getState().ui.route.page === 'dig' ? mounts.toastRegion : mounts.pageToasts;
    const paused = !tray.hidden && (tray.matches(':hover') || tray.contains(document.activeElement));
    if (paused === toastPaused) return;
    if (paused) {
      toastRemaining = Math.max(0, toastRemaining - (clock.now() - toastStarted));
      clearToastTimer();
    }
    toastPaused = paused;
    if (!paused) scheduleToast();
  }
  for (const tray of [mounts.toastRegion, mounts.pageToasts]) {
    listen(tray, 'pointerenter', syncPause);
    listen(tray, 'pointerleave', syncPause);
    listen(tray, 'focusin', syncPause);
    listen(tray, 'focusout', () => queueMicrotask(syncPause));
  }
  function renderToast(s) {
    const toast = s.ui.toast, message = toast?.message ?? text(toast?.id, toast?.params);
    const dig = s.ui.route.page === 'dig';
    const target = dig ? mounts.toastRegion : mounts.pageToasts;
    const other = dig ? mounts.pageToasts : mounts.toastRegion;
    other.hidden = true;
    target.hidden = !message;
    mounts.keyHints.hidden = !!message && dig;
    if (toast?.serial !== toastSerial) {
      toastSerial = toast?.serial ?? null;
      clearToastTimer();
      mounts.toastRegion.replaceChildren();
      mounts.pageToasts.replaceChildren();
      if (message) {
        const error = toast.error || toast.type === 'error' || /(?:ERR|FAIL|SRV-LOST)/.test(toast.id || '');
        const content = h('div', {className: 'toast' + (error ? ' toast--err' : '')},
          h('span', {className: 'toast__msg', title: message}, (error ? '! ' : '') + message));
        if (toast.undo) content.append(h('button', {type: 'button', className: 'btn btn--small toast-undo',
          'data-action': 'undo', onclick: () => store.actions.undoJudgment().catch(report)}, 'Undo'));
        target.append(content);
        toastRemaining = toast.undo ? 8000 : 5000;
        toastPaused = target.matches(':hover') || target.contains(document.activeElement);
        scheduleToast();
      }
    } else if (message && !target.firstChild && other.firstChild) {
      target.append(other.firstChild);
      syncPause();
    }
    const announcement = s.ui.announcement;
    if (announcement && announcedSerial !== announcement.serial) {
      announcedSerial = announcement.serial;
      mounts.announcer.textContent = announcement.id ? text(announcement.id, announcement.params) : message;
    }
  }

  function render(s) {
    const route = s.ui.route, dig = route.page === 'dig', inStep = dig && !!route.stepId;
    device.dataset.page = route.page;
    mounts.newDigRegion.hidden = !dig || inStep;
    mounts.digRegion.hidden = !inStep;
    mounts.feedsRegion.hidden = route.page !== 'feeds';
    mounts.setupRegion.hidden = route.page !== 'setup';
    mounts.connectRegion.hidden = route.page !== 'connect';
    mounts.keyStrip.hidden = !dig;
    mounts.viewToggle.hidden = !dig;
    mounts.digRegion.dataset.view = route.view;
    mounts.stepRegion.hidden = route.view === 'map';
    mounts.territoryRegion.hidden = route.view !== 'map';
    mounts.stepmapRegion.hidden = route.view === 'map';
    mounts.slimListRegion.hidden = route.view !== 'map';
    mounts.movesRegion.hidden = !s.ui.menu;
    mounts.sheetRegion.hidden = !s.ui.sheet;
    for (const page of ['dig', 'feeds', 'setup']) {
      const link = mounts['mode-' + page];
      if (route.page === page) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
    mounts.modeDig.href = formatRoute(s.ui.lastDigRoute || {page: 'dig', view: 'list'});
    for (const view of ['list', 'map']) {
      const button = mounts['view-' + view];
      button.disabled = !inStep;
      button.setAttribute('aria-pressed', String(route.view === view));
      if (inStep) {
        button.removeAttribute('title');
        button.removeAttribute('aria-describedby');
      } else {
        button.title = text('S-LISTMAP-OFF');
        button.setAttribute('aria-describedby', 'view-disabled-reason');
      }
    }
    const connected = s.ui.server === 'up';
    mounts.serverLed.dataset.state = connected ? 'ok' : s.ui.server === 'down' ? 'down' : 'connecting';
    mounts.serverLed.setAttribute('aria-label', connected ? text('S-SRV-LED-OK') :
      s.ui.server === 'down' ? text('S-SRV-LED-DOWN') : 'Server: connecting');
    const down = s.ui.server === 'down';
    if (!down) {
      clock.clearTimeout(lostTimer);
      lostTimer = null;
      lostLong = false;
    } else if (lostTimer === null && !lostLong) {
      lostTimer = clock.setTimeout(() => {
        lostTimer = null;
        lostLong = true;
        if (!destroyed) render(store.getState());
      }, LOST_GRACE_MS);
    }
    // The last Digger this page saw decides the advice: start can reopen it, serve and Docker can't.
    const launcher = s.ui.setup.health?.launcher;
    mounts.serverBanner.hidden = !down;
    mounts.serverBanner.textContent = text(!lostLong ? 'S-SRV-LOST' : launcher === 'serve' ? 'S-SRV-LOST-SERVE' : 'S-SRV-LOST-START');
    const version = s.ui.setup.health?.version;
    if (version) mounts.version.textContent = 'v' + version;
    const data = s.ui.settings?.data;
    if (data && launcher) {
      mounts.aboutVersion.textContent = text(launcher === 'serve' ? 'S-ABOUT-SERVE' : 'S-ABOUT-START',
        {'data folder': data.home, 'database file': data.db});
    }
    renderToast(s);
  }
  const unsubscribe = store.subscribe(render);
  disposers.push(unsubscribe, () => clock.clearTimeout(lostTimer));
  render(store.getState());

  const motion = matchMedia('(prefers-reduced-motion: reduce)');
  const applyMotion = () => {
    device.dataset.reducedMotion = String(motion.matches);
    store.actions.setReducedMotion(motion.matches);
  };
  motion.addEventListener('change', applyMotion);
  disposers.push(() => motion.removeEventListener('change', applyMotion));
  applyMotion();

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    clearToastTimer();
    for (const dispose of disposers.reverse()) dispose();
    for (const component of components.reverse()) component.destroy();
    store.destroy();
    device.dataset.shellState = 'destroyed';
    activeBoot = null;
  }
  listen(window, 'pagehide', (event) => { if (!event.persisted) destroy(); });

  try {
    const modules = new Map();
    for (const spec of registry) {
      if (!modules.has(spec.path)) {
        try { modules.set(spec.path, await import(spec.path)); }
        catch (error) {
          // Missing imports may include a not-yet-merged component dependency.
          if (!(error instanceof TypeError) || !/fetch|import|module/i.test(error.message)) throw error;
          modules.set(spec.path, null);
          omittedModules.push(spec.path);
        }
      }
      const module = modules.get(spec.path);
      if (!module) continue;
      for (const entry of spec.exports) {
        if (typeof module[entry] !== 'function') throw new TypeError(spec.path + ' must export ' + entry);
      }
      const stylesheet = document.querySelector('link[data-component-style="' + spec.name + '"]');
      stylesheet.href = stylesheet.dataset.href;
      const mounted = await module[spec.entry](mounts[spec.root], ctx);
      if (typeof mounted?.destroy !== 'function') throw new TypeError(spec.entry + ' must return destroy()');
      components.push(mounted);
    }
    // Server failure is a visible, recoverable shell state, not a boot failure.
    await store.actions.hydrate().then(() => { hydrated = true; }).catch(() => {});
    const healthTimer = clock.setInterval(async () => {
      if (destroyed || healthPending) return;
      healthPending = true;
      try {
        await api.request('GET', '/api/health');
        if (!destroyed) {
          if (!hydrated) { await store.actions.hydrate(); hydrated = true; }
          else await store.actions.setServer('up');
        }
      } catch { if (!destroyed) await store.actions.setServer('down'); }
      finally { healthPending = false; }
    }, 5000);
    disposers.push(() => clock.clearInterval(healthTimer));
    device.dataset.shellState = 'ready';
    return Object.freeze({ctx, store, api, mounts, clock, registry,
      omittedModules: Object.freeze(omittedModules), destroy});
  } catch (error) {
    destroy();
    throw error;
  }
}

if (document.getElementById('device')) {
  boot().catch((error) => console.error('Shell boot failed:', error));
}
