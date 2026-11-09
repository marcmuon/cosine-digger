import {selectors} from './state.js';

const timeLabel = (seconds) => {
  const whole = Math.max(0, Math.floor(seconds || 0));
  const minutes = Math.floor(whole / 60), tail = String(whole % 60).padStart(2, '0');
  return minutes < 60 ? `${minutes}:${tail}` :
    `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, '0')}:${tail}`;
};

/** Execute store intents; routes never replace the visible YouTube player. */
export function mountPlayer(root, {store, clock, h, text, safeUrl}) {
  const frame = root.querySelector('#player-frame'), title = root.querySelector('#lcd-title');
  const from = root.querySelector('#lcd-from'), note = root.querySelector('#lcd-note');
  const transport = root.querySelector('#transport'), canvas = root.querySelector('#spectrum');
  const caption = h('span', {className: 'player-caption'}), time = h('span', {className: 'player-time'});
  title.replaceChildren(caption, time);
  const noteText = h('span'), noteActions = h('span', {className: 'player-note-actions'});
  note.replaceChildren(noteText, noteActions);
  let yt = null, ready = false, destroyed = false, scriptFailed = false;
  let handledIntent = null, loadedGeneration = -1, callbackGeneration = -1;
  let gestureTimer = null, loadTimer = null, refusalGeneration = -1, refusalAt = 0;
  let errorGeneration = -1, noteKey = null, spectrumKey = null;
  const removers = [];

  function listen(target, type, handler) {
    target.addEventListener(type, handler);
    removers.push(() => target.removeEventListener(type, handler));
  }
  function clearLoadTimers() {
    clock.clearTimeout(gestureTimer); clock.clearTimeout(loadTimer);
    gestureTimer = loadTimer = null;
  }
  function reading() {
    return {position: yt?.getCurrentTime?.() || 0, duration: yt?.getDuration?.() || 0};
  }
  function send(generation, type, extra = {}) {
    if (!destroyed) store.actions.playerEvent({generation, type, ...extra});
  }
  function matches(generation, nativePlaying = false) {
    const p = store.getState().player;
    return !destroyed && generation === p.generation && p.current &&
      (nativePlaying ? yt.getPlayerState() === 1 && yt.getVideoData()?.video_id === p.current.videoId :
        !['waiting', 'ended'].includes(p.phase));
  }
  // Capture identity when the API delivers the event, before any queued work.
  // Deferral also keeps synchronous SDK callbacks out of a store commit.
  function stateChanged(event) {
    const generation = callbackGeneration, value = event.data, position = reading();
    queueMicrotask(() => {
      if (!matches(generation, value === 1)) return;
      const p = store.getState().player;
      if (p.state === 'refused' || !p.current.videoId) return;
      if (value === 1 && document.hidden) {
        yt.pauseVideo(); send(generation, 'paused', position); return;
      }
      const type = {0: 'ended', 1: 'playing', 2: 'paused', 3: 'buffering', 5: 'cued'}[value];
      if (!type) return;
      // A late ENDED notification must agree with the current iframe state.
      if (value === 0 && yt.getPlayerState() !== 0) return;
      if (value === 5 && (p.phase === 'gesture' || p.gesture && p.state === 'loading')) return;
      if ([0, 1, 2, 5].includes(value)) clearLoadTimers();
      send(generation, type, position);
    });
  }
  function failed(event) {
    const generation = callbackGeneration, code = Number(event.data);
    queueMicrotask(() => {
      if (!matches(generation) || errorGeneration === generation || !store.getState().player.gesture) return;
      errorGeneration = generation;
      clearLoadTimers();
      send(generation, 'error', {code});
    });
  }
  function autoplayBlocked() {
    const generation = callbackGeneration;
    queueMicrotask(() => {
      if (matches(generation) && store.getState().player.state !== 'refused') {
        clearLoadTimers(); send(generation, 'gesture');
      }
    });
  }
  function armLoadTimers(generation) {
    clearLoadTimers();
    gestureTimer = clock.setTimeout(() => {
      if (matches(generation) && store.getState().player.state === 'loading' &&
          [-1, 5].includes(yt.getPlayerState())) send(generation, 'gesture');
    }, 1500);
    loadTimer = clock.setTimeout(() => {
      if (matches(generation) && !navigator.onLine && store.getState().player.state === 'loading')
        send(generation, 'offline');
    }, 10000);
  }
  function cueCurrent() {
    const p = store.getState().player;
    if (!ready || !p.current?.videoId) return;
    callbackGeneration = p.generation; loadedGeneration = p.generation;
    yt.cueVideoById({videoId: p.current.videoId, startSeconds: p.position});
  }
  function execute(intent) {
    if (!intent || intent.id === handledIntent) return;
    handledIntent = intent.id;
    const p = store.getState().player;
    if (intent.generation !== p.generation) return;
    if (['load', 'cue', 'stop'].includes(intent.type)) {
      clearLoadTimers(); callbackGeneration = intent.generation; errorGeneration = -1;
    }
    if (intent.type === 'stop') { if (ready) yt.stopVideo(); return; }
    if (!p.current?.videoId) { if (ready) yt.stopVideo(); return; }
    if (scriptFailed) { send(intent.generation, 'offline'); return; }
    if (!ready) {
      clearLoadTimers();
      loadTimer = clock.setTimeout(() => {
        if (matches(intent.generation) && !ready && !navigator.onLine) offline();
      }, 10000);
      if (intent.type !== 'cue') send(intent.generation, 'gesture');
      return;
    }
    if (intent.type === 'cue') { cueCurrent(); return; }
    if (intent.type === 'pause') { clearLoadTimers(); yt.pauseVideo(); return; }
    if (intent.type === 'seek') {
      // seekTo preserves PAUSED (2), but starts playback from CUED (5).
      if (p.state === 'paused' && yt.getPlayerState() !== 2) cueCurrent();
      else yt.seekTo(intent.position, true);
      return;
    }
    if (!['load', 'play'].includes(intent.type)) return;
    errorGeneration = -1;
    if (document.hidden) { cueCurrent(); send(intent.generation, 'gesture'); return; }
    armLoadTimers(intent.generation);
    if (loadedGeneration !== intent.generation) {
      callbackGeneration = intent.generation; loadedGeneration = intent.generation;
      yt.loadVideoById({videoId: p.current.videoId, startSeconds: p.position});
    }
    // This call remains inside the originating click/key; onReady only cues.
    yt.playVideo();
    const remembered = p.refusedByVideo[p.current.videoId];
    if (remembered) failed({data: remembered.code});
  }

  function play() {
    const s = store.getState();
    if (s.player.state === 'idle') {
      const target = selectors.judgingTarget(s);
      if (target) store.actions.playPick({pickId: target.pickId});
      else if (selectors.viewedStep(s)) store.actions.playStep(s.ui.route.stepId);
    } else store.actions.togglePlayback();
  }
  const buttons = [
    ['previous', '|◀◀', 'Previous track', () => store.actions.previous()],
    ['play', '▶', 'Play', play],
    ['next', '▶▶|', 'Next track', () => store.actions.next()],
    ['seek-back', '−15s', 'Back 15 seconds', () => store.actions.seek(-15)],
    ['seek-forward', '+15s', 'Forward 15 seconds', () => store.actions.seek(15)],
    ['seek-far', '+60s', 'Forward 60 seconds', () => store.actions.seek(60)],
  ].map(([action, label, name, handler]) => {
    // Silkscreen draws "s" like "5", so the seconds unit uses the body face (O17.6).
    const content = action.startsWith('seek') ? [label.slice(0, -1), h('span', {className: 'transport-unit'}, 's')] : [label];
    const button = h('button', {type: 'button', className: 'icon-btn',
      'data-action': action, 'aria-label': name, title: name}, ...content);
    listen(button, 'click', handler);
    return button;
  });
  transport.replaceChildren(...buttons);
  listen(title, 'click', () => store.actions.goPlaying());

  function endButtons(s) {
    const sourceStepId = s.player.queue.stepId;
    return selectors.whereNext(s, sourceStepId).map((choice) => {
      const pick = selectors.playingPick(s), point = choice.point;
      const params = {...choice.params, 'Artist - Title': pick ? `${pick.artist} - ${pick.title}` : ''};
      const name = text({'label': 'S-LCD-WN-LABEL-ARIA', 'person': 'S-LCD-WN-PERSON-ARIA',
        'track': 'S-LCD-WN-TRACK-ARIA', 'artist': 'S-MENU-A'}[choice.move], params);
      const label = choice.move === 'label' ? `■ ${point.name}` : choice.move === 'person' ? `◯ ${point.name}` :
        choice.move === 'track' ? '◆ this track' : text('S-LCD-WN-ARTIST', params);
      const button = h('button', {type: 'button', className: 'btn btn--small',
        'data-action': 'where-next', 'data-move': choice.move, 'aria-label': name,
        'aria-disabled': String(!!choice.disabled_reason), disabled: !!choice.disabled_reason,
        title: choice.disabled_reason ? text(choice.disabled_reason) : name}, label);
      button.addEventListener('click', () => {
        // Capture the playing source even while Feeds or another dig is viewed.
        if (point) {
          store.actions.startMove({parentStepId: sourceStepId, origins: [point], move: choice.move, further: false});
          return;
        }
        const origin = {
          point_id: `track:${pick.track_key}`, kind: 'track', name: `${pick.artist} - ${pick.title}`,
          artist: pick.artist, title: pick.title, track_key: pick.track_key, cosine_id: pick.cosine_id,
          video_id: pick.video_id, source_step_id: sourceStepId, source_rank: pick.rank,
        };
        store.actions.openMoveMenu(origin, {sourceStepId});
        store.actions.chooseMove(choice.move);
      });
      return button;
    });
  }
  function render(s) {
    const p = s.player, pick = selectors.playingPick(s), step = s.steps.byId[p.current?.stepId];
    const name = pick ? `${pick.artist ? pick.artist + ' - ' : ''}${pick.title}` : text('S-LCD-IDLE');
    const stopped = ['waiting', 'ended'].includes(p.phase);
    const glyph = stopped ? '■' : p.state === 'playing' ? '▶' : p.state === 'paused' ? '❚❚' :
      p.state === 'refused' ? '!' : p.state === 'loading' ? '▶ LOADING' : '';
    caption.textContent = `${glyph ? glyph + ' ' : ''}${name}`;
    time.textContent = pick && !['refused', 'offline'].includes(p.state) ?
      `${timeLabel(p.phase === 'ended' ? p.duration : p.position)} / ${timeLabel(p.duration)}` : '';
    title.title = name; title.disabled = p.state === 'idle';
    root.dataset.playerState = p.state; root.dataset.playerPhase = p.phase;
    if (step && p.state !== 'offline') {
      const elsewhere = s.ui.route.page !== 'dig' || s.ui.route.digId !== p.current.digId;
      from.textContent = text(elsewhere ? 'S-LCD-FROM-DIG' : 'S-LCD-FROM',
        {n: step.number, sentence: step.header, digName: s.digs.byId[p.current.digId]?.name || ''});
    } else from.textContent = p.state === 'idle' && !pick && s.ui.route.page === 'dig' && selectors.viewedDig(s) ?
      // Space needs a selected track (P16): until one is, point at what works without it.
      text(selectors.judgingTarget(s) ? 'S-LCD-IDLE-HINT' : 'S-LCD-IDLE-HINT-NOSEL') : '';
    from.title = from.textContent;
    const idle = p.state === 'idle';
    buttons.forEach((button, index) => {
      button.disabled = index === 1 ? idle && !selectors.judgingTarget(s) && !selectors.viewedStep(s) : idle;
      button.setAttribute('aria-disabled', String(button.disabled));
    });
    buttons[1].textContent = p.state === 'playing' ? '❚❚' : '▶';
    buttons[1].setAttribute('aria-label', p.state === 'playing' ? 'Pause' : 'Play');
    buttons[1].title = p.state === 'playing' ? 'Pause' : 'Play';

    let id = null, params = {}, actions = [];
    if (p.state === 'refused') {
      if (refusalGeneration !== p.generation) { refusalGeneration = p.generation; refusalAt = clock.now(); }
      id = !p.current?.videoId ? 'S-LCD-NOVIDEO' : p.phase === 'unavailable' ? 'S-LCD-UNAVAIL' : 'S-LCD-REFUSED';
      params.s = Math.max(0, Math.ceil((2000 - (clock.now() - refusalAt)) / 1000));
    } else {
      refusalGeneration = -1;
      if (p.state === 'offline') id = 'S-LCD-OFFLINE';
      else if (p.phase === 'gesture') id = 'S-LCD-GESTURE';
      else if (p.phase === 'waiting' && !selectors.isPicksComplete(s, p.queue.stepId)) {
        id = 'S-LCD-WAITING'; params.n = s.steps.byId[p.queue.stepId]?.number;
      } else if (p.phase === 'ended' || p.phase === 'waiting') {
        id = 'S-LCD-END'; params.n = s.steps.byId[p.queue.stepId]?.number;
      } else if (p.state === 'paused' && !p.gesture) id = 'S-LCD-RESTORED';
      else if (p.nextQueue) { id = 'S-LCD-UPNEXT'; params.n = s.steps.byId[p.nextQueue.stepId]?.number; }
    }
    noteText.textContent = id ? text(id, params) : '';
    note.dataset.stringId = id || '';
    note.classList.toggle('player-note-warning', ['refused', 'offline'].includes(p.state));
    const key = JSON.stringify([id, p.generation, p.queue.stepId, s.steps.byId[p.queue.stepId]?.hubs,
      s.ui.settings?.discogs_token?.set]);
    if (key !== noteKey) {
      noteKey = key;
      if (['refused', 'offline'].includes(p.state) && pick) {
        const href = safeUrl(p.current.videoId ? `https://music.youtube.com/watch?v=${encodeURIComponent(p.current.videoId)}` :
          `https://music.youtube.com/search?q=${encodeURIComponent(name)}`);
        actions = [h('a', {className: 'btn btn--small btn--yt', href, target: '_blank', rel: 'noopener'},
          p.current.videoId ? 'Open in YouTube Music' : 'Search YouTube Music')];
      } else if (id === 'S-LCD-END') actions = endButtons(s);
      noteActions.replaceChildren(...actions);
    }
    const nextSpectrumKey = `${p.state}:${s.ui.reducedMotion}`;
    if (nextSpectrumKey !== spectrumKey) { spectrumKey = nextSpectrumKey; drawSpectrum(s); }
    execute(p.intent);
  }

  const paint = canvas.getContext('2d');
  function drawSpectrum(s) {
    const tokens = getComputedStyle(root), playing = s.player.state === 'playing';
    const colors = [tokens.getPropertyValue('--lcd'), tokens.getPropertyValue('--amber'), tokens.getPropertyValue('--red')];
    paint.clearRect(0, 0, canvas.width, canvas.height);
    for (let bar = 0; bar < 24; bar++) {
      const level = !playing ? 1 : s.ui.reducedMotion ? 3 + bar % 7 :
        2 + Math.round(7 * (1 + Math.sin(clock.now() / 240 + bar * 1.7)) / 2);
      for (let row = 0; row < 11; row++) {
        paint.globalAlpha = row < level ? 1 : 0.07;
        paint.fillStyle = colors[row > 8 ? 2 : row > 5 ? 1 : 0];
        paint.fillRect(3 + bar * (canvas.width - 6) / 24, canvas.height - 4 - row * 4.5,
          (canvas.width - 6) / 24 - 2, 3);
      }
    }
    paint.globalAlpha = 1;
  }

  function offline() {
    scriptFailed = true; clearLoadTimers();
    if (!frame.querySelector('iframe')) frame.querySelector('#player').textContent = text('S-VID-NOSIGNAL');
    send(store.getState().player.generation, 'offline');
  }
  function createPlayer() {
    if (destroyed || yt || !window.YT?.Player) return;
    yt = new window.YT.Player(frame.querySelector('#player'), {width: 200, height: 200,
      playerVars: {controls: 1, autoplay: 0, playsinline: 1, rel: 0, enablejsapi: 1, origin: location.origin},
      events: {onReady(event) {
        if (destroyed) return;
        yt = event.target; ready = true; scriptFailed = false; clearLoadTimers();
        yt.getIframe().tabIndex = -1;
        const p = store.getState().player;
        if (p.current && p.state === 'offline') send(p.generation, 'gesture');
        cueCurrent();
      }, onStateChange: stateChanged, onError: failed, onAutoplayBlocked: autoplayBlocked},
    });
    yt.getIframe().tabIndex = -1;
  }
  const previousReady = window.onYouTubeIframeAPIReady;
  const apiReady = () => { previousReady?.(); createPlayer(); };
  let script = null;
  if (window.YT?.Player) createPlayer();
  else {
    window.onYouTubeIframeAPIReady = apiReady;
    script = h('script', {src: 'https://www.youtube.com/iframe_api'});
    listen(script, 'error', offline);
    document.head.append(script);
  }
  listen(document, 'visibilitychange', () => {
    const p = store.getState().player;
    if (!document.hidden || !p.current || p.state === 'idle' || p.state === 'refused') return;
    clearLoadTimers();
    if (ready) yt.pauseVideo();
    send(p.generation, 'paused', ready ? reading() : {});
  });
  const unsubscribe = store.subscribe(render);
  render(store.getState());
  const ticker = clock.setInterval(() => {
    const s = store.getState();
    if (ready && s.player.current && !['idle', 'refused', 'offline'].includes(s.player.state))
      send(s.player.generation, 'time', reading());
    else if (s.player.state === 'refused') render(s);
    if (s.player.state === 'playing' && !s.ui.reducedMotion && !document.hidden && canvas.getBoundingClientRect().width)
      drawSpectrum(s);
  }, 100);

  return {destroy() {
    if (destroyed) return;
    destroyed = true; unsubscribe(); clearLoadTimers(); clock.clearInterval(ticker);
    removers.forEach((remove) => remove());
    if (window.onYouTubeIframeAPIReady === apiReady) window.onYouTubeIframeAPIReady = previousReady;
    script?.remove(); yt?.destroy();
    frame.replaceChildren(h('div', {id: 'player'}));
  }};
}
