import {selectors} from './state.js';

const editable = 'input, textarea, select, [contenteditable]:not([contenteditable="false"]), [role="textbox"]';
const moves = {t: 'track', l: 'label', a: 'artist', c: 'person'};
const repeatedActions = new Set(['/', '?', 'g', '[', ']', 'k', 'x', 'z', 't', 'l', 'a', 'c', 'm',
  ' ', 'p', 'u', 'v', 'enter', 'escape']);

// The six approved groups also document native keys and the canvas/menu handlers.
const groups = [
  ['Start', [
    ['/', 'Open the new-dig screen and focus the seed box; the player keeps playing'],
    ['Enter', 'In the search box: add the highlighted result, or start on an empty box with chips'],
    ['Backspace', 'In an empty search box: drop the last chip'],
    ['?', 'Open or close the key sheet'],
  ]],
  ['Move around', [
    ['↑ / ↓', 'Select picks in rank order in the list, slim list, map or territory; move focus in the trail'],
    ['Tab / Shift+Tab', 'Next or previous focus stop'],
    ['Enter', 'In the trail: open the focused step, or expand the focused stub'],
    ['U', 'Open the parent step'], ['G', 'Select the playing track and open its step'],
    ['Cmd+[ / Cmd+]', "The browser's back and forward, which walk the steps you viewed"],
    ['V', 'Switch List and Map'],
    ['Esc', 'Close the picker, menu, switcher or sheet; otherwise collapse the expanded rail; otherwise clear the selection'],
    ['Esc', 'In search: close results, then clear text, then blur'],
  ]],
  ['Play', [
    ['Space', 'Play the selection from its row; on the playing track, pause or resume'],
    ['Space', 'On a focused button or link: native press'],
    ['P / Shift+P', 'Play this step from the top, now / after the current track'],
    ['[ / ]', 'Previous / next track'], ['← / →', 'Seek back 15 s / forward 15 s'],
    ['Shift+→', 'Seek forward 60 s'],
  ]],
  ['Keep', [
    ['K', 'Keep the playing track, or the selection when idle'],
    ['X', 'Pass the playing track, or the selection when idle; skip the playing track'],
    ['Z / Cmd+Z', 'Undo the last keep or pass'],
  ]],
  ['Dig from…', [
    ['Enter', 'In the list, map or territory: open the move menu for the selection'],
    ['T L A C', 'Next step: tracks that sound like this track / its label / its artist / a credited person'],
    ['Shift+T L A C', 'The same, one step further (about 30 s)'],
    ['M / Shift+M', 'Sounds like the keeps in this step / in the whole dig'],
    ['↑ / ↓', 'In a menu or picker: move the highlight'],
    ['Enter / Shift+Enter', 'In a menu: choose near / one step further; in a picker: choose the highlighted row'],
    ['→ / ← / Esc', 'In a menu: open a picker / go back or close; arrows never seek'],
    ['1–9', 'In a picker: choose that row; arrows and Enter reach every remaining row'],
  ]],
  ['Map', [
    ['+ (or =) / −', 'Zoom in / out'], ['0', 'Fit'],
    ['drag', 'Pan the camera'], ['double-click empty space', 'Fit'],
  ]],
];

