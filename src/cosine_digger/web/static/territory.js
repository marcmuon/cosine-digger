import {selectors, selectionId} from './state.js';
import {drawStep, hitStep, stepAnchor} from './stepmap.js';
import {formatWhen} from './dom.js';

const FULL_RADIUS = 160, COLLAPSED_RADIUS = 60, CAMERA_MS = 300;
const TOKENS = ['node-pick', 'node-label', 'node-person', 'node-seed', 'mark-selected',
  'mark-playing', 'mark-kept', 'mark-passed', 'panel', 'text', 'muted', 'line',
  'st-pending', 'st-error', 'st-info', 'focus'];
const nameOf = (pick) => pick.artist ? `${pick.artist} - ${pick.title}` : pick.title;
const pointOf = (pick) => ({point_id: `track:${pick.track_key}`, kind: 'track', name: nameOf(pick),
  track_key: pick.track_key, artist: pick.artist, title: pick.title, cosine_id: pick.cosine_id,
  video_id: pick.video_id, source_step_id: pick.step_id, source_rank: pick.rank});
const hubPoint = (hub, stepId) => ({point_id: hub.hub_key, kind: hub.kind, name: hub.name,
  discogs_id: hub.discogs_id, source_step_id: stepId, source_rank: null});

// These are projections of saved geometry, never a second layout or dig model.
function polar(step, layout, radius) {
  return {x: step.island_x + layout.radius * radius * Math.sin(layout.angle),
    y: step.island_y - layout.radius * radius * Math.cos(layout.angle)};
}
function originAt(scene, index) {
  const angle = Math.PI * 2 * (index + 0.5) / scene.originPoints.length;
  return {x: scene.step.island_x + (scene.originPoints.length > 1 ? 18 * Math.sin(angle) : 0),
    y: scene.step.island_y - (scene.originPoints.length > 1 ? 18 * Math.cos(angle) : 0)};
}
function sourceAt(scene, point, radius, sourceToken) {
  if (sourceToken) {
    const origin = scene.originPoints.findIndex((item) => selectionId(item) === sourceToken);
    if (origin >= 0) return originAt(scene, origin);
    const factRank = sourceToken.startsWith('fact:') ? Number(sourceToken.split(':')[1]) : null;
    const pick = scene.picks.find((item) => item.rank === factRank ||
      selectionId(pointOf(item)) === sourceToken && (point?.source_rank == null || item.rank === point.source_rank));
    if (pick) return polar(scene.step, pick.layout, radius);
    const hub = scene.hubs.find((item) => item.hub_key === sourceToken);
    return hub ? polar(scene.step, hub.layout, radius) : {x: scene.step.island_x, y: scene.step.island_y};
  }
  const token = selectionId(point);
  const origin = scene.originPoints.findIndex((item) => selectionId(item) === token);
  if (origin >= 0) return originAt(scene, origin);
  const pick = point?.source_rank != null && scene.picks.find((item) => item.rank === point.source_rank);
  if (pick) return polar(scene.step, pick.layout, radius);
  const hub = scene.hubs.find((item) => item.hub_key === token);
  if (hub) return polar(scene.step, hub.layout, radius);
  // Saved branch provenance has no UI origin flag. Resolve that legacy form
  // only after checking its pick/fact/hub identity.
  const index = scene.originPoints.findIndex((item) => selectionId(item) === selectionId(point && {...point, is_origin: true}));
  return index >= 0 ? originAt(scene, index) : {x: scene.step.island_x, y: scene.step.island_y};
}
function circle(ctx, p, radius) {
  ctx.beginPath(); ctx.arc(p.x, p.y, radius, 0, Math.PI * 2); ctx.stroke();
}
function star(ctx, p) {
  ctx.beginPath();
  for (let i = 0; i < 10; i++) {
    const angle = -Math.PI / 2 + i * Math.PI / 5, radius = i % 2 ? 3 : 7;
    const x = p.x + Math.cos(angle) * radius, y = p.y + Math.sin(angle) * radius;
    if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y);
  }
  ctx.closePath(); ctx.fill();
}

