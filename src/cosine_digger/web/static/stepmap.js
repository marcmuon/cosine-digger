import {selectors, selectionId} from './state.js';

const TAU = Math.PI * 2;
const MIN_SCALE = 0.2, MAX_SCALE = 6;
const TOKENS = ['node-pick', 'node-label', 'node-person', 'node-seed', 'mark-selected',
  'mark-playing', 'mark-kept', 'mark-passed', 'panel', 'text', 'muted', 'line', 'st-pending', 'focus'];
const hitFrames = new WeakMap();
const arrivals = new WeakMap();
const nameOf = (pick) => pick.artist ? `${pick.artist} - ${pick.title}` : pick.title;
const short = (value, limit) => value.length > limit ? value.slice(0, limit - 1) + '…' : value;
const pointOf = (pick) => ({point_id: `track:${pick.track_key}`, kind: 'track', name: nameOf(pick),
  track_key: pick.track_key, artist: pick.artist, title: pick.title, cosine_id: pick.cosine_id,
  video_id: pick.video_id, source_step_id: pick.step_id, source_rank: pick.rank});

function nodesFor(scene, viewport) {
  const {cx, cy, R, scale = 1, tx = 0, ty = 0} = viewport;
  const at = (x, y) => ({x: tx + scale * (cx + x), y: ty + scale * (cy + y)});
  const polar = (layout) => at(layout.radius * R * Math.sin(layout.angle),
    -layout.radius * R * Math.cos(layout.angle));
  const nodes = scene.picks.map((pick) => ({...polar(pick.layout), id: pick.id, kind: 'track',
    point: pointOf(pick), rank: pick.rank, data: pick, target: 12, size: 4}));
  for (const hub of scene.hubs) nodes.push({...polar(hub.layout), id: hub.hub_key, kind: hub.kind,
    point: {point_id: hub.hub_key, kind: hub.kind, name: hub.name, discogs_id: hub.discogs_id,
      source_step_id: scene.step.id, source_rank: null}, rank: null, data: hub, target: 14, size: 6});
  scene.originPoints.forEach((point, index) => {
    const angle = TAU * (index + 0.5) / scene.originPoints.length;
    nodes.push({...at(scene.originPoints.length > 1 ? 18 * Math.sin(angle) : 0,
      scene.originPoints.length > 1 ? -18 * Math.cos(angle) : 0), id: `origin:${point.point_id}`,
      kind: 'origin', point: {...point, source_step_id: scene.step.id, source_rank: null, is_origin: true},
      rank: null, data: point, target: 14, size: 7, angle});
  });
  return nodes;
}

function matches(point, node) {
  if (!point || selectionId(point) !== selectionId(node.point)) return false;
  return node.kind !== 'track' || point.source_rank === node.rank;
}

function circle(ctx, x, y, radius, fill = false) {
  ctx.beginPath();
  ctx.arc(x, y, radius, 0, TAU);
  if (fill) ctx.fill(); else ctx.stroke();
}

function square(ctx, node, colors) {
  ctx.strokeStyle = colors['--mark-selected'];
  ctx.lineWidth = 1.5;
  ctx.setLineDash([3, 2]);
  ctx.strokeRect(node.x - 9, node.y - 9, 18, 18);
  ctx.setLineDash([]);
}

function star(ctx, x, y, radius) {
  ctx.beginPath();
  for (let i = 0; i < 10; i++) {
    const angle = -Math.PI / 2 + i * Math.PI / 5, r = i % 2 ? radius * 0.45 : radius;
    const px = x + Math.cos(angle) * r, py = y + Math.sin(angle) * r;
    if (i) ctx.lineTo(px, py); else ctx.moveTo(px, py);
  }
  ctx.closePath();
  ctx.fill();
}