/** Page shortcuts share the store with the native region handlers. */
export function mountKeys(root, ctx) {
  const {store, h, text, mounts} = ctx;
  const strip = mounts.keyStrip, hints = mounts.keyHints, stage = mounts.main;
  const disposers = [];
  let sheetOpen = false, opener = null, lastFocused = null, lastRegion = null, routeStep = null, destroyed = false;
  // Pointer presses focus their own targets (often with preventScroll); only
  // keyboard focus is scrolled into view here, so a click never moves its target.
  let pointerInput = false;

  function listen(target, name, handler, options) {
    target.addEventListener(name, handler, options);
    disposers.push(() => target.removeEventListener(name, handler, options));
  }
  function visible(element) {
    return element?.isConnected && !element.disabled && !element.closest('[hidden]') && element.getClientRects().length;
  }
  function focus(element) {
    if (!visible(element)) return;
    element.focus({preventScroll: true});
    element.scrollIntoView({block: 'nearest', inline: 'nearest', behavior: 'auto'});
  }
  function stepFocus() {
    const s = store.getState();
    if (s.ui.route.page !== 'dig') return stage;
    if (!s.ui.route.stepId) return document.getElementById('seed-input') || stage;
    if (s.ui.route.view === 'map') return document.getElementById('territory-canvas') || mounts.territoryRegion;
    return mounts.stepRegion.querySelector('.steplist-row[tabindex="0"]') || mounts.stepRegion;
  }
  function run(result, navigate = false) {
    Promise.resolve(result).then(() => {
      if (!destroyed && navigate && !store.getState().ui.menu && !store.getState().ui.sheet) focus(stepFocus());
    }).catch(error => {
      if (!destroyed) store.actions.showToast('S-T-MOVE-ERR', {reason: error.message});
    });
  }

  const toggle = h('button', {id: 'keys-toggle', type: 'button', className: 'btn btn--small',
    'aria-controls': 'keys-sheet', 'aria-expanded': 'false', onclick: () => store.actions.toggleSheet()}, '? keys');
  strip.append(toggle);
  const close = h('button', {id: 'keys-close', type: 'button', className: 'btn btn--small',
    'aria-label': 'Close keys', onclick: () => store.actions.toggleSheet()}, 'Close · Esc');
  const content = h('div', {className: 'keys-content', tabindex: 0, 'aria-label': 'Keyboard reference'},
    h('div', {className: 'keys-groups'}, ...groups.map(([name, rows], index) =>
      h('section', {className: 'keys-group', 'aria-labelledby': `keys-group-${index}`},
        h('h3', {id: `keys-group-${index}`}, name),
        h('dl', {}, ...rows.flatMap(([key, action]) => [h('dt', {}, h('kbd', {}, key)), h('dd', {}, action)]))))),
    h('div', {className: 'keys-footnotes'}, ...[1, 2, 3].map(n => h('p', {}, text(`S-KEYS-FOOT-${n}`)))));
  const dialog = h('section', {id: 'keys-sheet', className: 'keys-sheet', role: 'dialog',
    'aria-modal': 'true', 'aria-labelledby': 'keys-title', tabindex: -1},
    h('header', {className: 'keys-header'}, h('h2', {id: 'keys-title'}, text('S-KEYS-TITLE')), close), content);
  const backdrop = h('div', {className: 'keys-backdrop'}, dialog);
  root.replaceChildren(backdrop);

  function region(element) {
    if (element?.matches('iframe') && mounts.playerRegion.contains(element)) return 'iframe';
    if (mounts.movesRegion.contains(element)) return store.getState().ui.menu?.level === 'picker' ? 'picker' : 'menu';
    if (mounts.trailRegion.contains(element)) return 'trail';
    if (mounts.inspectorRegion.contains(element)) return 'inspector';
    if (mounts.stepmapRegion.contains(element)) return 'map';
    if (mounts.territoryRegion.contains(element)) return 'territory';
    // The search's third Esc blurs to the region itself: that is page focus.
    if (mounts.newDigRegion.contains(element)) return element === mounts.newDigRegion ? 'page' : 'search';
    return 'list';
  }
  function focusin(event) {
    if (store.getState().ui.sheet) {
      if (!root.contains(event.target)) focus(close);
      return;
    }
    // Only store-open overlays participate in application focus regions.
    if (root.contains(event.target) || mounts.movesRegion.contains(event.target) && !store.getState().ui.menu) return;
    lastFocused = event.target;
    lastRegion = region(event.target);
    // Menus are not modal: native Tab can leave them without retaining a stale source.
    // Focus returning to the menu's own opener (a double-click's second press) keeps it.
    const menu = store.getState().ui.menu;
    const menuOpener = menu?.openerId && document.getElementById(menu.openerId);
    if (menu && !mounts.movesRegion.contains(event.target) && !menuOpener?.contains(event.target)) {
      if (menu.level === 'picker') store.actions.menuBack();
      store.actions.menuBack();
    }
    const name = region(event.target);
    if (store.getState().ui.focusRegion !== name) store.actions.focusRegion(name);
    // Each scroll container honours its own scroll padding (including sticky headers).
    if (!pointerInput) event.target.scrollIntoView?.({block: 'nearest', inline: 'nearest', behavior: 'auto'});
  }
  function iframeFocus() {
    queueMicrotask(() => {
      if (!destroyed && region(document.activeElement) === 'iframe' && !store.getState().ui.sheet)
        store.actions.focusRegion('iframe');
    });
  }

  function render(s) {
    const changedStep = routeStep !== s.ui.route.stepId;
    routeStep = s.ui.route.stepId;
    const hint = selectors.keyHints(s);
    let wording = text(hint.id, hint.params);
    if (hint.id === 'S-KS-LIST') {
      wording = selectors.judgingTarget(s) ? wording.replace('the playing track', `the ${hint.judging} track`) :
        wording.replace('K keep the playing track · ', '');
    }
    const line = wording.replace(/ · \? (?:all )?keys$/, '');
    if (hints.textContent !== line) {
      const parts = line.split(' · ').flatMap((part, index) => {
        const key = /^(↑↓|T L A C|\+ −|Enter|Backspace|Space|Cmd\+\[|Tab|Shift|Esc|K|U|V|L)(?= )/.exec(part);
        return [index ? ' · ' : '', key ? h('span', {className: 'keys-hint'},
          h('kbd', {}, key[0]), part.slice(key[0].length)) : part];
      });
      hints.replaceChildren(...parts.map(part => typeof part === 'string' ? document.createTextNode(part) : part));
    }
    hints.title = line;
    toggle.textContent = wording.endsWith(' · ? all keys') ? '? all keys' : '? keys';
    toggle.setAttribute('aria-expanded', String(s.ui.sheet));
    root.hidden = !s.ui.sheet;
    if (s.ui.sheet !== sheetOpen) {
      sheetOpen = s.ui.sheet;
      if (sheetOpen) {
        opener = document.activeElement;
        focus(close);
      } else {
        focus(visible(opener) ? opener : stepFocus());
        opener = null;
      }
    }
    if (changedStep && routeStep && !s.ui.sheet && !visible(lastFocused) &&
        ['search', 'list', 'menu', 'picker', 'inspector'].includes(lastRegion))
      focus(stepFocus());
  }

  // Guard local handlers too, without cancelling native editing/browser defaults.
  function beforeKey(event) {
    pointerInput = false;
    const key = event.key.toLowerCase(), typing = event.target.closest?.(editable);
    const undo = event.metaKey && !event.ctrlKey && !event.altKey && !event.shiftKey && key === 'z' && !typing;
    const browserChord = (event.ctrlKey || event.altKey || event.metaKey) && !undo;
    const repeated = event.repeat && (!typing && repeatedActions.has(key) ||
      event.target.id === 'seed-input' && (key === 'enter' || key === 'backspace' && !event.target.value));
    if (event.defaultPrevented || event.isComposing || event.keyCode === 229 ||
        browserChord ||
        repeated) {
      // Native Enter activation on a menu button must not bypass the repeat guard.
      if (!browserChord && (repeated || event.isComposing && !typing && repeatedActions.has(key))) event.preventDefault();
      event.stopPropagation();
      return;
    }
    if (!store.getState().ui.sheet) return;
    if (key === 'tab') {
      const stops = [close, content], index = stops.indexOf(document.activeElement);
      event.preventDefault(); event.stopPropagation();
      focus(stops[index < 0 ? 0 : (index + (event.shiftKey ? -1 : 1) + stops.length) % stops.length]);
    } else if (key === 'escape' || key === '?') {
      event.preventDefault(); event.stopPropagation();
      store.actions.toggleSheet();
    } else event.stopPropagation();
  }

  function keydown(event) {
    if (event.defaultPrevented || event.isComposing || event.target.closest?.(editable) ||
        region(document.activeElement) === 'iframe') return;
    const key = event.key.toLowerCase(), s = store.getState(), actions = store.actions;
    const undo = event.metaKey && !event.ctrlKey && !event.altKey && !event.shiftKey && key === 'z';
    if ((event.ctrlKey || event.altKey || event.metaKey) && !undo || event.repeat && repeatedActions.has(key)) return;
    let handled = true;
    if (key === '/') run(actions.openNewDig(), true);
    else if (key === '?') actions.toggleSheet();
    else if (key === 'g') run(actions.goPlaying(), true);
    else if (key === '[') actions.previous();
    else if (key === ']') actions.next();
    else if (key === 'z') run(actions.undoJudgment());
    else if (key === 'k' || key === 'x') {
      const target = selectors.judgingTarget(s);
      if (target) run(actions.judge({trackKey: target.trackKey, stepId: target.stepId,
        verdict: key === 'k' ? 'keep' : 'pass', mode: 'key'}));
      else actions.showToast(key === 'k' ? 'S-T-NOTARGET-K' : 'S-T-NOTARGET-X');
    } else if (s.ui.route.page !== 'dig') handled = false;
    else if (key === 'escape') actions.closeOverlay();
    else if (moves[key]) run(actions.chooseMove(moves[key], {further: event.shiftKey}), true);
    else if (key === 'm') run(actions.soundsLikeKeeps({scope: event.shiftKey ? 'dig' : 'step'}), true);
    else if (key === 'p') actions.playStep(s.ui.route.stepId, {afterCurrent: event.shiftKey});
    else if (key === 'u') run(actions.goParent(), true);
    else if (key === 'v') { actions.setView(s.ui.route.view === 'map' ? 'list' : 'map'); focus(stepFocus()); }
    else if (key === 'arrowleft' || key === 'arrowright') actions.seek(key === 'arrowleft' ? -15 : event.shiftKey ? 60 : 15);
    else if (key === ' ' && !event.target.closest?.('button, a, [role="button"]')) {
      const point = selectors.selectedPoint(s);
      if (point?.kind === 'track' && point.source_rank != null)
        actions.playPick({pickId: `${point.source_step_id}:${point.source_rank}`});
      else actions.showToast('S-T-SPACE-NOSEL');
    } else if (['list', 'map', 'territory'].includes(region(event.target)) &&
        !event.target.closest?.('button, a, [role="button"]')) {
      if (key === 'arrowup' || key === 'arrowdown') actions.selectAdjacent(key === 'arrowup' ? -1 : 1);
      else if (key === 'enter') {
        const point = selectors.selectedPoint(s);
        if (!point) return;
        const rect = event.target.getBoundingClientRect();
        let anchor = {left: rect.left, top: rect.top, bottom: rect.bottom};
        if (event.target.matches('#stepmap-canvas, #territory-canvas')) {
          const detail = {point, anchor: null};
          event.target.dispatchEvent(new CustomEvent('map-menu-anchor', {detail}));
          const bounds = stage.getBoundingClientRect();
          const top = Math.max(rect.top, bounds.top, 0);
          anchor = detail.anchor || {left: Math.max(rect.left, bounds.left, 0), top, bottom: top};
        }
        actions.openMoveMenu(point, {sourceStepId: s.ui.route.stepId, openerId: event.target.id || null,
          further: event.shiftKey, anchor});
      } else handled = false;
    } else handled = false;
    if (handled) event.preventDefault();
  }

  listen(document, 'pointerdown', () => { pointerInput = true; }, true);
  listen(document, 'keydown', beforeKey, true);
  listen(document, 'keydown', keydown);
  listen(document, 'focusin', focusin);
  listen(window, 'blur', iframeFocus);
  listen(backdrop, 'click', event => { if (event.target === backdrop) store.actions.toggleSheet(); });
  const unsubscribe = store.subscribe(render);
  render(store.getState());
  return {destroy() {
    destroyed = true;
    unsubscribe(); disposers.reverse().forEach(dispose => dispose());
    toggle.remove(); root.replaceChildren(); hints.replaceChildren();
  }};
}
