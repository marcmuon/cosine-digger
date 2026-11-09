import {selectors} from './state.js';

// Where a signed-in cosine.club user copies their API key (cosine.club's API docs link it).
const KEY_PAGE = 'https://cosine.club/account/api';

/** The key screen (#connect) and the notice for a returning user without a key. A pasted key
 * lives only in the field until cosine.club accepts it: the store and storage never hold it. */
export function mountConnect(root, {store, h, text, mounts}) {
  const actions = store.actions;
  let destroyed = false, wasActive = false, busy = false, result = null, envFor = null, serverWas = null;

  const input = h('input', {id: 'connect-key', className: 'input input--mono', type: 'password', autocomplete: 'off',
    spellcheck: 'false', placeholder: text('S-KEY-PH'), 'aria-describedby': 'connect-status connect-privacy'});
  const [before, after] = text('S-KEY-HELP').split(text('S-KEY-HELP-LINK'));
  const help = h('p', {className: 'field__help'}, before,
    h('a', {href: KEY_PAGE, target: '_blank', rel: 'noopener noreferrer'}, text('S-KEY-HELP-LINK')), after);
  const field = h('div', {className: 'field'},
    h('label', {className: 'field__label', for: 'connect-key'}, text('S-KEY-LABEL')), input, help);
  const status = h('p', {id: 'connect-status', className: 'connect-status', role: 'status', hidden: true});
  const tryAgain = h('button', {type: 'button', className: 'btn btn--small', 'data-action': 'connect-retry', hidden: true,
    onclick: () => submit()}, text('S-KEY-TRY-AGAIN'));
  const go = h('button', {type: 'submit', className: 'btn btn--go', 'data-action': 'connect'}, text('S-KEY-BTN'));
  const goRow = h('div', {className: 'connect-actions'}, go);
  const privacy = h('p', {id: 'connect-privacy', className: 'connect-privacy'}, text('S-KEY-PRIVACY'));
  const lede = h('p', {className: 'connect-lede'}, text('S-KEY-LEDE'));
  const envLine = h('p', {className: 'connect-env', hidden: true});
  const form = h('form', {className: 'connect-form', autocomplete: 'off', novalidate: true,
    onsubmit: (event) => { event.preventDefault(); submit(); }},
    field, h('div', {className: 'connect-result'}, status, tryAgain), goRow, privacy);
  root.replaceChildren(h('section', {className: 'panel connect-panel', 'aria-labelledby': 'connect-heading'},
    h('header', {className: 'panel__bar'}, h('h1', {id: 'connect-heading', className: 'panel__title'}, text('S-KEY-TITLE'))),
    h('div', {className: 'panel__body connect-body'}, lede, envLine, form)));

  const notice = mounts.keyNotice;
  notice.replaceChildren(h('span', {className: 'key-notice__text'}, text('S-KEY-NOTICE')),
    h('button', {type: 'button', className: 'btn btn--small', 'data-action': 'add-key',
      onclick: () => actions.openConnect().catch(report)}, text('S-KEY-ADD')));

  function report(error) {
    if (!destroyed) actions.showToast(null, {}, {message: error.message, error: true});
  }
  function envKey(s) {
    const key = selectors.cosineKey(s);
    return key.source === 'env' ? key : null;
  }
  async function submit() {
    if (busy || destroyed) return;
    const env = envKey(store.getState());
    if (env && !env.set) return;
    const value = env ? undefined : input.value;
    if (!env && !value.trim()) {
      result = {state: 'empty'};
      render(store.getState());
      input.focus();
      return;
    }
    busy = true;
    result = null;
    render(store.getState());
    let outcome;
    try { outcome = await actions.connectKey(value); }
    catch (error) { outcome = {state: 'digger', reason: error.message}; }
    finally { busy = false; }
    if (destroyed) return;
    // Accepted: the store went back where the screen was opened from. No answer from Digger: the banner says so.
    result = outcome?.state === 'connected' ? null : outcome;
    render(store.getState());
    if (outcome?.state === 'rejected' && !env) input.select();
  }
  function message(outcome, env) {
    const wait = {n: outcome?.retry_after_s ?? 30};
    switch (outcome?.state) {
      case 'empty': case 'missing': return {line: text('S-KEY-EMPTY')};
      case 'malformed': return {line: text(env ? 'S-KEY-MALFORMED-ENV' : 'S-KEY-MALFORMED')};
      case 'rejected': return {line: '! ' + text('S-KEY-REJECTED'), error: true};
      case 'unreachable': return {line: '! ' + text('S-KEY-UNREACHABLE'), error: true, retry: true};
      // A pasted key is sent again with the button; a key from the environment has only Try again.
      case 'busy': return {line: '! ' + text('S-KEY-BUSY', wait), error: true, retry: env};
      case 'server_error': return {line: '! ' + text('S-KEY-SERVER-ERR', wait), error: true, retry: env};
      case 'save_failed': return {line: '! ' + text('S-KEY-SAVE-FAILED', {reason: outcome.reason || 'unknown error'}), error: true};
      case 'digger': return {line: '! ' + text('S-KEY-DIGGER-ERR', {reason: outcome.reason}), error: true, retry: env};
      case 'superseded': return {line: text('S-KEY-SUPERSEDED')};
      default: return null;  // 'env': the settings now name the variable, and the screen says how to change it
    }
  }
  function render(s) {
    if (destroyed) return;
    // Digger came back (perhaps restarted with another key in the variable): check the key again.
    if (s.ui.server === 'up' && serverWas !== 'up') envFor = null;
    serverWas = s.ui.server;
    const key = selectors.cosineKey(s);
    // A notice, not a gate: a returning user without a key still opens and plays saved digs.
    notice.hidden = !(s.ui.route.page === 'dig' && key.known && !key.set && !selectors.keyGate(s) && s.ui.server === 'up');
    const active = s.ui.route.page === 'connect';
    if (!active) {
      if (wasActive) { input.value = ''; result = null; envFor = null; }
      wasActive = false;
      return;
    }
    const env = envKey(s);
    if (!wasActive) {
      wasActive = true;
      result = null;
      envFor = null;
      if (!env) queueMicrotask(() => { if (!destroyed && store.getState().ui.route.page === 'connect') input.focus(); });
    }
    // A key from the environment can't be pasted over (A7): show it with its check result, checked
    // again when the variable names another key or Digger comes back (a restart may have changed it).
    const subject = env?.set ? `${env.env} ${env.hint}` : null;
    if (subject && subject !== envFor && s.ui.server === 'up') {
      envFor = subject;
      result = null;
      queueMicrotask(submit);
    }
    lede.hidden = !!env;
    field.hidden = goRow.hidden = privacy.hidden = !!env;
    envLine.hidden = !env;
    if (env) envLine.textContent = env.set ? text('S-KEY-ENV', {variable: env.env, last4: env.hint.replace(/^…/, '')}) :
      text('S-KEY-ENV-EMPTY', {variable: env.env});
    go.disabled = busy || s.ui.server !== 'up';
    go.textContent = text(busy ? 'S-KEY-CHECKING' : 'S-KEY-BTN');
    const shown = busy ? env ? {line: text('S-KEY-CHECKING')} : null : message(result, !!env);
    status.textContent = shown?.line || '';
    status.hidden = !shown;
    status.classList.toggle('connect-status--error', !!shown?.error);
    tryAgain.hidden = !shown?.retry;
  }
  const unsubscribe = store.subscribe(render);
  render(store.getState());
  return {destroy() {
    if (destroyed) return;
    destroyed = true;
    unsubscribe();
    input.value = '';
    root.replaceChildren();
    notice.replaceChildren();
    notice.hidden = true;
  }};
}
