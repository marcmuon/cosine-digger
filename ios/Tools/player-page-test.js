// Checks the YouTube player page's script (in Digger/Player/YouTubePlayer.swift) against a fake
// IFrame API: queued commands, cue without play, the 15 s give-up, a script that won't load.
// Run with: node ios/Tools/player-page-test.js
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const swift = fs.readFileSync(path.join(__dirname, '..', 'Digger', 'Player', 'YouTubePlayer.swift'), 'utf8');
const js = swift.split('<script>')[1].split('</script>')[0]
  .split('\\(quoted(origin))').join('"https://com.marcmuon.cosinedigger"');
// The script sits in a Swift string literal: a backslash there would be a Swift escape, not JS.
if (js.includes('\\')) { console.log('FAIL the page script has a backslash; Swift would rewrite it'); process.exit(1); }

function run(scenario) {
  const posted = [];
  const timers = [];
  let now = 0;
  let appended = null;
  const calls = [];
  const ctx = {
    window: {},
    document: {
      createElement: () => ({}),
      head: { appendChild: (el) => { appended = el; } },
    },
    setTimeout: (fn, ms) => timers.push({ at: now + ms, fn, every: 0 }),
    setInterval: (fn, ms) => timers.push({ at: now + ms, fn, every: ms }),
  };
  ctx.window = ctx;
  ctx.webkit = { messageHandlers: { player: { postMessage: (m) => posted.push(m) } } };
  vm.createContext(ctx);
  vm.runInContext(js, ctx);
  const advance = (ms) => {
    const until = now + ms;
    for (;;) {
      timers.sort((a, b) => a.at - b.at);
      const next = timers.find((t) => t.at <= until);
      if (!next) break;
      now = next.at;
      if (next.every) next.at += next.every; else timers.splice(timers.indexOf(next), 1);
      next.fn();
    }
    now = until;
  };
  const youTube = (readyAfter) => {
    ctx.YT = {
      Player: function (id, options) {
        this.state = -1;
        this.loadVideoById = (o) => { calls.push(['load', o.videoId, o.startSeconds]); this.state = 1; };
        this.cueVideoById = (o) => { calls.push(['cue', o.videoId, o.startSeconds]); this.state = 5; };
        this.playVideo = () => calls.push(['play']);
        this.pauseVideo = () => calls.push(['pause']);
        this.stopVideo = () => calls.push(['stop']);
        this.getPlayerState = () => this.state;
        this.getCurrentTime = () => 12;
        this.getDuration = () => 200;
        if (readyAfter !== null) timers.push({ at: now + readyAfter, fn: () => options.events.onReady(), every: 0 });
      },
    };
    ctx.onYouTubeIframeAPIReady();
  };
  scenario({ ctx, posted, calls, advance, youTube, script: () => appended });
  return { posted, calls };
}

let failures = 0;
const check = (name, ok, detail) => { console.log((ok ? 'ok   ' : 'FAIL ') + name + (ok ? '' : ' ' + JSON.stringify(detail))); if (!ok) failures++; };

// 1. Ready quickly: queued commands run after ready, cue doesn't play, no give-up later.
let r = run(({ ctx, youTube, advance }) => {
  ctx.digger.cue('abcdefghijk', 30);
  youTube(500);
  advance(600);
  ctx.digger.load('jNQXAC9IVRw', 0);
  advance(20000);
});
check('cue queued before ready runs after ready', JSON.stringify(r.calls[0]) === JSON.stringify(['cue', 'abcdefghijk', 30]), r.calls);
check('load after ready runs at once', JSON.stringify(r.calls[1]) === JSON.stringify(['load', 'jNQXAC9IVRw', 0]), r.calls);
check('ready posted once', r.posted.filter((m) => m.type === 'ready').length === 1, r.posted);
check('no give-up once ready', !r.posted.some((m) => m.type === 'error'), r.posted);
check('time posted while playing', r.posted.some((m) => m.type === 'time' && m.position === 12 && m.duration === 200), r.posted);

// 2. YouTube never gets ready: one -2 after 15 s, and a late onReady is ignored.
r = run(({ ctx, youTube, advance }) => {
  ctx.digger.load('jNQXAC9IVRw', 0);
  youTube(16000);
  advance(30000);
});
check('gives up once at 15 s', r.posted.filter((m) => m.type === 'error' && m.value === -2).length === 1, r.posted);
check('says why it gave up', r.posted.some((m) => m.type === 'problem' && /15 seconds/.test(m.value)), r.posted);
check('late ready ignored', !r.posted.some((m) => m.type === 'ready') && r.calls.length === 0, r);

// 3. The script fails to load: one -2 now, none from the timer later.
r = run(({ script, advance }) => {
  script().onerror();
  advance(20000);
});
check('script failure gives up once', r.posted.filter((m) => m.type === 'error').length === 1, r.posted);
check('script failure says why', r.posted.some((m) => m.type === 'problem' && /did not load/.test(m.value)), r.posted);

// 4. Video errors pass through.
r = run(({ ctx, youTube, advance }) => {
  let options;
  ctx.YT = { Player: function (id, o) { options = o; this.getPlayerState = () => 5; } };
  ctx.onYouTubeIframeAPIReady();
  options.events.onReady();
  options.events.onError({ data: 150 });
  options.events.onStateChange({ data: 0 });
});
check('video error passes through', r.posted.some((m) => m.type === 'error' && m.value === 150), r.posted);
check('state change passes through', r.posted.some((m) => m.type === 'state' && m.value === 0), r.posted);

process.exit(failures ? 1 : 0);
