/* Served only by a harness route for YouTube's IFrame API; never shipped. */
(() => {
  const injectedClock = /* harness clock */ null;
  if (window.__sliceYTStub && !injectedClock) {
    queueMicrotask(() => window.onYouTubeIframeAPIReady?.());
    return;
  }
  function controlledClock() {
    let now = 0, serial = 0;
    const timers = new Map();
    const schedule = (fn, delay, interval = 0) => {
      const id = ++serial;
      timers.set(id, {fn, at: now + Math.max(0, delay || 0), interval});
      return id;
    };
    return Object.freeze({
      now: () => now,
      setTimeout: (fn, delay) => schedule(fn, delay),
      clearTimeout: id => timers.delete(id),
      setInterval: (fn, delay) => schedule(fn, Math.max(1, delay), Math.max(1, delay)),
      clearInterval: id => timers.delete(id),
      async advance(ms) {
        if (!Number.isFinite(ms) || ms < 0) throw new Error('Clock must advance monotonically');
        const target = now + ms;
        for (let count = 0; count < 100000; count++) {
          const next = [...timers].filter(([, t]) => t.at <= target)
            .sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
          if (!next) { now = target; return; }
          const [id, timer] = next;
          now = timer.at;
          if (timer.interval) timer.at += timer.interval; else timers.delete(id);
          await timer.fn();
        }
        throw new Error('Clock did not settle');
      },
    });
  }

  // Python harnesses may replace this expression with their explicit JS clock.
  // Production code never reads this global or an env/query clock override.
  const clock = injectedClock || controlledClock();
  const states = Object.freeze({UNSTARTED: -1, ENDED: 0, PLAYING: 1,
    PAUSED: 2, BUFFERING: 3, CUED: 5});
  const errors = Object.freeze({S9GONE00100: 100, S9REFUSE101: 101, Pso5YU_mntg: 150});
  const players = [], calls = [];

  class Player {
    constructor(target, options = {}) {
      const element = typeof target === 'string' ? document.getElementById(target) : target;
      if (!element) throw new Error('Player mount missing');
      this.options = options;
      this.listeners = new Map();
      this.state = states.UNSTARTED;
      this.videoId = options.videoId || '';
      this.position = 0;
      this.duration = 180;
      this.since = clock.now();
      this.generation = 0;
      this.errorGeneration = -1;
      this.destroyed = false;
      this.endTimer = null;
      this.iframe = document.createElement('iframe');
      this.iframe.id = element.id;
      this.iframe.width = Math.max(200, Number(options.width) || 200);
      this.iframe.height = Math.max(200, Number(options.height) || 200);
      this.iframe.title = 'YouTube video player (STUB)';
      this.iframe.tabIndex = -1;
      this.iframe.style.cssText = 'border:0;display:block';
      this.iframe.srcdoc = '<!doctype html><style>html,body{margin:0;width:100%;height:100%;background:#000}</style>';
      element.replaceWith(this.iframe);
      players.push(this);
      this.record('Player', {videoId: this.videoId, playerVars: options.playerVars || {}});
      queueMicrotask(() => {
        if (!this.destroyed) { this.record('onReady'); this.emit('onReady'); }
      });
    }
    record(method, args = {}) {
      calls.push({player: players.indexOf(this), method, args, at: clock.now(),
        videoId: this.videoId, gesture: !!navigator.userActivation?.isActive});
    }
    emit(name, data) {
      if (this.destroyed) return;
      const event = {target: this, data};
      this.options.events?.[name]?.(event);
      for (const listener of this.listeners.get(name) || []) listener(event);
    }
    setState(state) {
      this.position = this.getCurrentTime();
      this.since = clock.now();
      this.state = state;
      clock.clearTimeout(this.endTimer);
      if (state === states.PLAYING) {
        this.endTimer = clock.setTimeout(() => {
          this.setState(states.ENDED);
        }, Math.max(0, (this.duration - this.position) * 1000));
      }
      this.record('onStateChange', {state});
      this.emit('onStateChange', state);
    }
    fail() {
      const code = errors[this.videoId];
      if (!code) return false;
      if (this.errorGeneration !== this.generation) {
        this.errorGeneration = this.generation;
        this.setState(states.UNSTARTED);
        this.record('onError', {code});
        this.emit('onError', code);
      }
      return true;
    }
    video(method, input, startSeconds = 0) {
      const value = typeof input === 'string' ? {videoId: input, startSeconds} : input;
      this.generation++;
      const generation = this.generation;
      clock.clearTimeout(this.endTimer);
      this.videoId = value.videoId;
      this.position = Math.max(0, Math.min(this.duration, Number(value.startSeconds) || 0));
      this.since = clock.now();
      this.state = states.UNSTARTED;
      this.record(method, {videoId: this.videoId, startSeconds: this.position});
      this.setState(method === 'loadVideoById' ? states.BUFFERING : states.CUED);
      queueMicrotask(() => {
        if (this.destroyed || generation !== this.generation || this.fail()) return;
        if (method === 'loadVideoById' && this.state === states.BUFFERING) this.setState(states.PLAYING);
      });
    }
    loadVideoById(input, startSeconds) { this.video('loadVideoById', input, startSeconds); }
    cueVideoById(input, startSeconds) { this.video('cueVideoById', input, startSeconds); }
    playVideo() {
      this.record('playVideo');
      if (this.videoId && !this.fail()) this.setState(states.PLAYING);
    }
    pauseVideo() { this.record('pauseVideo'); this.setState(states.PAUSED); }
    stopVideo() { this.record('stopVideo'); this.setState(states.UNSTARTED); }
    seekTo(seconds, allowSeekAhead = true) {
      this.record('seekTo', {seconds, allowSeekAhead});
      this.position = Math.max(0, Math.min(this.duration, Number(seconds) || 0));
      this.since = clock.now();
      // O15: only PAUSED stays paused; seeking from CUED starts playing.
      // Reset the time origin so a seek never adds elapsed playing time.
      this.setState(this.state === states.PAUSED ? states.PAUSED : states.PLAYING);
    }
    getCurrentTime() {
      const elapsed = this.state === states.PLAYING ? (clock.now() - this.since) / 1000 : 0;
      return Math.min(this.duration, this.position + Math.max(0, elapsed));
    }
    getDuration() { return this.duration; }
    getPlayerState() { return this.state; }
    getIframe() { return this.iframe; }
    getVideoData() { return {video_id: this.videoId}; }
    addEventListener(name, fn) {
      if (!this.listeners.has(name)) this.listeners.set(name, new Set());
      this.listeners.get(name).add(fn);
    }
    removeEventListener(name, fn) { this.listeners.get(name)?.delete(fn); }
    destroy() {
      this.record('destroy');
      this.destroyed = true;
      this.generation++;
      clock.clearTimeout(this.endTimer);
      this.iframe.remove();
    }
  }
  window.__sliceYTStub = Object.freeze({clock, calls, players, errors,
    advance: ms => clock.advance(ms),
    setState: (state, index = 0) => players[index].setState(state)});
  window.YT = {Player, PlayerState: states, loaded: 1, ready: fn => queueMicrotask(fn)};
  queueMicrotask(() => window.onYouTubeIframeAPIReady?.());
})();