// The old map's right/left/below/above collision pass, applied only to text.
function drawLabels(ctx, labels, width, height, colors, {labelTop = 0, labelBottom = height} = {}) {
  const placed = [];
  ctx.textBaseline = 'middle';
  for (const label of labels) {
    const {node, text, forced, alpha, color = colors['--text']} = label;
    if (node.x < 0 || node.x > width || node.y < labelTop || node.y > labelBottom) continue;
    const tw = ctx.measureText(text).width, size = parseFloat(ctx.font) || 12;
    const spots = [[node.x + node.size + 5, node.y], [node.x - node.size - 5 - tw, node.y],
      [node.x - tw / 2, node.y + node.size + size / 2 + 2],
      [node.x - tw / 2, node.y - node.size - size / 2 - 2]];
    let chosen;
    for (const [sx, sy] of spots) {
      const x = Math.max(2, Math.min(width - tw - 2, sx));
      const y = Math.max(labelTop + size / 2 + 2, Math.min(labelBottom - size / 2 - 2, sy));
      const box = {x0: x - 2, y0: y - size / 2, x1: x + tw + 2, y1: y + size / 2};
      if (!placed.some((old) => box.x0 < old.x1 && box.x1 > old.x0 && box.y0 < old.y1 && box.y1 > old.y0)) {
        chosen = {x, y, box}; break;
      }
    }
    if (!chosen && !forced) continue;
    if (!chosen) chosen = {x: Math.max(2, Math.min(width - tw - 2, spots[0][0])),
      y: Math.max(labelTop + size / 2 + 2, Math.min(labelBottom - size / 2 - 2, node.y))};
    if (chosen.box) placed.push(chosen.box);
    ctx.globalAlpha = alpha;
    ctx.lineWidth = 3;
    ctx.strokeStyle = colors['--panel'];
    ctx.strokeText(text, Math.round(chosen.x), Math.round(chosen.y));
    ctx.fillStyle = color;
    ctx.fillText(text, Math.round(chosen.x), Math.round(chosen.y));
  }
}

/** Frozen shared draw/hit contract for the step map and territory.
 * Draw onto the caller's canvas without clearing it or changing scene data.
 * x = tx + scale * (cx + radius * R * sin(angle)), y uses -cos(angle).
 * Node sizes and hit radii are CSS pixels, independent of camera scale/DPR.
 * scene.colors keys are the exact CSS property names (e.g. '--node-pick').
 * scene.pulsePlaying=false keeps the retained current pick's rings static.
 * Returns true while another animation frame is needed. hitStep uses the same
 * scene object and the latest drawn viewport; its x/y are canvas CSS pixels.
 * The parent-return button belongs to the mounted panel, not selection hits.
 */