export function mountTerritory(root, {store, h, text, clock}) {
  const title = h('h2', {id: 'territory-title', className: 'panel__title'}, text('S-TERR-TITLE'));
  const tools = h('div', {className: 'territory-tools'});
  for (const [action, label, name] of [['zoom-out', '−', 'Zoom out'], ['zoom-in', '+', 'Zoom in'], ['fit', 'Fit']])
    tools.append(h('button', {type: 'button', className: 'btn btn--small', tabindex: -1,
      'data-action': action, 'aria-label': name}, label));
  const canvas = h('canvas', {id: 'territory-canvas', tabindex: 0, 'aria-labelledby': 'territory-title',
    'aria-describedby': 'territory-caption'});
  const caption = h('p', {id: 'territory-caption', role: 'status', hidden: true});
  const from = h('button', {type: 'button', className: 'territory-from', tabindex: -1, 'data-action': 'from', hidden: true});
  const tooltip = h('div', {className: 'territory-tooltip', role: 'tooltip', hidden: true});
  root.classList.add('territory');
  root.replaceChildren(h('header', {className: 'panel__bar'}, title, tools),
    h('div', {className: 'territory-surface'}, canvas, caption, from, tooltip));
  const ctx = canvas.getContext('2d'), motion = matchMedia('(prefers-reduced-motion: reduce)');
  const disposers = [], arrivals = new Map();
  let scenes = [], recurring = new Map(), labels = [], openId = null, visible = false, colors;
  let width = 0, height = 0, dpr = 0, density = null;
  let camera = {scale: 1, tx: 0, ty: 0}, transition = null, fitMode = 'island';
  let frame = null, drag = null, dragged = false, pointer = null, lastIslandClick = null, destroyed = false;
  const reduced = () => motion.matches || store.getState().ui.reducedMotion;
  const radiusOf = (scene) => scene.step.id === openId ? FULL_RADIUS : COLLAPSED_RADIUS;
  const project = (p) => ({x: camera.tx + camera.scale * p.x, y: camera.ty + camera.scale * p.y});
  const centre = (scene) => project({x: scene.step.island_x, y: scene.step.island_y});
  const openScene = () => scenes.find((scene) => scene.step.id === openId);
  function listen(target, event, handler, options) {
    target.addEventListener(event, handler, options);
    disposers.push(() => target.removeEventListener(event, handler, options));
  }
  function targetCamera(whole) {
    const included = whole ? scenes : [openScene()].filter(Boolean);
    if (!included.length || !width || !height) return camera;
    const x0 = Math.min(...included.map((scene) => scene.step.island_x - radiusOf(scene)));
    const x1 = Math.max(...included.map((scene) => scene.step.island_x + radiusOf(scene)));
    const y0 = Math.min(...included.map((scene) => scene.step.island_y - radiusOf(scene)));
    const y1 = Math.max(...included.map((scene) => scene.step.island_y + radiusOf(scene)));
    const scale = Math.min(6, Math.max(0.01, Math.min((width - 64) / (x1 - x0), (height - 96) / (y1 - y0))));
    return {scale, tx: width / 2 - (x0 + x1) / 2 * scale,
      ty: (height - 32) / 2 - (y0 + y1) / 2 * scale};
  }
  function fit(whole, animate = false) {
    fitMode = whole ? 'dig' : 'island';
    const target = targetCamera(whole);
    transition = animate && !reduced() ? {from: {...camera}, target, at: clock.now()} : null;
    if (!transition) camera = target;
    paint();
  }
  function zoom(x, y, factor) {
    transition = null; fitMode = null;
    const scale = Math.max(0.01, Math.min(6, camera.scale * factor));
    camera = {scale, tx: x - (x - camera.tx) / camera.scale * scale,
      ty: y - (y - camera.ty) / camera.scale * scale};
    paint();
  }
  function activeHub() {
    const s = store.getState(), point = s.ui.hover?.point;
    return [point, openScene()?.selection].find((item) => ['label', 'person'].includes(item?.kind) &&
      selectionId(item) === item.point_id)?.point_id || null;
  }
  function line(a, b, color, alpha, dotted = false) {
    ctx.strokeStyle = color; ctx.globalAlpha = alpha; ctx.lineWidth = 1;
    ctx.setLineDash(dotted ? [1, 3] : []);
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
    ctx.setLineDash([]); ctx.globalAlpha = 1;
  }
  function drawLinks(hubKey) {
    const path = new Set();
    if (openScene()?.selection?.source_rank != null) {
      let scene = openScene();
      while (scene?.step.parent_id && !path.has(scene.step.id)) {
        path.add(scene.step.id); scene = scenes.find((item) => item.step.id === scene.step.parent_id);
      }
    }
    for (const child of scenes) {
      const parent = scenes.find((scene) => scene.step.id === child.step.parent_id);
      if (!parent) continue; // Parent-null re-runs never get a fabricated branch.
      const start = sourceAt(parent, child.step.context?.source_point, radiusOf(parent),
        child.step.context?.source_selection_id);
      line(project(start), centre(child), colors[path.has(child.step.id) ? '--text' : '--muted'],
        path.has(child.step.id) ? 1 : 0.6, true);
    }
    for (const [key, appearances] of recurring) {
      for (let i = 1; i < appearances.length; i++) {
        const a = appearances[i - 1], b = appearances[i];
        line(project(polar(a.scene.step, a.hub.layout, radiusOf(a.scene))),
          project(polar(b.scene.step, b.hub.layout, radiusOf(b.scene))),
          colors[`--node-${a.hub.kind}`], key === hubKey ? 1 : 0.25);
      }
    }
  }
  function drawCollapsed(scene, hubKey) {
    const c = centre(scene);
    ctx.strokeStyle = colors['--line']; ctx.lineWidth = 1; ctx.setLineDash([1, 3]);
    circle(ctx, c, COLLAPSED_RADIUS * camera.scale); ctx.setLineDash([]);
    ctx.fillStyle = colors['--node-seed'];
    scene.originPoints.forEach((_, i) => star(ctx, project(originAt(scene, i))));
    let animated = false;
    function fade(id) {
      const key = `${scene.step.id}:${id}`, now = clock.now();
      if (!arrivals.has(key)) arrivals.set(key, scene.step.status === 'running' && !reduced() ? now : now - 200);
      const alpha = reduced() ? 1 : Math.min(1, (now - arrivals.get(key)) / 200);
      if (alpha < 1) animated = true;
      return alpha;
    }
    for (const pick of scene.picks) {
      const kept = scene.marks[pick.id]?.kept, playing = scene.playing?.id === pick.id;
      if (!kept && !playing) continue;
      const p = project(polar(scene.step, pick.layout, COLLAPSED_RADIUS));
      const alpha = fade(pick.id); ctx.globalAlpha = alpha;
      if (kept) { ctx.fillStyle = colors['--mark-kept']; star(ctx, p); }
      else { ctx.fillStyle = colors['--node-pick']; ctx.beginPath(); ctx.arc(p.x, p.y, 4, 0, Math.PI * 2); ctx.fill(); }
      if (playing) {
        ctx.strokeStyle = colors['--mark-playing']; ctx.lineWidth = 1.5;
        if (reduced() || scene.pulsePlaying === false) { circle(ctx, p, 9); circle(ctx, p, 12); }
        else {
          const phase = (clock.now() % 1400) / 1400;
          ctx.globalAlpha = alpha * (1 - phase); circle(ctx, p, 10 + phase * 6); animated = true;
        }
      }
    }
    ctx.globalAlpha = 1;
    for (const hub of scene.hubs.filter((item) => recurring.has(item.hub_key))) {
      const p = project(polar(scene.step, hub.layout, COLLAPSED_RADIUS));
      ctx.globalAlpha = fade(hub.hub_key);
      ctx.fillStyle = colors[`--node-${hub.kind}`];
      if (hub.kind === 'label') ctx.fillRect(p.x - 6, p.y - 6, 12, 12);
      else {
        ctx.fillStyle = colors['--panel']; ctx.beginPath(); ctx.arc(p.x, p.y, 6, 0, Math.PI * 2); ctx.fill();
        ctx.strokeStyle = colors['--node-person']; ctx.lineWidth = 2; circle(ctx, p, 6);
      }
      if (hub.hub_key === hubKey) { ctx.strokeStyle = colors['--mark-selected']; ctx.lineWidth = 1.5; circle(ctx, p, 10); }
    }
    ctx.globalAlpha = 1;
    return animated;
  }
  function drawIslandLabels() {
    labels = [];
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    for (const scene of [...scenes].sort((a, b) => (b.step.id === openId) - (a.step.id === openId))) {
      const c = centre(scene), radius = radiusOf(scene) * camera.scale;
      const s = store.getState(), step = scene.step;
      const playing = s.player.state !== 'idle' && s.player.current?.stepId === step.id;
      const status = step.status === 'failed' ? '!' : step.status === 'stopped' ? '‖' :
        !selectors.isPicksComplete(s, step.id) ? '~' : step.rerun_of ? 'changed' : '';
      const prefix = `${playing ? '▶ ' : ''}${step.number} · `;
      let name = step.origins.map((point) => point.name).join(', ');
      const maxWidth = Math.max(64, Math.min(220, 300 * camera.scale));
      const suffix = status ? ` ${status}` : '';
      while (name.length > 1 && ctx.measureText(prefix + name + suffix).width > maxWidth)
        name = name.slice(0, name.endsWith('…') ? -2 : -1) + '…';
      const label = prefix + name + suffix, tw = ctx.measureText(label).width;
      const candidates = [c.y + radius + 16, c.y - radius - 16, c.y + radius + 32, c.y - radius - 32];
      const x = Math.max(tw / 2 + 4, Math.min(width - tw / 2 - 4, c.x));
      const y = candidates.find((y) => y >= 8 && y <= height - 8 && !labels.some((box) =>
        Math.abs(box.y - y) < 16 && Math.abs(box.x - x) < (box.width + tw) / 2 + 4)) ?? candidates[0];
      ctx.lineWidth = 3; ctx.strokeStyle = colors['--panel']; ctx.strokeText(label, x, y);
      ctx.fillStyle = colors[playing ? '--mark-playing' : status === '!' ? '--st-error' :
        status === '~' ? '--st-pending' : status === 'changed' ? '--st-info' : '--text'];
      ctx.fillText(label, x, y);
      labels.push({scene, x, y, width: tw});
    }
    ctx.textAlign = 'start'; ctx.textBaseline = 'alphabetic';
  }
  function paint() {
    if (frame != null) { cancelAnimationFrame(frame); frame = null; }
    if (destroyed || !visible || !width || !height) return;
    if (transition) {
      const progress = reduced() ? 1 : Math.min(1, (clock.now() - transition.at) / CAMERA_MS);
      const ease = 1 - (1 - progress) ** 3;
      camera = Object.fromEntries(['scale', 'tx', 'ty'].map((key) => [key,
        transition.from[key] + (transition.target[key] - transition.from[key]) * ease]));
      if (progress === 1) transition = null;
    }
    ctx.clearRect(0, 0, width, height); ctx.fillStyle = colors['--panel']; ctx.fillRect(0, 0, width, height);
    const style = getComputedStyle(canvas); ctx.font = `${style.fontSize} ${style.fontFamily}`;
    const hubKey = activeHub(); drawLinks(hubKey);
    let animated = !!transition;
    for (const scene of scenes) {
      if (scene.step.id === openId) {
        scene.reducedMotion = reduced();
        const sharedHover = store.getState().ui.hover;
        const hoveredHub = sharedHover?.stepId !== openId && scene.hubs.find((hub) =>
          hub.hub_key === selectionId(sharedHover?.point));
        if (hoveredHub) scene.hover = hubPoint(hoveredHub, openId);
        animated = drawStep(ctx, scene, {cx: scene.step.island_x, cy: scene.step.island_y,
          R: FULL_RADIUS, ...camera}) || animated;
        if (hoveredHub) {
          ctx.strokeStyle = colors['--mark-selected']; ctx.lineWidth = 1.5;
          circle(ctx, project(polar(scene.step, hoveredHub.layout, FULL_RADIUS)), 10);
        }
      } else animated = drawCollapsed(scene, hubKey) || animated;
    }
    drawIslandLabels();
    const open = openScene();
    if (open) {
      const p = project({x: open.step.island_x - FULL_RADIUS, y: open.step.island_y - FULL_RADIUS});
      from.style.left = `${p.x}px`; from.style.top = `${p.y}px`;
    }
    if (animated) frame = requestAnimationFrame(() => { frame = null; paint(); });
  }
  function resize() {
    if (destroyed) return;
    const rect = canvas.getBoundingClientRect(), nextDpr = window.devicePixelRatio || 1;
    if (!rect.width || !rect.height || rect.width === width && rect.height === height && nextDpr === dpr) return;
    const sizeChanged = rect.width !== width || rect.height !== height;
    width = rect.width; height = rect.height;
    dpr = nextDpr;
    canvas.width = Math.round(width * dpr); canvas.height = Math.round(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (fitMode && sizeChanged) { camera = targetCamera(fitMode === 'dig'); transition = null; }
    paint();
  }
  function watchDensity() {
    if (destroyed) return;
    density?.removeEventListener('change', watchDensity);
    density = matchMedia(`(resolution: ${window.devicePixelRatio || 1}dppx)`);
    density.addEventListener('change', watchDensity);
    resize();
  }
  function readColors() {
    const css = getComputedStyle(document.documentElement);
    colors = Object.fromEntries(TOKENS.map((token) => ['--' + token, css.getPropertyValue('--' + token).trim()]));
    scenes.forEach((scene) => { scene.colors = colors; }); paint();
  }
  function local(event) {
    const rect = canvas.getBoundingClientRect();
    return {x: event.clientX - rect.left, y: event.clientY - rect.top};
  }
  function hitAt(p) {
    const open = openScene();
    let nearest = null, distance = Infinity;
    // Reverse the actual island/node paint order so equal distances keep the top target.
    for (const scene of [...scenes].reverse()) {
      if (scene === open) {
        const hit = hitStep(scene, p.x, p.y);
        if (hit && hit.distance < distance) { distance = hit.distance; nearest = {scene, ...hit}; }
        continue;
      }
      const candidates = scene.originPoints.map((point, i) => ({at: originAt(scene, i),
        point: {...point, source_step_id: scene.step.id, source_rank: null, is_origin: true}, rank: null, radius: 14}));
      scene.picks.filter((pick) => scene.marks[pick.id]?.kept || scene.playing?.id === pick.id).forEach((pick) =>
        candidates.push({at: polar(scene.step, pick.layout, COLLAPSED_RADIUS), point: pointOf(pick), rank: pick.rank, radius: 12}));
      scene.hubs.filter((hub) => recurring.has(hub.hub_key)).forEach((hub) =>
        candidates.push({at: polar(scene.step, hub.layout, COLLAPSED_RADIUS), point: hubPoint(hub, scene.step.id), rank: null, radius: 14}));
      for (const node of candidates.reverse()) {
        const q = project(node.at), d = Math.hypot(p.x - q.x, p.y - q.y);
        if (d <= node.radius && d < distance) { distance = d; nearest = {scene, point: node.point, rank: node.rank}; }
      }
    }
    if (nearest) return nearest;
    const label = labels.find((box) => Math.abs(p.x - box.x) <= box.width / 2 + 4 && Math.abs(p.y - box.y) <= 12);
    if (label) return {scene: label.scene, point: null, rank: null};
    const island = [...scenes].reverse().find((scene) => {
      const c = centre(scene); return Math.hypot(p.x - c.x, p.y - c.y) <= radiusOf(scene) * camera.scale;
    });
    return island ? {scene: island, point: null, rank: null} : null;
  }
  function showTooltip(hit) {
    tooltip.hidden = !hit || !pointer;
    if (tooltip.hidden) return;
    const {scene, point, rank} = hit, step = scene.step;
    const hub = scene.hubs.find((item) => item.hub_key === selectionId(point));
    const pick = rank != null && scene.picks.find((item) => item.rank === rank);
    if (point?.is_origin) tooltip.textContent = point.name;
    else if (pick) tooltip.textContent = text('S-MAP-TIP-PICK', {'Artist - Title': nameOf(pick),
      '0.79': Number.isFinite(pick.score) ? pick.score.toFixed(2) : '—'}) +
      (pick.facts?.labels?.length ? ` · on ${pick.facts.labels.map((label) => label.name).join(', ')}` : '');
    else if (hub) {
      const roles = [...new Set(scene.picks.filter((item) => hub.pick_ranks.includes(item.rank))
        .flatMap((item) => item.facts?.credits?.filter((credit) => (credit.id ? `person:${credit.id}` :
          `person:name:${credit.name.trim().toLowerCase().replace(/\s*\(\d+\)\s*$/, '').replace(/\s+/g, ' ').trim()}`) === hub.hub_key)
          .flatMap((credit) => credit.roles) || []))];
      tooltip.textContent = text(hub.kind === 'label' ? 'S-MAP-TIP-LABEL' : 'S-MAP-TIP-PERSON',
        {Label: hub.name, Person: hub.name, k: hub.pick_ranks.length, roles: roles.join(', ')});
      const others = recurring.get(hub.hub_key)?.filter((item) => item.scene.step.id !== step.id).map((item) => item.scene.step.number);
      if (others?.length) tooltip.textContent += ` · also in step ${others.join(', ')}`;
    } else tooltip.textContent = text('S-TR-TIP', {Sentence: step.header, c: scene.picks.length,
      k: scene.picks.filter((pick) => scene.marks[pick.id]?.kept).length,
      p: scene.picks.filter((pick) => scene.marks[pick.id]?.passed).length, 'HH:MM': formatWhen(step.started_at)});
    tooltip.style.left = `${Math.max(4, Math.min(width - tooltip.offsetWidth - 4, pointer.x + 14))}px`;
    tooltip.style.top = `${Math.max(4, Math.min(height - tooltip.offsetHeight - 4, pointer.y + 14))}px`;
  }
  function report(error) { if (!destroyed) store.actions.showToast('S-T-MOVE-ERR', {reason: error.message}); }
  listen(root, 'click', (event) => {
    const action = event.target.closest('[data-action]')?.dataset.action;
    if (!action || event.detail > 1) return;
    if (action === 'from') Promise.resolve(store.actions.goParent()).catch(report);
    else if (action === 'fit') fit(true);
    else zoom(width / 2, height / 2, action === 'zoom-in' ? 1.25 : 0.8);
  });
  listen(canvas, 'focus', () => store.actions.focusRegion('territory'));
  listen(canvas, 'map-menu-anchor', (event) => {
    event.detail.anchor = stepAnchor(openScene(), event.detail.point, canvas);
  });
  listen(canvas, 'keydown', (event) => {
    if (!['+', '=', '-', '_', '0'].includes(event.key) || event.ctrlKey || event.metaKey || event.altKey) return;
    event.preventDefault(); event.stopPropagation();
    if (event.key === '0') fit(true);
    else zoom(width / 2, height / 2, ['+', '='].includes(event.key) ? 1.25 : 0.8);
  });
  listen(canvas, 'wheel', (event) => {
    event.preventDefault(); const p = local(event), unit = event.deltaMode === 1 ? 32 : event.deltaMode === 2 ? 400 : 1;
    zoom(p.x, p.y, Math.exp(-event.deltaY * unit * (event.ctrlKey ? 0.01 : 0.0018)));
  }, {passive: false});
  listen(canvas, 'pointerdown', (event) => {
    if (event.button !== 0) return;
    transition = null; const p = local(event);
    drag = {id: event.pointerId, ...p, camera: {...camera}}; dragged = false;
    canvas.setPointerCapture(event.pointerId); canvas.focus({preventScroll: true});
  });
  listen(canvas, 'pointermove', (event) => {
    const p = local(event);
    if (drag) {
      if (drag.id !== event.pointerId || !dragged && Math.hypot(p.x - drag.x, p.y - drag.y) < 4) return;
      dragged = true; fitMode = null; pointer = null; tooltip.hidden = true;
      camera = {...drag.camera, tx: drag.camera.tx + p.x - drag.x, ty: drag.camera.ty + p.y - drag.y};
      canvas.classList.add('is-dragging'); paint(); return;
    }
    pointer = p; const hit = hitAt(p), current = store.getState().ui.hover;
    const point = hit && (hit.point || {point_id: hit.scene.step.id, kind: 'step', name: hit.scene.step.header});
    canvas.classList.toggle('is-over-island', !!hit);
    if (current?.stepId !== hit?.scene.step.id || selectionId(current?.point) !== selectionId(point) ||
        current?.point?.source_rank !== point?.source_rank)
      store.actions.hoverPoint(hit?.scene.step.id || openId, point || null);
    showTooltip(hit);
  });
  function endDrag(event) {
    if (drag?.id !== event.pointerId) return;
    drag = null; canvas.classList.remove('is-dragging');
    if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
  }
  listen(canvas, 'pointerup', endDrag);
  listen(canvas, 'pointercancel', (event) => { dragged = true; endDrag(event); });
  listen(canvas, 'pointerleave', () => {
    pointer = null; tooltip.hidden = true; canvas.classList.remove('is-over-island');
    if (store.getState().ui.hover) store.actions.hoverPoint(openId, null);
  });
  listen(canvas, 'click', (event) => {
    if (dragged || event.detail > 1 || !openId) return;
    const hit = hitAt(local(event));
    if (hit && hit.scene.step.id !== openId) {
      lastIslandClick = clock.now();
      Promise.resolve(store.actions.openRoute({page: 'dig', digId: hit.scene.step.dig_id,
        stepId: hit.scene.step.id, view: 'map', ...(hit.point ? {sel: selectionId(hit.point)} : {})})).catch(report);
    } else { lastIslandClick = null; store.actions.selectPoint(openId, hit?.point || null); }
  });
  listen(canvas, 'dblclick', (event) => {
    if (dragged || !openId || lastIslandClick != null && clock.now() - lastIslandClick < 500) return;
    const hit = hitAt(local(event));
    if (!hit) fit(true);
    else if (hit.scene.step.id === openId && hit.rank != null)
      store.actions.playPick({pickId: `${openId}:${hit.rank}`});
  });
  listen(motion, 'change', paint);
  function render(s) {
    const step = selectors.viewedStep(s), wasVisible = visible, previousId = openId;
    visible = s.ui.route.page === 'dig' && s.ui.route.view === 'map' && !!step;
    openId = step?.id || null;
    scenes = (s.steps.byDig[s.ui.route.digId] || []).map((id) => ({...selectors.hubScene(s, id), colors,
      pulsePlaying: s.player.state === 'playing',
      selection: id === openId ? selectors.selectedPoint(s, id) : null}));
    recurring = new Map();
    for (const scene of scenes) for (const hub of scene.hubs) {
      if (!recurring.has(hub.hub_key)) recurring.set(hub.hub_key, []);
      recurring.get(hub.hub_key).push({scene, hub});
    }
    for (const [key, appearances] of recurring) if (appearances.length < 2) recurring.delete(key);
    root.dataset.stepId = openId || ''; canvas.dataset.stepId = openId || '';
    if (step) {
      from.hidden = !step.parent_id; from.dataset.stepId = step.parent_id || '';
      from.textContent = text('S-MAP-FROM', {p: s.steps.byId[step.parent_id]?.number || ''});
      const open = openScene(), notes = [];
      if (!open.hubs.length && open.picks.length) notes.push(text('S-MAP-NOHUB', {
        loading: open.picks.some((pick) => pick.facts_state === 'loading'),
        i: open.picks.filter((pick) => pick.facts_state !== 'loading').length, n: open.picks.length}));
      const noToken = open.picks.some((pick) => pick.facts_state === 'no_token') ||
        s.ui.settings && !s.ui.settings.discogs_token?.set;
      const unavailable = open.picks.some((pick) => pick.facts_state === 'unavailable');
      const codes = new Set((open.step.note || []).map(note => note.code));
      const partial = !codes.has('discogs_unavailable') &&
        (codes.has('discogs_budget_exhausted') || codes.has('facts_interrupted'));
      // Without Discogs the map doesn't ask for a token; moves offer Discogs when asked for (P8).
      if (!noToken && unavailable) notes.push(text(partial ? 'S-MAP-FACTS-PARTIAL' : 'S-MAP-DISCOGS-DOWN'));
      caption.textContent = notes.join('\n'); caption.hidden = !notes.length;
      caption.style.color = unavailable && !noToken ? 'var(--st-warning)' : '';
    }
    if (visible) {
      resize();
      if (!wasVisible || previousId !== openId) {
        pointer = null; tooltip.hidden = true; fit(false, wasVisible && previousId !== openId);
        // Store notifications are synchronous: finish camera/scene work before focusing.
        if (document.activeElement !== canvas) canvas.focus({preventScroll: true});
        else if (s.ui.focusRegion !== 'territory') store.actions.focusRegion('territory');
      } else paint();
      if (pointer) showTooltip(hitAt(pointer));
    } else { transition = null; tooltip.hidden = true; paint(); }
  }
  const observer = new ResizeObserver(resize); observer.observe(canvas);
  const theme = new MutationObserver(readColors);
  theme.observe(document.documentElement, {attributes: true, attributeFilter: ['class', 'style']});
  const unsubscribe = store.subscribe(render);
  readColors(); render(store.getState()); watchDensity();
  // Chromium can change DPR without dispatching a resolution-change event.
  const densityTimer = setInterval(() => { if (!density.matches) watchDensity(); }, 250);
  document.fonts?.ready.then(() => { if (!destroyed) paint(); });
  return {destroy() {
    destroyed = true; unsubscribe(); observer.disconnect(); theme.disconnect();
    density.removeEventListener('change', watchDensity);
    clearInterval(densityTimer);
    if (frame != null) cancelAnimationFrame(frame);
    disposers.reverse().forEach((dispose) => dispose()); arrivals.clear(); root.replaceChildren(); root.classList.remove('territory');
  }};
}
