import {selectors} from './state.js';

const letters = {track: 'T', label: 'L', artist: 'A', person: 'C'};
const pickerTitles = {label: 'S-PICK-LABEL', artist: 'S-PICK-ARTIST', person: 'S-PICK-PERSON'};

/** The store captures the source, choices and further mode; this mount owns their presentation. */
export function mountMoves(root, ctx) {
  const {store, h, text, clampPopover, mounts} = ctx;
  const stage = mounts.main;
  let popover = null, signature = null, previousMenu = null, opener = null, destroyed = false;

  function visibleChoices(menu) {
    const origin = menu.level === 'menu' && menu.point.kind === 'track' && menu.point.source_rank == null;
    return menu.choices.map((choice, index) => ({choice, index}))
      .filter(({choice}) => !origin || choice.move !== 'track');
  }

  function highlight(index) {
    let menu = store.getState().ui.menu;
    if (!menu || menu.choices[index]?.disabled_reason) return;
    const enabled = menu.choices.map((choice, i) => choice.disabled_reason ? -1 : i).filter(i => i >= 0);
    if (!enabled.includes(menu.highlight)) {
      store.actions.menuHighlight(0);
      menu = store.getState().ui.menu;
    }
    const delta = enabled.indexOf(index) - enabled.indexOf(menu.highlight);
    if (delta) store.actions.menuHighlight(delta);
  }

  function moveHighlight(delta) {
    const menu = store.getState().ui.menu;
    const enabled = visibleChoices(menu).filter(({choice}) => !choice.disabled_reason);
    const current = enabled.findIndex(({index}) => index === menu.highlight);
    const next = enabled[Math.max(0, Math.min(enabled.length - 1, current < 0 ? 0 : current + delta))];
    if (next) highlight(next.index);
  }

  function run(result) {
    Promise.resolve(result).catch(error => {
      if (!destroyed) store.actions.showToast('S-T-MOVE-ERR', {reason: error.message});
    });
  }

  function choose(index, further = false) {
    const menu = store.getState().ui.menu;
    if (!menu || menu.choices[index]?.disabled_reason || store.getState().ui.server !== 'up') return;
    highlight(index);
    run(store.actions.menuEnter({further}));
  }

  function canFocus(element) {
    return element?.isConnected && !element.disabled && !element.closest('[hidden]') && element.getClientRects().length;
  }

  function restoreFocus() {
    const s = store.getState();
    const target = document.getElementById(previousMenu?.openerId) || opener;
    const fallback = s.ui.route.view === 'map' ? mounts.territoryRegion : mounts.stepRegion;
    const visible = [target, fallback, stage].find(canFocus);
    visible?.focus({preventScroll: true});
  }

  function position() {
    const menu = store.getState().ui.menu;
    if (!popover || !menu) return;
    const row = opener?.closest('.steplist-row');
    let anchor = menu.anchor || opener;
    if (row) {
      const rect = row.getBoundingClientRect();
      anchor = {left: menu.anchor?.left ?? rect.left, top: rect.top, bottom: rect.bottom};
    }
    clampPopover(popover, anchor, stage, {avoidAnchor: !!(row || menu.anchor)});
  }

  function sourceText(point) {
    if (point.kind === 'label') return text('S-MENU-FROM-LABEL', {Label: point.name});
    if (point.kind === 'person') return text('S-MENU-FROM-PERSON', {Person: point.name});
    return text('S-MENU-FROM', {'Artist - Title': point.name});
  }

  function itemText(choice) {
    const full = text(choice.label_id, choice.params);
    const separator = full.lastIndexOf(' · ');
    const estimate = separator < 0 ? '' : full.slice(separator + 3);
    const label = choice.picker_points.length > 1 && ['label', 'artist'].includes(choice.move) ?
      text(`S-MENU-${letters[choice.move]}-MANY`) : separator < 0 ? full : full.slice(0, separator);
    return {label, estimate};
  }

  function pickerText(s, choice) {
    const point = choice.point;
    if (point.kind !== 'person') return point.name;
    // Roles are display metadata, read from the verified source facts, never new move inference.
    const pick = selectors.orderedPicks(s, point.source_step_id).find(p => p.rank === point.source_rank);
    const credit = pick?.facts?.credits?.find(item => String(item.id) === String(point.discogs_id) && item.name === point.name);
    return point.name + (credit?.roles?.length ? ' · ' + credit.roles.join(', ') : '');
  }

  function build(s, menu) {
    const title = menu.level === 'picker' ? text(pickerTitles[menu.choices[0]?.move]) : text('S-MENU-TITLE');
    const list = h('div', {className: 'moves-choices', role: 'none'});
    for (const {choice, index} of visibleChoices(menu)) {
      const disabled = choice.disabled_reason || (s.ui.server !== 'up' ? 'S-SRV-LOST' : null);
      const reasonId = `moves-reason-${index}`;
      const item = menu.level === 'picker' ? {label: pickerText(s, choice), estimate: ''} : itemText(choice);
      const label = item.label + (item.estimate ? ' · ' + item.estimate : '');
      const control = h('button', {type: 'button', role: 'menuitem', className: 'moves-choice',
        tabindex: '-1', 'data-action': 'choose', 'data-index': index, 'data-point-id': choice.point?.point_id,
        'aria-label': label, 'aria-disabled': String(!!disabled), 'aria-describedby': disabled ? reasonId : null},
      h('kbd', {'aria-hidden': 'true'}, menu.level === 'picker' ? index < 9 ? String(index + 1) : '' : letters[choice.move]),
      h('span', {className: 'moves-wording'}, h('span', {className: 'moves-label'}, item.label),
        item.estimate && h('span', {className: 'moves-estimate'}, ' · ' + item.estimate)));
      const row = h('div', {className: 'moves-row', role: 'none'}, control);
      if (menu.level === 'menu') row.append(h('button', {type: 'button', role: 'menuitem',
        className: 'moves-further', tabindex: '-1', 'data-action': 'further', 'data-index': index,
        'aria-label': text('S-MENU-FAR-ARIA', {item: label}), 'aria-disabled': String(!!disabled),
        'aria-describedby': disabled ? reasonId : null}, '» further'));
      if (disabled) row.append(h('span', {id: reasonId, className: 'moves-reason'}, text(disabled)));
      list.append(row);
    }
    popover = h('section', {className: 'moves-popover', role: 'menu', tabindex: '-1',
      'aria-labelledby': 'moves-title', 'aria-describedby': 'moves-from', 'data-level': menu.level,
      'data-step-id': menu.sourceStepId},
    h('h2', {id: 'moves-title'}, title), h('p', {id: 'moves-from'}, sourceText(menu.point)),
    h('p', {className: 'moves-mode', hidden: !menu.further}, text('S-MENU-FAR-MODE')),
    list,
    h('p', {className: 'moves-foot'}, text('S-MENU-FOOT')));
    root.replaceChildren(popover);
  }

  function render(s) {
    const menu = s.ui.menu;
    root.hidden = !menu;
    if (!menu) {
      if (previousMenu) {
        const hadFocus = root.contains(document.activeElement);
        root.replaceChildren();
        if (hadFocus) restoreFocus();
      }
      popover = null; signature = null; previousMenu = null; opener = null;
      return;
    }
    const opening = !previousMenu;
    if (opening) opener = document.getElementById(menu.openerId) ||
      (root.contains(document.activeElement) ? null : document.activeElement);
    const enabled = visibleChoices(menu).filter(({choice}) => !choice.disabled_reason);
    if (enabled.length && !enabled.some(({index}) => index === menu.highlight)) {
      highlight(enabled[0].index);
      return;
    }
    const next = JSON.stringify([menu.level, menu.sourceStepId, menu.point, menu.choices, menu.anchor, s.ui.server]);
    const rebuilt = signature !== next;
    const hadFocus = root.contains(document.activeElement);
    previousMenu = menu;
    signature = next;
    if (rebuilt) build(s, menu);
    popover.querySelector('.moves-mode').hidden = !menu.further;
    popover.dataset.further = String(menu.further);
    for (const button of popover.querySelectorAll('.moves-choice')) {
      const active = Number(button.dataset.index) === menu.highlight && button.getAttribute('aria-disabled') !== 'true';
      button.tabIndex = active ? 0 : -1;
      button.closest('.moves-row').classList.toggle('moves-highlight', active);
    }
    position();
    if (opening || hadFocus) {
      const target = popover.querySelector('.moves-choice[tabindex="0"]') || popover;
      const active = document.activeElement;
      const focusedFurther = active?.matches('.moves-further') && Number(active.dataset.index) === menu.highlight;
      if (active !== target && !focusedFurther) {
        target.focus({preventScroll: true});
        target.scrollIntoView({block: 'nearest', inline: 'nearest'});
      }
    }
  }

  function click(event) {
    const control = event.target.closest('[data-action]');
    if (!control || !root.contains(control)) return;
    event.preventDefault(); event.stopPropagation();
    if (event.detail > 1 || control.getAttribute('aria-disabled') === 'true') return;
    choose(Number(control.dataset.index), event.shiftKey || control.dataset.action === 'further');
  }

  function keydown(event) {
    const menu = store.getState().ui.menu;
    if (!menu || event.defaultPrevented || event.isComposing || event.ctrlKey || event.altKey || event.metaKey ||
      event.target.closest('input,textarea,select,[contenteditable]:not([contenteditable="false"]),[role="textbox"]')) return;
    const key = event.key.toLowerCase();
    if (!['arrowup', 'arrowdown', 'arrowleft', 'arrowright', 'enter', 'escape', 't', 'l', 'a', 'c'].includes(key) &&
      !(menu.level === 'picker' && /^[1-9]$/.test(key))) return;
    event.preventDefault(); event.stopPropagation();
    if (event.repeat && !['arrowup', 'arrowdown'].includes(key)) return;
    if (key === 'arrowup' || key === 'arrowdown') moveHighlight(key === 'arrowup' ? -1 : 1);
    else if (key === 'escape' || key === 'arrowleft') store.actions.menuBack();
    else if (key === 'arrowright') {
      const choice = menu.choices[menu.highlight];
      if (menu.level === 'menu' && choice?.picker_points.length > 1)
        store.actions.openPicker(choice.move, {further: event.shiftKey});
    } else if (key === 'enter') choose(Number(event.target.closest('[data-index]')?.dataset.index ?? menu.highlight),
      event.shiftKey || event.target.closest('[data-action]')?.dataset.action === 'further');
    else if (/^[1-9]$/.test(key)) choose(Number(key) - 1, event.shiftKey);
    else if (menu.level === 'menu' && store.getState().ui.server === 'up') {
      const move = Object.keys(letters).find(move => letters[move].toLowerCase() === key);
      run(store.actions.chooseMove(move, {further: event.shiftKey}));
    }
  }

  function focusin(event) {
    if (!store.getState().ui.menu) return;
    const region = store.getState().ui.menu.level === 'picker' ? 'picker' : 'menu';
    if (store.getState().ui.focusRegion !== region) store.actions.focusRegion(region);
    const control = event.target.closest('[data-index]');
    if (control?.getAttribute('aria-disabled') === 'false') highlight(Number(control.dataset.index));
  }

  function outside(event) {
    const menu = store.getState().ui.menu;
    if (!menu || root.contains(event.target)) return;
    // A button opener's presses belong to its own click handler, so the second
    // press of a double-click cannot close the menu its first click opened.
    const openerButton = menu.openerId && document.getElementById(menu.openerId);
    if (openerButton?.matches('button') && openerButton.contains(event.target)) return;
    if (menu.level === 'picker') store.actions.menuBack();
    store.actions.menuBack();
  }

  function dblclick(event) { event.stopPropagation(); }

  root.addEventListener('click', click);
  root.addEventListener('dblclick', dblclick);
  root.addEventListener('keydown', keydown);
  root.addEventListener('focusin', focusin);
  stage.addEventListener('pointerdown', outside);
  const resize = new ResizeObserver(position);
  resize.observe(stage);
  const unsubscribe = store.subscribe(render);
  render(store.getState());
  return {destroy() {
    destroyed = true;
    unsubscribe(); resize.disconnect();
    root.removeEventListener('click', click);
    root.removeEventListener('dblclick', dblclick);
    root.removeEventListener('keydown', keydown);
    root.removeEventListener('focusin', focusin);
    stage.removeEventListener('pointerdown', outside);
    root.replaceChildren();
  }};
}