export function drawStep(ctx, scene, viewport) {
  const nodes = nodesFor(scene, viewport), colors = scene.colors;
  hitFrames.set(scene, nodes);
  const now = performance.now(), canvas = ctx.canvas;
  let history = arrivals.get(canvas);
  if (!history) { history = new Map(); arrivals.set(canvas, history); }
  let born = history.get(scene.step.id);
  if (!born) { born = new Map(); history.set(scene.step.id, born); }
  let animated = false;
  const fade = (node) => {
    if (!born.has(node.id)) born.set(node.id, scene.step.status === 'running' ? now : now - 200);
    const alpha = scene.reducedMotion ? 1 : Math.min(1, (now - born.get(node.id)) / 200);
    if (alpha < 1) animated = true;
    return alpha;
  };
  const opacity = new Map(nodes.map((node) => [node.id, node.kind === 'origin' ? 1 : fade(node)]));
  const selected = nodes.find((node) => matches(scene.selection, node));
  const hovered = nodes.find((node) => matches(scene.hover, node));
  const related = new Set();
  function linkFocus(focus) {
    if (!focus) return;
    related.add(focus.id);
    for (const hub of nodes.filter((node) => ['label', 'person'].includes(node.kind))) {
      if (hub === focus || (focus.rank != null && hub.data.pick_ranks.includes(focus.rank))) {
        related.add(hub.id);
        if (hub === focus) for (const node of nodes) if (hub.data.pick_ranks.includes(node.rank)) related.add(node.id);
      }
    }
  }
  linkFocus(selected);
  linkFocus(hovered);
  const alphaOf = (node) => opacity.get(node.id) * (scene.selection && !related.has(node.id) ? 0.4 : 1);
  const {cx, cy, R, scale = 1, tx = 0, ty = 0} = viewport;
  const centre = {x: tx + cx * scale, y: ty + cy * scale};
  ctx.save();
  const style = getComputedStyle(canvas);
  ctx.font = `${style.fontSize} ${style.fontFamily}`;
  ctx.lineWidth = 1;
  ctx.strokeStyle = colors['--line'];
  ctx.setLineDash([1, 3]);
  for (const radius of [0.34, 0.68]) circle(ctx, centre.x, centre.y, radius * R * scale);
  if (scene.originPoints.length > 1) {
    for (let i = 0; i < scene.originPoints.length; i++) {
      const angle = i * TAU / scene.originPoints.length;
      ctx.beginPath(); ctx.moveTo(centre.x, centre.y);
      ctx.lineTo(centre.x + R * scale * Math.sin(angle), centre.y - R * scale * Math.cos(angle));
      ctx.stroke();
    }
  }
  if (scene.step.parent_id) {
    ctx.strokeStyle = colors['--muted'];
    ctx.beginPath(); ctx.moveTo(centre.x - R * scale, centre.y - R * scale);
    ctx.lineTo(centre.x, centre.y); ctx.stroke();
  }
  ctx.setLineDash([]);
  const byRank = new Map(nodes.filter((node) => node.rank != null).map((node) => [node.rank, node]));
  for (const hub of nodes.filter((node) => ['label', 'person'].includes(node.kind))) {
    ctx.strokeStyle = colors[`--node-${hub.kind}`];
    for (const rank of hub.data.pick_ranks) {
      const pick = byRank.get(rank);
      if (!pick) continue;
      const active = (selected || hovered) && related.has(hub.id) && related.has(pick.id);
      ctx.globalAlpha = Math.min(opacity.get(hub.id), opacity.get(pick.id)) *
        (active ? 1 : scene.selection ? 0.45 * 0.4 : 0.45);
      ctx.beginPath(); ctx.moveTo(hub.x, hub.y); ctx.lineTo(pick.x, pick.y); ctx.stroke();
    }
  }
  const labels = [];
  for (const node of nodes) {
    ctx.globalAlpha = alphaOf(node);
    ctx.lineWidth = 1.5;
    if (node.kind === 'track') {
      const marks = scene.marks?.[node.id] || {};
      ctx.fillStyle = colors['--node-pick'];
      ctx.strokeStyle = colors['--mark-passed'];
      if (marks.heard) ctx.globalAlpha *= 0.55;
      circle(ctx, node.x, node.y, 4, !marks.passed);
      ctx.globalAlpha = alphaOf(node);
      if (marks.kept) {
        ctx.strokeStyle = colors['--mark-kept'];
        circle(ctx, node.x, node.y, 7);
        ctx.save(); ctx.font = `9px ${style.fontFamily}`; ctx.fillStyle = colors['--mark-kept'];
        ctx.fillText('★', node.x + 6, node.y - 8); ctx.restore();
      }
      if (scene.playing?.id === node.id) {
        ctx.strokeStyle = colors['--mark-playing'];
        if (scene.reducedMotion || scene.pulsePlaying === false) {
          circle(ctx, node.x, node.y, 9); circle(ctx, node.x, node.y, 12);
        } else {
          const phase = (now % 1400) / 1400;
          ctx.globalAlpha *= 1 - phase;
          circle(ctx, node.x, node.y, 10 + phase * 6);
          animated = true;
        }
        ctx.globalAlpha = alphaOf(node);
      }
      if (node === hovered) { ctx.strokeStyle = colors['--text']; circle(ctx, node.x, node.y, 8); }
      if (node === selected) labels.unshift({node, text: short(nameOf(node.data), 28), forced: true, alpha: 1});
    } else if (node.kind === 'origin') {
      ctx.fillStyle = colors['--node-seed']; star(ctx, node.x, node.y, 7);
      if (scene.step.status === 'running' && !scene.step.picks_done_at) {
        ctx.strokeStyle = colors['--st-pending'];
        if (scene.reducedMotion) { ctx.setLineDash([3, 3]); circle(ctx, node.x, node.y, 22); ctx.setLineDash([]); }
        else {
          const phase = (now % 1600) / 1600;
          ctx.globalAlpha *= 1 - phase; circle(ctx, node.x, node.y, 18 + 16 * phase);
          ctx.globalAlpha = alphaOf(node); animated = true;
        }
      }
      if (scene.originPoints.length > 1) {
        const sharedArtist = node.point.artist && scene.originPoints.some(point =>
          point.point_id !== node.point.point_id && point.artist?.trim().toLowerCase() === node.point.artist.trim().toLowerCase());
        const rimName = sharedArtist && node.point.title ? node.point.title : node.point.name;
        labels.push({node: {...node,
          x: centre.x + R * scale * Math.sin(node.angle), y: centre.y - R * scale * Math.cos(node.angle)},
          text: short(rimName, 16), color: colors['--muted'], alpha: alphaOf(node)});
      }
    } else {
      ctx.fillStyle = colors[`--node-${node.kind}`];
      if (node.kind === 'label') ctx.fillRect(node.x - 6, node.y - 6, 12, 12);
      else {
        ctx.fillStyle = colors['--panel']; circle(ctx, node.x, node.y, 6, true);
        ctx.strokeStyle = colors['--node-person']; ctx.lineWidth = 2; circle(ctx, node.x, node.y, 6);
      }
      const width = canvas.getBoundingClientRect().width || canvas.width;
      let label = node.data.name;
      while (label.length > 1 && ctx.measureText(label).width > width - 8) label = short(label, label.length - 1);
      labels.push({node, text: label, alpha: alphaOf(node), forced: node === selected});
    }
    if (node === selected) { ctx.globalAlpha = 1; square(ctx, node, colors); }
  }
  const rect = canvas.getBoundingClientRect();
  drawLabels(ctx, labels, rect.width || canvas.width, rect.height || canvas.height, colors, viewport);
  ctx.restore();
  return animated;
}

/** Nearest painted target, with CSS-pixel distance and paint-order ties; never moves a node. */
export function hitStep(scene, x, y) {
  let best = null, distance = Infinity;
  for (const node of [...(hitFrames.get(scene) || [])].reverse()) {
    const d = Math.hypot(node.x - x, node.y - y);
    if (d <= node.target && d < distance) { best = node; distance = d; }
  }
  return best ? {point: best.point, rank: best.rank, distance} : null;
}

/** Screen bounds of the last painted node, including its selection mark.
 * Projection and targets are already in CSS pixels, independent of backing DPR.
 */
export function stepAnchor(scene, point, canvas) {
  const node = hitFrames.get(scene)?.find((node) => matches(point, node));
  if (!node) return null;
  const rect = canvas.getBoundingClientRect(), stage = canvas.closest('#main').getBoundingClientRect();
  const x = rect.left + node.x, y = rect.top + node.y;
  if (x < Math.max(rect.left, stage.left, 0) || x > Math.min(rect.right, stage.right, innerWidth) ||
      y < Math.max(rect.top, stage.top, 0) || y > Math.min(rect.bottom, stage.bottom, innerHeight)) return null;
  return {left: x - node.target, top: y - node.target, bottom: y + node.target};
}

export function mountStepMap(root, {store, h, text}) {
  const disposers = [];
  const title = h('h2', {className: 'panel__title', id: 'stepmap-title'});
  const toolbar = h('div', {className: 'stepmap-tools'});
  for (const [action, label, name] of [['zoom-out', '−', 'Zoom out'], ['zoom-in', '+', 'Zoom in'], ['fit', 'Fit', 'Fit']])
    toolbar.append(h('button', {type: 'button', className: 'btn btn--small', tabindex: -1,
      'data-action': action, 'aria-label': name}, label));
  const canvas = h('canvas', {id: 'stepmap-canvas', tabindex: 0, 'aria-labelledby': 'stepmap-title',
    'aria-describedby': 'stepmap-legend stepmap-caption'});
  const caption = h('div', {id: 'stepmap-caption', role: 'status'});
  const from = h('button', {type: 'button', className: 'stepmap-from', 'data-action': 'from', tabindex: -1});
  const tooltip = h('div', {className: 'stepmap-tooltip', role: 'tooltip', hidden: true});
  const surface = h('div', {className: 'stepmap-surface'}, canvas, caption, from, tooltip);
  const legend = h('p', {id: 'stepmap-legend', title: text('S-MAP-LEGEND')}, text('S-MAP-LEGEND'));
  root.classList.add('stepmap');
  root.replaceChildren(h('header', {className: 'panel__bar'}, title, toolbar), surface, legend);
  const ctx = canvas.getContext('2d');
  const motion = matchMedia('(prefers-reduced-motion: reduce)');
  const compact = matchMedia('(max-height: 759px)');
  let scene = null, stepId = null, view = null, colors, width = 0, height = 0;
  let scale = 1, tx = 0, ty = 0, frame = null, drag = null, dragged = false, destroyed = false;
  let tooltipAt = null, density = null, paintSignature = null;

  function listen(target, event, handler, options) {
    target.addEventListener(event, handler, options);
    disposers.push(() => target.removeEventListener(event, handler, options));
  }
  function readColors() {
    const css = getComputedStyle(document.documentElement);
    colors = Object.fromEntries(TOKENS.map((token) => ['--' + token, css.getPropertyValue('--' + token).trim()]));
    if (scene) scene = {...scene, colors};
    paint();
  }
  function viewport() {
    caption.style.whiteSpace = '';
    caption.style.overflow = '';
    caption.style.textOverflow = '';
    caption.removeAttribute('title');
    if (!compact.matches && caption.hidden) return {cx: width / 2, cy: height / 2,
      R: Math.max(1, Math.min(width, height) / 2 - 24), scale, tx, ty};
    const legendHeight = compact.matches ? legend.offsetHeight : 0;
    // Keep enough free height for glyphs and the 36px from control. A tall
    // caption becomes a single band only when it would consume that room.
    if (!caption.hidden && height - legendHeight - caption.offsetTop - caption.offsetHeight < 36) {
      caption.style.whiteSpace = 'nowrap';
      caption.style.overflow = 'hidden';
      caption.style.textOverflow = 'ellipsis';
      caption.title = caption.textContent;
    }
    const labelTop = caption.hidden ? 0 : caption.offsetTop + caption.offsetHeight;
    const labelBottom = height - legendHeight;
    const radius = Math.max(1, Math.min(width - 48, labelBottom - labelTop - 32) / 2);
    // The 18px inner origin ring must fit too when a caption wraps. Scale the
    // entire projection; saved polar geometry and CSS-pixel glyph sizes stay put.
    const R = Math.max(scene?.originPoints.length > 1 ? 18 : 1, radius);
    const fittedScale = radius / R;
    return {cx: width / 2 / fittedScale, cy: (labelTop + labelBottom) / 2 / fittedScale,
      R, scale: scale * fittedScale, tx, ty, labelTop, labelBottom};
  }
  function paint() {
    if (destroyed) return;
    if (frame != null) { cancelAnimationFrame(frame); frame = null; }
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = colors?.['--panel'] || getComputedStyle(canvas).backgroundColor;
    ctx.fillRect(0, 0, width, height);
    if (!scene || !width || !height || root.hidden || !root.getClientRects().length) return;
    scene = {...scene, reducedMotion: motion.matches || store.getState().ui.reducedMotion};
    const vp = viewport(), animate = drawStep(ctx, scene, vp);
    const fromX = tx + vp.scale * (vp.cx - vp.R), fromY = ty + vp.scale * (vp.cy - vp.R);
    const banded = compact.matches || !caption.hidden;
    from.style.left = `${banded ? Math.max(4, fromX - from.offsetWidth - 4) : fromX}px`;
    from.style.top = `${banded ? Math.max(vp.labelTop,
      Math.min(vp.labelBottom - from.offsetHeight, fromY)) : fromY}px`;
    if (animate) frame = requestAnimationFrame(() => { frame = null; paint(); });
  }
  function resize() {
    if (destroyed) return;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    width = rect.width; height = rect.height;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * dpr); canvas.height = Math.round(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    paint();
  }
  function placeLegend() {
    (compact.matches ? surface : root).append(legend);
    resize();
  }
  function watchDensity() {
    if (destroyed) return;
    density?.removeEventListener('change', watchDensity);
    density = matchMedia(`(resolution: ${window.devicePixelRatio || 1}dppx)`);
    density.addEventListener('change', watchDensity);
    resize();
  }
  function fit() { scale = 1; tx = ty = 0; paint(); }
  function zoomAt(x, y, factor) {
    const next = Math.max(MIN_SCALE, Math.min(MAX_SCALE, scale * factor));
    tx = x - (x - tx) / scale * next;
    ty = y - (y - ty) / scale * next;
    scale = next;
    paint();
  }
  function local(event) {
    const rect = canvas.getBoundingClientRect();
    return {x: event.clientX - rect.left, y: event.clientY - rect.top};
  }
  function hover(event) {
    if (!scene) return;
    const at = local(event), hit = hitStep(scene, at.x, at.y);
    tooltipAt = hit ? at : null;
    canvas.classList.toggle('is-over-node', !!hit);
    const current = store.getState().ui.hover;
    if ((hit || current) && (current?.stepId !== stepId || selectionId(current?.point) !== selectionId(hit?.point) ||
        current?.point?.source_rank !== hit?.point.source_rank))
      store.actions.hoverPoint(stepId, hit?.point || null);
    showTooltip();
  }
  function showTooltip() {
    const point = scene?.hover;
    tooltip.hidden = !point || !tooltipAt || point.kind === 'origin';
    if (tooltip.hidden) return;
    const pick = scene.picks.find((item) => item.rank === point.source_rank);
    const hub = scene.hubs.find((item) => item.hub_key === selectionId(point));
    if (point.is_origin) tooltip.textContent = point.name;
    else if (pick && point.kind === 'track') {
      tooltip.textContent = text('S-MAP-TIP-PICK', {'Artist - Title': nameOf(pick), '0.79': pick.score.toFixed(2)}) +
        (pick.facts_state === 'ready' && pick.facts.labels.length ? ` · on ${pick.facts.labels.map((label) => label.name).join(', ')}` : '');
    } else if (hub) {
      const roles = [...new Set(scene.picks.filter((item) => hub.pick_ranks.includes(item.rank))
        .flatMap((item) => item.facts?.credits.filter((credit) => (credit.id ? `person:${credit.id}` :
          `person:name:${credit.name.trim().toLowerCase().replace(/\s*\(\d+\)\s*$/, '').replace(/\s+/g, ' ').trim()}`) === hub.hub_key)
          .flatMap((credit) => credit.roles) || []))];
      tooltip.textContent = text(hub.kind === 'label' ? 'S-MAP-TIP-LABEL' : 'S-MAP-TIP-PERSON',
        {Label: hub.name, Person: hub.name, k: hub.pick_ranks.length, roles: roles.join(', ')});
      const s = store.getState();
      const other = (s.steps.byDig[scene.step.dig_id] || []).map((id) => s.steps.byId[id])
        .find((step) => step.id !== stepId && step.hubs?.some((item) => item.hub_key === hub.hub_key));
      if (other) tooltip.textContent += ` · also in step ${other.number}`;
    } else { tooltip.hidden = true; return; }
    tooltip.style.left = `${Math.max(4, Math.min(width - tooltip.offsetWidth - 4, tooltipAt.x + 14))}px`;
    tooltip.style.top = `${Math.max(4, Math.min(height - tooltip.offsetHeight - 4, tooltipAt.y + 14))}px`;
  }
  function report(error) { if (!destroyed) store.actions.showToast('S-T-MOVE-ERR', {reason: error.message}); }
  listen(root, 'click', (event) => {
    const action = event.target.closest('[data-action]')?.dataset.action;
    if (!action || event.detail > 1) return;
    if (action === 'from') Promise.resolve(store.actions.goParent()).catch(report);
    else if (action === 'fit') fit();
    else zoomAt(width / 2, height / 2, action === 'zoom-in' ? 1.25 : 0.8);
  });
  listen(canvas, 'focus', () => store.actions.focusRegion('map'));
  listen(canvas, 'map-menu-anchor', (event) => {
    event.detail.anchor = stepAnchor(scene, event.detail.point, canvas);
  });
  listen(canvas, 'keydown', (event) => {
    if (!['+', '=', '-', '_', '0'].includes(event.key) || event.ctrlKey || event.metaKey || event.altKey) return;
    event.preventDefault(); event.stopPropagation();
    if (event.key === '0') fit();
    else zoomAt(width / 2, height / 2, ['+', '='].includes(event.key) ? 1.25 : 0.8);
  });
  listen(canvas, 'wheel', (event) => {
    event.preventDefault();
    const p = local(event), unit = event.deltaMode === 1 ? 32 : event.deltaMode === 2 ? 400 : 1;
    zoomAt(p.x, p.y, Math.exp(-event.deltaY * unit * (event.ctrlKey ? 0.01 : 0.0018)));
  }, {passive: false});
  listen(canvas, 'pointerdown', (event) => {
    if (event.button !== 0) return;
    const p = local(event);
    drag = {id: event.pointerId, x: p.x, y: p.y, tx, ty}; dragged = false;
    canvas.setPointerCapture(event.pointerId);
    canvas.focus({preventScroll: true});
  });
  listen(canvas, 'pointermove', (event) => {
    if (!drag) { hover(event); return; }
    if (drag.id !== event.pointerId) return;
    const p = local(event);
    if (!dragged && Math.hypot(p.x - drag.x, p.y - drag.y) < 4) return;
    dragged = true; tx = drag.tx + p.x - drag.x; ty = drag.ty + p.y - drag.y;
    canvas.classList.add('is-dragging'); tooltip.hidden = true; paint();
  });
  function endDrag(event) {
    if (drag?.id !== event.pointerId) return;
    drag = null; canvas.classList.remove('is-dragging');
    if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
  }
  listen(canvas, 'pointerup', endDrag);
  listen(canvas, 'pointercancel', (event) => { dragged = true; endDrag(event); });
  listen(canvas, 'click', (event) => {
    if (!scene || dragged || event.detail > 1) return;
    const p = local(event), hit = hitStep(scene, p.x, p.y);
    store.actions.selectPoint(stepId, hit?.point || null);
  });
  listen(canvas, 'dblclick', (event) => {
    if (!scene || dragged) return;
    const p = local(event), hit = hitStep(scene, p.x, p.y);
    if (hit?.rank != null) store.actions.playPick({pickId: `${stepId}:${hit.rank}`});
    else if (!hit) fit();
  });
  listen(canvas, 'pointerleave', () => {
    tooltipAt = null; tooltip.hidden = true; canvas.classList.remove('is-over-node');
    if (store.getState().ui.hover?.stepId === stepId) store.actions.hoverPoint(stepId, null);
  });
  listen(motion, 'change', paint);
  listen(compact, 'change', placeLegend);
  function render(s) {
    const step = selectors.viewedStep(s), nextView = s.ui.route.view;
    const reducedMotion = motion.matches || s.ui.reducedMotion;
    if (!step || s.ui.route.page !== 'dig') {
      if (scene || !reducedMotion) { scene = null; tooltip.hidden = true; paint(); }
      paintSignature = null;
      return;
    }
    const nextScene = {...selectors.hubScene(s, step.id), colors, pulsePlaying: s.player.state === 'playing'};
    // Playback progress and unrelated UI notifications do not change the map.
    const signature = reducedMotion ? JSON.stringify([nextScene, nextView,
      s.steps.byId[step.parent_id]?.number, s.ui.settings?.discogs_token?.set]) : null;
    if (reducedMotion && signature === paintSignature) { showTooltip(); return; }
    paintSignature = signature;
    const changed = stepId !== step.id || view !== nextView;
    stepId = step.id; view = nextView;
    if (changed) { scale = 1; tx = ty = 0; tooltipAt = null; }
    scene = nextScene;
    root.dataset.stepId = stepId; canvas.dataset.stepId = stepId;
    const mapTitle = text('S-MAP-TITLE', {n: step.number}), number = String(step.number);
    title.replaceChildren(
      h('span', {className: 'stepmap-title-words'}, mapTitle.slice(0, -number.length)),
      h('span', {className: 'stepmap-title-number'}, number));
    title.title = mapTitle;
    from.hidden = !step.parent_id;
    from.dataset.stepId = step.parent_id || '';
    from.textContent = text('S-MAP-FROM', {p: s.steps.byId[step.parent_id]?.number || ''});
    const noToken = scene.picks.some((pick) => pick.facts_state === 'no_token') ||
      (s.ui.settings && !s.ui.settings.discogs_token?.set);
    const unavailable = scene.picks.some((pick) => pick.facts_state === 'unavailable');
    const codes = new Set((step.note || []).map(note => note.code));
    const partial = !codes.has('discogs_unavailable') &&
      (codes.has('discogs_budget_exhausted') || codes.has('facts_interrupted'));
    const loading = scene.picks.some((pick) => pick.facts_state === 'loading');
    const captions = [];
    if (!scene.hubs.length && scene.picks.length) captions.push(text('S-MAP-NOHUB', {loading,
      i: scene.picks.filter((pick) => pick.facts_state !== 'loading').length, n: scene.picks.length}));
    // Without Discogs the map doesn't ask for a token; moves offer Discogs when asked for (P8).
    if (!noToken && unavailable) captions.push(text(partial ? 'S-MAP-FACTS-PARTIAL' : 'S-MAP-DISCOGS-DOWN'));
    caption.textContent = captions.join('\n'); caption.hidden = !captions.length;
    caption.classList.toggle('stepmap-warning', unavailable && !noToken);
    showTooltip();
    resize();
  }
  const observer = new ResizeObserver(resize);
  observer.observe(canvas);
  const theme = new MutationObserver(readColors);
  theme.observe(document.documentElement, {attributes: true, attributeFilter: ['class', 'style']});
  const unsubscribe = store.subscribe(render);
  placeLegend(); readColors(); render(store.getState()); watchDensity();
  // Chromium can change DPR without dispatching a resolution-change event.
  const densityTimer = setInterval(() => { if (!density.matches) watchDensity(); }, 250);
  document.fonts?.ready.then(() => { if (!destroyed) paint(); });
  return {destroy() {
    destroyed = true; unsubscribe(); observer.disconnect(); theme.disconnect();
    density.removeEventListener('change', watchDensity);
    clearInterval(densityTimer);
    if (frame != null) cancelAnimationFrame(frame);
    for (const dispose of disposers.reverse()) dispose();
    arrivals.delete(canvas); root.replaceChildren();
  }};
}
