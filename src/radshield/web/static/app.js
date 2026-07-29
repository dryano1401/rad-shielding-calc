'use strict';

// Rendering zoom used when asking the server for a page raster. Image pixels
// therefore equal PDF units multiplied by this factor, and that is the only
// place the two spaces are related -- everything stored is PDF units.
const RENDER_ZOOM = 2;

const state = {
  project: null,
  options: null,
  floorId: null,
  tool: 'select',
  selection: null,          // {kind: 'source'|'poi', id}
  view: { scale: 1, x: 0, y: 0 },
  images: new Map(),        // floorId -> HTMLImageElement
  calibrationPick: [],      // points collected by the calibrate tool
  measurePick: [],          // points collected by the measure tool
  wallPick: [],             // points collected by the wall tool
  barriers: null,           // /api/barriers payload
  hover: null,              // cursor position in PDF space, for rubber banding
  distances: null,          // /api/distances payload, refreshed after edits
  drag: null,
  results: null,
  ghost: false,
};

const canvas = document.getElementById('plan');
const ctx = canvas.getContext('2d');

/* ------------------------------------------------------------------ api */

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

const send = (path, method, body) => api(path, {
  method,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body || {}),
});

function setProject(data) {
  state.project = data;
  if (!state.floorId || !data.floors.some(f => f.id === state.floorId)) {
    state.floorId = data.floors.length ? data.floors[0].id : null;
    fitToView();
  }
  renderAll();
}

/* ----------------------------------------------------------------- units */

const METRES_PER = { ft: 0.3048, in: 0.0254, m: 1, cm: 0.01, mm: 0.001 };

// Distinct hues per material so a plan reads at a glance.
const MATERIAL_COLOUR = {
  lead: '#b892ff', concrete: '#8fa6c4', iron: '#c98b6b', steel: '#c98b6b',
  gypsum: '#e6d2a8', glass: '#7fd4e8', wood: '#c9a06b',
};
const materialColour = m => MATERIAL_COLOUR[m] || '#96a0b1';

const displayUnit = () => state.project?.display_unit || 'ft';
const toMetres = (value, unit) => value * METRES_PER[unit || displayUnit()];
const fromMetres = (metres, unit) => metres / METRES_PER[unit || displayUnit()];

// Feet are shown as feet and inches, which is how drawings are dimensioned.
function formatLength(metres) {
  if (metres === null || metres === undefined) return '';
  const unit = displayUnit();
  if (unit === 'm') return `${metres.toFixed(2)} m`;
  const converted = fromMetres(metres, unit);
  if (unit !== 'ft') return `${converted.toFixed(2)} ${unit} (${metres.toFixed(2)} m)`;
  // Round before splitting so 11.98" rolls over instead of printing as 12.0".
  const totalInches = Math.round(converted * 120) / 10;
  const feet = Math.floor(totalInches / 12);
  const inches = totalInches - feet * 12;
  return `${feet}' ${inches.toFixed(1)}" (${metres.toFixed(2)} m)`;
}

/* -------------------------------------------------------------- geometry */

const currentFloor = () => state.project?.floors.find(f => f.id === state.floorId) || null;

// Screen <-> PDF space. PDF units are what we store; screen pixels are transient.
function toScreen(px, py) {
  return {
    x: px * RENDER_ZOOM * state.view.scale + state.view.x,
    y: py * RENDER_ZOOM * state.view.scale + state.view.y,
  };
}

function toPdf(sx, sy) {
  return {
    x: (sx - state.view.x) / (state.view.scale * RENDER_ZOOM),
    y: (sy - state.view.y) / (state.view.scale * RENDER_ZOOM),
  };
}

function eventPdf(event) {
  const rect = canvas.getBoundingClientRect();
  return toPdf(event.clientX - rect.left, event.clientY - rect.top);
}

// Length in metres of a segment on the current floor, or null when uncalibrated.
function pdfSegmentMetres(floor, p1, p2) {
  if (!floor?.metres_per_unit) return null;
  return Math.hypot(p2.x - p1.x, p2.y - p1.y) * floor.metres_per_unit;
}

function fitToView() {
  const floor = currentFloor();
  if (!floor || !floor.page_width) return;
  const rect = canvas.getBoundingClientRect();
  const scale = Math.min(
    rect.width / (floor.page_width * RENDER_ZOOM),
    rect.height / (floor.page_height * RENDER_ZOOM),
  ) * 0.94;
  state.view.scale = scale;
  state.view.x = (rect.width - floor.page_width * RENDER_ZOOM * scale) / 2;
  state.view.y = (rect.height - floor.page_height * RENDER_ZOOM * scale) / 2;
}

function zoomBy(factor, anchorX, anchorY) {
  const rect = canvas.getBoundingClientRect();
  const ax = anchorX ?? rect.width / 2;
  const ay = anchorY ?? rect.height / 2;
  const before = toPdf(ax, ay);
  state.view.scale = Math.min(Math.max(state.view.scale * factor, 0.02), 30);
  const after = toScreen(before.x, before.y);
  state.view.x += ax - after.x;
  state.view.y += ay - after.y;
  draw();
}

/* --------------------------------------------------------------- drawing */

function floorImage(floorId) {
  if (state.images.has(floorId)) return state.images.get(floorId);
  const image = new Image();
  image.onload = draw;
  image.src = `/api/floors/${floorId}/image?zoom=${RENDER_ZOOM}&t=${Date.now()}`;
  state.images.set(floorId, image);
  return image;
}

function pointsOnFloor(floorId) {
  if (!state.project) return { sources: [], pois: [] };
  return {
    sources: state.project.sources.filter(s => s.floor_id === floorId),
    pois: state.project.pois.filter(p => p.floor_id === floorId),
  };
}

function draw() {
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  if (canvas.width !== rect.width * dpr || canvas.height !== rect.height * dpr) {
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, rect.width, rect.height);

  const floor = currentFloor();
  if (!floor) {
    ctx.fillStyle = '#5c6675';
    ctx.font = '14px system-ui';
    ctx.fillText('Add a floor to begin.', 24, 36);
    return;
  }

  const image = floorImage(floor.id);
  if (image.complete && image.naturalWidth) {
    ctx.save();
    ctx.translate(state.view.x, state.view.y);
    ctx.scale(state.view.scale, state.view.scale);
    ctx.drawImage(image, 0, 0);
    ctx.restore();
  }

  if (state.ghost) {
    for (const other of state.project.floors) {
      if (other.id === floor.id) continue;
      drawPoints(other, 0.28);
    }
  }
  drawWalls(floor);
  drawLinks(floor);
  drawMeasurements(floor);
  drawPoints(floor, 1);
  drawCalibration(floor);
}

function drawWalls(floor) {
  ctx.save();
  ctx.lineCap = 'round';
  for (const wall of floor.walls || []) {
    const a = toScreen(wall.p1[0], wall.p1[1]);
    const b = toScreen(wall.p2[0], wall.p2[1]);
    // Thickness is drawn to scale where possible so a 200 mm wall looks like one.
    const scaled = floor.metres_per_unit
      ? (wall.thickness_mm / 1000) / floor.metres_per_unit * RENDER_ZOOM * state.view.scale
      : 5;
    ctx.strokeStyle = materialColour(wall.material);
    ctx.lineWidth = Math.max(scaled, 3);
    ctx.globalAlpha = 0.75;
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.stroke();
  }

  if (state.tool === 'wall' && state.wallPick.length === 1 && state.hover) {
    const a = toScreen(state.wallPick[0].x, state.wallPick[0].y);
    const b = toScreen(state.hover.x, state.hover.y);
    ctx.strokeStyle = materialColour(document.getElementById('wall-material').value);
    ctx.lineWidth = 4;
    ctx.globalAlpha = 0.6;
    ctx.setLineDash([6, 5]);
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.stroke();
    ctx.setLineDash([]);
  }
  ctx.restore();
}

function drawMeasurements(floor) {
  ctx.save();
  ctx.font = '11px system-ui';
  for (const item of floor.measurements || []) {
    dimensionLine(
      toScreen(item.p1[0], item.p1[1]),
      toScreen(item.p2[0], item.p2[1]),
      item.label ? `${item.label}: ${item.display}` : item.display,
      '#ffc857',
    );
  }

  // Rubber band for the measurement in progress.
  if (state.tool === 'measure' && state.measurePick.length === 1 && state.hover) {
    const start = state.measurePick[0];
    const metres = pdfSegmentMetres(floor, start, state.hover);
    dimensionLine(
      toScreen(start.x, start.y),
      toScreen(state.hover.x, state.hover.y),
      metres === null ? 'floor not calibrated' : formatLength(metres),
      '#ffc857',
      true,
    );
  }
  ctx.restore();
}

// A measured segment with end ticks and a length label at its midpoint.
function dimensionLine(a, b, text, colour, dashed) {
  ctx.save();
  ctx.strokeStyle = colour;
  ctx.fillStyle = colour;
  ctx.lineWidth = 1.5;
  ctx.setLineDash(dashed ? [5, 4] : []);
  ctx.beginPath();
  ctx.moveTo(a.x, a.y);
  ctx.lineTo(b.x, b.y);
  ctx.stroke();

  // End ticks drawn perpendicular to the run.
  const angle = Math.atan2(b.y - a.y, b.x - a.x) + Math.PI / 2;
  const tx = Math.cos(angle) * 6;
  const ty = Math.sin(angle) * 6;
  ctx.setLineDash([]);
  for (const end of [a, b]) {
    ctx.beginPath();
    ctx.moveTo(end.x - tx, end.y - ty);
    ctx.lineTo(end.x + tx, end.y + ty);
    ctx.stroke();
  }

  if (text) {
    const mx = (a.x + b.x) / 2;
    const my = (a.y + b.y) / 2;
    const width = ctx.measureText(text).width;
    ctx.fillStyle = 'rgba(10,12,16,.85)';
    ctx.fillRect(mx - width / 2 - 4, my - 17, width + 8, 15);
    ctx.fillStyle = colour;
    ctx.textAlign = 'center';
    ctx.fillText(text, mx, my - 6);
    ctx.textAlign = 'left';
  }
  ctx.restore();
}

function drawCalibration(floor) {
  // The in-progress pick, then the stored calibration and alignment marks.
  if (state.tool === 'calibrate' && state.calibrationPick.length === 1) {
    const p = toScreen(state.calibrationPick[0].x, state.calibrationPick[0].y);
    marker(p.x, p.y, '#4da3ff');
  }
  if (floor.calibration) {
    const a = toScreen(floor.calibration.p1[0], floor.calibration.p1[1]);
    const b = toScreen(floor.calibration.p2[0], floor.calibration.p2[1]);
    ctx.save();
    ctx.strokeStyle = '#4da3ff';
    ctx.lineWidth = 1.5;
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.stroke();
    ctx.restore();
    marker(a.x, a.y, '#4da3ff');
    marker(b.x, b.y, '#4da3ff');
  }
  if (floor.alignment) {
    const p = toScreen(floor.alignment[0], floor.alignment[1]);
    ctx.save();
    ctx.strokeStyle = '#c78bff';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(p.x - 9, p.y); ctx.lineTo(p.x + 9, p.y);
    ctx.moveTo(p.x, p.y - 9); ctx.lineTo(p.x, p.y + 9);
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(p.x, p.y, 7, 0, Math.PI * 2);
    ctx.stroke();
    ctx.restore();
  }
}

function marker(x, y, colour) {
  ctx.save();
  ctx.fillStyle = colour;
  ctx.beginPath();
  ctx.arc(x, y, 4, 0, Math.PI * 2);
  ctx.fill();
  ctx.restore();
}

function drawLinks(floor) {
  // Draw source-to-point links. Cross-floor links are dashed, since their
  // on-screen length is not the distance being used.
  ctx.save();
  ctx.lineWidth = 1;
  for (const poi of state.project.pois) {
    for (const sourceId of poi.linked_source_ids) {
      const source = state.project.sources.find(s => s.id === sourceId);
      if (!source) continue;
      const poiHere = poi.floor_id === floor.id;
      const sourceHere = source.floor_id === floor.id;
      if (!poiHere && !sourceHere) continue;
      const highlighted = state.selection &&
        (state.selection.id === poi.id || state.selection.id === source.id);
      ctx.strokeStyle = highlighted ? 'rgba(77,163,255,.9)' : 'rgba(150,160,177,.35)';
      ctx.setLineDash(poiHere && sourceHere ? [] : [3, 4]);
      const a = toScreen(source.x, source.y);
      const b = toScreen(poi.x, poi.y);
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
    }
  }
  ctx.restore();
}

function drawPoints(floor, alpha) {
  const { sources, pois } = pointsOnFloor(floor.id);
  ctx.save();
  ctx.globalAlpha = alpha;
  ctx.font = '11px system-ui';

  for (const source of sources) {
    const p = toScreen(source.x, source.y);
    const on = state.selection?.id === source.id;
    // Equipment with a scatter chart is directional, so show which way its
    // chart is pointing: the arrow is the chart's +y, usually the table axis.
    if (source.method === 'ncrp147_ct' && source.params?.scatter_method === 'chart') {
      const angle = -(source.rotation_deg || 0) * Math.PI / 180;
      const length = 34;
      const ax = p.x - Math.sin(angle) * length;
      const ay = p.y - Math.cos(angle) * length;
      ctx.save();
      ctx.strokeStyle = '#ff8a3d';
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(p.x, p.y);
      ctx.lineTo(ax, ay);
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(ax, ay, 3.5, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    }
    ctx.fillStyle = '#ff8a3d';
    ctx.beginPath();
    ctx.moveTo(p.x, p.y - 8);
    ctx.lineTo(p.x + 8, p.y);
    ctx.lineTo(p.x, p.y + 8);
    ctx.lineTo(p.x - 8, p.y);
    ctx.closePath();
    ctx.fill();
    if (on) { ctx.strokeStyle = '#fff'; ctx.lineWidth = 2; ctx.stroke(); }
    label(p.x + 11, p.y + 4, source.label || 'source', '#ffb684', alpha);
  }

  for (const poi of pois) {
    const p = toScreen(poi.x, poi.y);
    const on = state.selection?.id === poi.id;
    ctx.fillStyle = '#40d0a0';
    ctx.beginPath();
    ctx.arc(p.x, p.y, 7, 0, Math.PI * 2);
    ctx.fill();
    if (on) { ctx.strokeStyle = '#fff'; ctx.lineWidth = 2; ctx.stroke(); }
    label(p.x + 11, p.y + 4, poi.label || 'point', '#8fe6c8', alpha);
  }
  ctx.restore();
}

function label(x, y, text, colour, alpha) {
  if (alpha < 1) return;
  ctx.fillStyle = 'rgba(10,12,16,.72)';
  const width = ctx.measureText(text).width;
  ctx.fillRect(x - 3, y - 11, width + 6, 15);
  ctx.fillStyle = colour;
  ctx.fillText(text, x, y);
}

function hitTest(pdf) {
  const floor = currentFloor();
  if (!floor) return null;
  const tolerance = 11 / (state.view.scale * RENDER_ZOOM);
  const { sources, pois } = pointsOnFloor(floor.id);
  for (const poi of pois) {
    if (Math.hypot(poi.x - pdf.x, poi.y - pdf.y) < tolerance) return { kind: 'poi', id: poi.id };
  }
  for (const source of sources) {
    if (Math.hypot(source.x - pdf.x, source.y - pdf.y) < tolerance) {
      return { kind: 'source', id: source.id };
    }
  }
  return null;
}

/* -------------------------------------------------------------- interaction */

canvas.addEventListener('mousedown', event => {
  if (!currentFloor()) return;
  const pdf = eventPdf(event);

  if (state.tool === 'select') {
    const hit = hitTest(pdf);
    if (hit) {
      const item = hit.kind === 'poi'
        ? state.project.pois.find(p => p.id === hit.id)
        : state.project.sources.find(s => s.id === hit.id);
      state.drag = { ...hit, dx: item.x - pdf.x, dy: item.y - pdf.y, moved: false };
      select(hit);
    } else {
      state.drag = { pan: true, x: event.clientX, y: event.clientY };
      select(null);
    }
  }
});

canvas.addEventListener('mousemove', event => {
  const floor = currentFloor();
  state.hover = floor ? eventPdf(event) : null;

  if (state.tool === 'wall' && state.wallPick.length === 1 && state.hover) {
    const metres = pdfSegmentMetres(floor, state.wallPick[0], state.hover);
    setStatus(metres === null
      ? 'This floor has no scale yet — set the scale before drawing walls.'
      : `Wall length: ${formatLength(metres)} — click to place, Esc to cancel.`);
    draw();
  }

  if (state.tool === 'measure' && state.measurePick.length === 1 && state.hover) {
    const metres = pdfSegmentMetres(floor, state.measurePick[0], state.hover);
    setStatus(metres === null
      ? 'This floor has no scale yet — set the scale before measuring.'
      : `Length: ${formatLength(metres)} — click to record, Esc to cancel.`);
    draw();
  }

  if (!state.drag) return;

  if (state.drag.pan) {
    state.view.x += event.clientX - state.drag.x;
    state.view.y += event.clientY - state.drag.y;
    state.drag.x = event.clientX;
    state.drag.y = event.clientY;
    draw();
    return;
  }

  const pdf = eventPdf(event);
  const list = state.drag.kind === 'poi' ? state.project.pois : state.project.sources;
  const item = list.find(i => i.id === state.drag.id);
  item.x = pdf.x + state.drag.dx;
  item.y = pdf.y + state.drag.dy;
  state.drag.moved = true;
  draw();
});

window.addEventListener('mouseup', async () => {
  const drag = state.drag;
  state.drag = null;
  if (!drag || drag.pan || !drag.moved) return;
  const list = drag.kind === 'poi' ? state.project.pois : state.project.sources;
  const item = list.find(i => i.id === drag.id);
  const path = drag.kind === 'poi' ? `/api/pois/${drag.id}` : `/api/sources/${drag.id}`;
  setProject(await send(path, 'PATCH', { x: item.x, y: item.y }));
});

canvas.addEventListener('click', async event => {
  const floor = currentFloor();
  if (!floor || state.drag?.moved) return;
  const pdf = eventPdf(event);

  if (state.tool === 'calibrate') {
    state.calibrationPick.push(pdf);
    if (state.calibrationPick.length < 2) {
      setStatus('Now click the second point of the known dimension.');
      draw();
      return;
    }
    const [p1, p2] = state.calibrationPick;
    state.calibrationPick = [];
    const entry = prompt(
      'Real-world distance between the two clicked points.\n' +
      'Include the unit, e.g. "40 ft", "12.5 m", "36 in".', '');
    if (!entry) { draw(); return; }
    const match = entry.trim().match(/^([\d.]+)\s*(ft|in|m|cm|mm)?$/i);
    if (!match) { alert('Could not read that. Try something like "40 ft".'); draw(); return; }
    try {
      setProject(await send(`/api/floors/${floor.id}`, 'PATCH', {
        calibration: {
          p1: [p1.x, p1.y], p2: [p2.x, p2.y],
          known_distance: parseFloat(match[1]),
          unit: (match[2] || 'ft').toLowerCase(),
        },
      }));
      setTool('select');
    } catch (error) { alert(error.message); }
    return;
  }

  if (state.tool === 'measure') {
    if (!floor.metres_per_unit) {
      alert('Set the scale on this floor before measuring.');
      return;
    }
    state.measurePick.push(pdf);
    if (state.measurePick.length < 2) {
      setStatus('Click the second point of the distance to measure.');
      draw();
      return;
    }
    const [p1, p2] = state.measurePick;
    state.measurePick = [];
    const metres = pdfSegmentMetres(floor, p1, p2);
    const label = prompt(
      `Measured ${formatLength(metres)}.\nName this measurement (optional), or Cancel to discard.`,
      '');
    if (label === null) { draw(); return; }
    setProject(await send(`/api/floors/${floor.id}/measurements`, 'POST', {
      p1: [p1.x, p1.y], p2: [p2.x, p2.y], label,
    }));
    return;
  }

  if (state.tool === 'wall') {
    if (!floor.metres_per_unit) {
      alert('Set the scale on this floor before drawing walls.');
      return;
    }
    state.wallPick.push(pdf);
    if (state.wallPick.length < 2) {
      setStatus('Click the far end of the wall.');
      draw();
      return;
    }
    const [p1, p2] = state.wallPick;
    state.wallPick = [];
    const entered = parseFloat(document.getElementById('wall-thickness').value);
    const thicknessMm = wallThicknessToMm(entered);
    try {
      setProject(await send(`/api/floors/${floor.id}/walls`, 'POST', {
        p1: [p1.x, p1.y], p2: [p2.x, p2.y],
        material: document.getElementById('wall-material').value,
        thickness_mm: thicknessMm,
        base_height_m: 0,
        top_height_m: parseFloat(document.getElementById('wall-height').value) || 3.0,
      }));
    } catch (error) { alert(error.message); }
    return;
  }

  if (state.tool === 'align') {
    setProject(await send(`/api/floors/${floor.id}`, 'PATCH', { alignment: [pdf.x, pdf.y] }));
    setTool('select');
    return;
  }

  if (state.tool === 'source') {
    const project = await send('/api/sources', 'POST', {
      floor_id: floor.id, x: pdf.x, y: pdf.y,
      label: `Source ${state.project.sources.length + 1}`,
      method: 'tg108',
      params: {
        kind: 'uptake', nuclide: 'F-18', administered_activity_MBq: 555,
        patients_per_week: 40, uptake_time_h: 1.0, imaging_time_h: 0.5,
        void_factor: 0.85, scanner_attenuation: 1.0,
      },
    });
    setProject(project);
    select({ kind: 'source', id: project.sources[project.sources.length - 1].id });
    setTool('select');
    return;
  }

  if (state.tool === 'poi') {
    const project = await send('/api/pois', 'POST', {
      floor_id: floor.id, x: pdf.x, y: pdf.y,
      label: `Point ${state.project.pois.length + 1}`,
      occupancy: 1.0, area_class: 'uncontrolled',
      linked_source_ids: state.project.sources.map(s => s.id),
    });
    setProject(project);
    select({ kind: 'poi', id: project.pois[project.pois.length - 1].id });
    setTool('select');
  }
});

canvas.addEventListener('wheel', event => {
  event.preventDefault();
  const rect = canvas.getBoundingClientRect();
  zoomBy(event.deltaY < 0 ? 1.12 : 1 / 1.12, event.clientX - rect.left, event.clientY - rect.top);
}, { passive: false });

window.addEventListener('keydown', event => {
  if (event.key !== 'Escape') return;
  state.calibrationPick = [];
  state.measurePick = [];
  state.wallPick = [];
  setStatus('Cancelled.');
  draw();
});

function setTool(tool) {
  state.tool = tool;
  state.calibrationPick = [];
  state.measurePick = [];
  state.wallPick = [];
  document.querySelectorAll('.tool').forEach(button => {
    button.classList.toggle('active', button.dataset.tool === tool);
  });
  const help = {
    select: 'Drag a point to move it. Drag the drawing to pan, scroll to zoom.',
    calibrate: 'Click two points a known distance apart on this drawing.',
    align: 'Click a feature that appears on every floor (a column, stair core, lift shaft).',
    measure: 'Click two points to measure the distance between them, e.g. a wall standoff.',
    wall: 'Click each end of the wall. It uses the material, thickness and height set on the left.',
    source: 'Click to place a radiation source.',
    poi: 'Click to place a point to be protected.',
  };
  setStatus(help[tool]);
  draw();
}

const setStatus = text => { document.getElementById('status').textContent = text || ''; };

function select(selection) {
  state.selection = selection;
  renderInspector();
  renderPointList();
  draw();
}

/* ------------------------------------------------------------------- panels */

function renderAll() {
  renderFloors();
  renderFloorSelect();
  renderWalls();
  renderScatterMaps();
  renderMeasurements();
  renderMaterials();
  renderPointList();
  renderInspector();
  renderProblems();
  draw();
  refreshDistances();
}

// Wall thickness is entered in inches in feet mode, millimetres in metric.
// Barrier thickness is entered in millimetres under metric and inches under
// feet -- nobody dimensions a wall in metres. The model always stores
// millimetres, and these three helpers are the only place that is converted.
const wallUnitLabel = () => (displayUnit() === 'm' ? 'mm' : 'inches');
const wallThicknessToMm = entered =>
  displayUnit() === 'm' ? entered : entered * 25.4;
const wallThicknessFromMm = mm =>
  displayUnit() === 'm' ? mm : mm / 25.4;
const wallThicknessDisplay = mm =>
  displayUnit() === 'm' ? `${mm.toFixed(2)} mm` : `${(mm / 25.4).toFixed(2)}"`;

function renderWalls() {
  const select = document.getElementById('wall-material');
  if (!select.options.length) {
    for (const material of (state.options?.materials || [])) {
      const option = document.createElement('option');
      option.value = option.textContent = material;
      select.appendChild(option);
    }
    select.value = 'concrete';
  }
  document.getElementById('wall-units').textContent = wallUnitLabel();

  const list = document.getElementById('wall-list');
  const floor = currentFloor();
  list.innerHTML = '';
  for (const wall of (floor?.walls || [])) {
    const div = document.createElement('div');
    div.className = 'wall';
    div.innerHTML = `
      <div class="top">
        <span class="swatch" style="background:${materialColour(wall.material)}"></span>
        <input class="name" type="text" value="${escapeHtml(wall.label)}"
               placeholder="unnamed wall" data-w="label">
        <button data-w="delete" title="Delete wall">×</button>
      </div>
      <div class="detail">
        <select data-w="material">${(state.options?.materials || []).map(m =>
          `<option ${wall.material === m ? 'selected' : ''}>${m}</option>`).join('')}</select>
        <span class="reading">${wallThicknessDisplay(wall.thickness_mm)}</span>
      </div>
      <div class="wall-fields">
        <label>Thickness (${wallUnitLabel()})
          <input type="number" step="0.01" min="0"
                 value="${wallThicknessFromMm(wall.thickness_mm).toFixed(2)}" data-w="thickness">
        </label>
        <label>Top (m)
          <input type="number" step="0.1" value="${wall.top_height_m}" data-w="top">
        </label>
      </div>`;

    const patch = async body => {
      try { setProject(await send(`/api/floors/${floor.id}/walls/${wall.id}`, 'PATCH', body)); }
      catch (error) { alert(error.message); }
    };
    div.querySelector('[data-w=label]').onchange = e => patch({ label: e.target.value });
    div.querySelector('[data-w=material]').onchange = e => patch({ material: e.target.value });
    div.querySelector('[data-w=thickness]').onchange = e => {
      const entered = parseFloat(e.target.value);
      if (!(entered > 0)) { alert('Wall thickness must be greater than zero.'); renderWalls(); return; }
      patch({ thickness_mm: wallThicknessToMm(entered) });
    };
    div.querySelector('[data-w=top]').onchange = e =>
      patch({ top_height_m: parseFloat(e.target.value) });
    div.querySelector('[data-w=delete]').onclick = async () =>
      setProject(await api(`/api/floors/${floor.id}/walls/${wall.id}`, { method: 'DELETE' }));
    list.appendChild(div);
  }
  if (!floor?.walls?.length) {
    list.innerHTML = '<p class="hint">No walls on this floor yet.</p>';
  }
}

function renderScatterMaps() {
  const list = document.getElementById('map-list');
  list.innerHTML = '';
  for (const map of (state.project.scatter_maps || [])) {
    const div = document.createElement('div');
    div.className = 'map-item';
    div.innerHTML = `<div class="top"><span class="name">${escapeHtml(map.name)}</span>
      <button title="Delete chart">×</button></div>
      <div class="detail">${escapeHtml(map.summary || '')}</div>`;
    div.querySelector('button').onclick = async () =>
      setProject(await api(`/api/scatter-maps/${map.id}`, { method: 'DELETE' }));
    list.appendChild(div);
  }
  if (!state.project.scatter_maps?.length) {
    list.innerHTML = '<p class="hint">No charts imported.</p>';
  }
}

function renderMeasurements() {
  const box = document.getElementById('measure-box');
  const list = document.getElementById('measure-list');
  const floor = currentFloor();
  const items = floor?.measurements || [];
  box.hidden = items.length === 0;
  list.innerHTML = '';
  for (const item of items) {
    const div = document.createElement('div');
    div.className = 'measure';
    div.innerHTML = `<span class="len">${escapeHtml(
      item.label ? `${item.label}: ${item.display}` : item.display)}</span>
      <button title="Delete measurement">×</button>`;
    div.querySelector('button').onclick = async () =>
      setProject(await api(`/api/floors/${floor.id}/measurements/${item.id}`, { method: 'DELETE' }));
    list.appendChild(div);
  }
}

// Distances and barriers are fetched separately so both can be shown before
// anything is calculated.
async function refreshDistances() {
  try {
    [state.distances, state.barriers] = await Promise.all([
      api('/api/distances'), api('/api/barriers'),
    ]);
  } catch (_) {
    state.distances = null;
    state.barriers = null;
    return;
  }
  if (state.selection?.kind === 'poi') renderInspector();
}

function renderFloors() {
  const box = document.getElementById('floor-list');
  box.innerHTML = '';
  for (const floor of state.project.floors) {
    const div = document.createElement('div');
    div.className = 'floor' + (floor.id === state.floorId ? ' current' : '');
    div.innerHTML = `
      <div class="title">
        <input type="text" value="${escapeHtml(floor.name)}" data-act="name">
        <button data-act="view" title="View this floor">View</button>
        <button data-act="delete" title="Remove floor">×</button>
      </div>
      <div class="row">
        <label>Elevation (m)<input type="number" step="0.1" value="${floor.elevation_m}" data-act="elevation"></label>
      </div>
      <div class="meta ${floor.metres_per_unit ? '' : 'bad'}">Scale: ${escapeHtml(floor.scale_description)}</div>
      <div class="meta ${floor.alignment ? '' : 'bad'}">Alignment: ${
        floor.alignment ? 'set' : 'not set — needed for cross-floor distances'}</div>`;

    div.querySelector('[data-act=view]').onclick = () => {
      state.floorId = floor.id;
      fitToView();
      renderAll();
    };
    div.querySelector('[data-act=delete]').onclick = async () => {
      if (!confirm(`Delete "${floor.name}" and everything placed on it?`)) return;
      state.images.delete(floor.id);
      setProject(await api(`/api/floors/${floor.id}`, { method: 'DELETE' }));
    };
    div.querySelector('[data-act=name]').onchange = async event =>
      setProject(await send(`/api/floors/${floor.id}`, 'PATCH', { name: event.target.value }));
    div.querySelector('[data-act=elevation]').onchange = async event =>
      setProject(await send(`/api/floors/${floor.id}`, 'PATCH', {
        elevation_m: parseFloat(event.target.value) || 0,
      }));
    box.appendChild(div);
  }
}

function renderFloorSelect() {
  const select = document.getElementById('floor-select');
  select.innerHTML = '';
  for (const floor of state.project.floors) {
    const option = document.createElement('option');
    option.value = floor.id;
    option.textContent = `${floor.name} (${floor.elevation_m} m)`;
    option.selected = floor.id === state.floorId;
    select.appendChild(option);
  }
  select.onchange = () => { state.floorId = select.value; fitToView(); renderAll(); };
}

function renderMaterials() {
  const box = document.getElementById('material-list');
  box.innerHTML = '';
  for (const material of (state.options?.materials || [])) {
    const label = document.createElement('label');
    const checked = state.project.materials.includes(material);
    label.innerHTML = `<input type="checkbox" ${checked ? 'checked' : ''}> ${material}`;
    label.querySelector('input').onchange = async event => {
      const chosen = new Set(state.project.materials);
      event.target.checked ? chosen.add(material) : chosen.delete(material);
      setProject(await send('/api/materials', 'POST', { materials: [...chosen] }));
    };
    box.appendChild(label);
  }
}

function renderPointList() {
  const box = document.getElementById('point-list');
  box.innerHTML = '';
  const floorName = id => state.project.floors.find(f => f.id === id)?.name || '?';

  for (const source of state.project.sources) {
    box.appendChild(pointRow('source', source.id, source.label || 'source', floorName(source.floor_id)));
  }
  for (const poi of state.project.pois) {
    box.appendChild(pointRow('poi', poi.id, poi.label || 'point', floorName(poi.floor_id)));
  }
  if (!state.project.sources.length && !state.project.pois.length) {
    box.innerHTML = '<p class="hint">Nothing placed yet.</p>';
  }
}

function pointRow(kind, id, name, where) {
  const div = document.createElement('div');
  div.className = 'point' + (state.selection?.id === id ? ' selected' : '');
  div.innerHTML = `<span class="dot ${kind}"></span>
    <span class="name">${escapeHtml(name)}</span>
    <span class="where">${escapeHtml(where)}</span>`;
  div.onclick = () => {
    const item = kind === 'poi'
      ? state.project.pois.find(p => p.id === id)
      : state.project.sources.find(s => s.id === id);
    if (item.floor_id !== state.floorId) { state.floorId = item.floor_id; fitToView(); }
    select({ kind, id });
    renderFloors();
    renderFloorSelect();
  };
  return div;
}

function renderProblems() {
  const box = document.getElementById('problems-box');
  const list = document.getElementById('problems');
  const problems = state.project.problems || [];
  box.hidden = problems.length === 0;
  list.innerHTML = problems.map(p => `<li>${escapeHtml(p)}</li>`).join('');
}

/* ---------------------------------------------------------------- inspector */

function renderInspector() {
  const title = document.getElementById('inspector-title');
  const box = document.getElementById('inspector');
  if (!state.selection) {
    title.textContent = 'Nothing selected';
    box.innerHTML = '<p class="hint">Place a source or a point of interest, then select it to edit its parameters.</p>';
    return;
  }
  if (state.selection.kind === 'source') renderSourceInspector(title, box);
  else renderPoiInspector(title, box);
}

// What the chart assigned to a source quotes its values per.
function chartBasis(params) {
  const id = params.plan_map_id || params.elevation_map_id;
  const chart = (state.project.scatter_maps || []).find(m => m.id === id);
  return chart?.per || 'procedure';
}

function renderSourceInspector(title, box) {
  const source = state.project.sources.find(s => s.id === state.selection.id);
  if (!source) return select(null);
  title.textContent = 'Source';
  const p = source.params || {};
  const nuclideOptions = (state.options?.nuclides || []).map(n =>
    `<option ${p.nuclide === n ? 'selected' : ''}>${n}</option>`).join('');
  const workloadOptions = (state.options?.workloads || []).map(w =>
    `<option ${p.workload === w ? 'selected' : ''}>${escapeHtml(w)}</option>`).join('');

  box.innerHTML = `
    <div class="field">Label<input type="text" value="${escapeHtml(source.label)}" data-p="label"></div>
    <div class="field">Method
      <select data-p="method">
        <option value="tg108" ${source.method === 'tg108' ? 'selected' : ''}>TG-108 nuclear medicine</option>
        <option value="ncrp147" ${source.method === 'ncrp147' ? 'selected' : ''}>NCRP 147 x-ray / fluoroscopy</option>
        <option value="ncrp147_ct" ${source.method === 'ncrp147_ct' ? 'selected' : ''}>NCRP 147 CT</option>
      </select>
    </div>
    <div class="field">Height above floor (m)
      <input type="number" step="0.1" value="${source.height_above_floor_m}" data-p="height_above_floor_m">
    </div>
    ${source.method === 'tg108' ? `
      <div class="field">Source type
        <select data-k="kind">
          <option value="uptake" ${p.kind === 'uptake' ? 'selected' : ''}>Uptake / waiting patient</option>
          <option value="imaging" ${p.kind === 'imaging' ? 'selected' : ''}>Imaging / scanner</option>
        </select>
      </div>
      <div class="field">Radionuclide<select data-k="nuclide">${nuclideOptions}</select></div>
      <div class="field">Administered activity (MBq)<input type="number" value="${p.administered_activity_MBq ?? 555}" data-k="administered_activity_MBq"></div>
      <div class="field">Patients per week<input type="number" value="${p.patients_per_week ?? 40}" data-k="patients_per_week"></div>
      <div class="field">Uptake time (h)<input type="number" step="0.25" value="${p.uptake_time_h ?? 1}" data-k="uptake_time_h"></div>
      ${p.kind === 'imaging' ? `
        <div class="field">Imaging time (h)<input type="number" step="0.25" value="${p.imaging_time_h ?? 0.5}" data-k="imaging_time_h"></div>
        <div class="field">Voiding factor<input type="number" step="0.05" value="${p.void_factor ?? 0.85}" data-k="void_factor"></div>
        <div class="field">Scanner self-shielding<input type="number" step="0.05" value="${p.scanner_attenuation ?? 1.0}" data-k="scanner_attenuation"></div>
        <p class="hint">1.0 takes no gantry credit. TG-108 suggests ~0.85 is realistic.</p>` : ''}
    ` : source.method === 'ncrp147' ? `
      <div class="field">Workload distribution<select data-k="workload">${workloadOptions}</select></div>
      <div class="field">Barrier type
        <select data-k="barrier_type">
          <option value="secondary" ${p.barrier_type !== 'primary' ? 'selected' : ''}>Secondary (scatter + leakage)</option>
          <option value="primary" ${p.barrier_type === 'primary' ? 'selected' : ''}>Primary</option>
        </select>
      </div>
      ${p.barrier_type === 'primary'
        ? `<div class="field">Use factor U<input type="number" step="0.01" value="${p.use_factor ?? 1}" data-k="use_factor"></div>`
        : `<div class="field">Scatter geometry
             <select data-k="scatter_geometry">
               <option value="side" ${p.scatter_geometry !== 'forward_back' ? 'selected' : ''}>Leakage + side scatter</option>
               <option value="forward_back" ${p.scatter_geometry === 'forward_back' ? 'selected' : ''}>Leakage + forward/backscatter</option>
             </select>
           </div>`}
      <div class="field">Patients per week<input type="number" placeholder="survey default" value="${p.patients_per_week ?? ''}" data-k="patients_per_week"></div>
      <p class="hint">Leave blank to use the Table 4.2 surveyed patient load.</p>
    ` : `
      <div class="field">Scatter method
        <select data-k="scatter_method">
          <option value="dlp" ${!p.scatter_method || p.scatter_method === 'dlp' ? 'selected' : ''}>DLP × κ</option>
          <option value="chart" ${p.scatter_method === 'chart' ? 'selected' : ''}>Manufacturer scatter chart</option>
          <option value="isodose" ${p.scatter_method === 'isodose' ? 'selected' : ''}>Single isodose value</option>
        </select>
      </div>
      ${p.scatter_method === 'chart'
        ? `<div class="field">Plan chart
             <select data-k="plan_map_id">
               <option value="">none</option>
               ${(state.project.scatter_maps || []).filter(m => m.plane === 'plan').map(m =>
                 `<option value="${m.id}" ${p.plan_map_id === m.id ? 'selected' : ''}>${escapeHtml(m.name)}</option>`).join('')}
             </select>
           </div>
           <div class="field">Elevation chart (used across floors)
             <select data-k="elevation_map_id">
               <option value="">none</option>
               ${(state.project.scatter_maps || []).filter(m => m.plane === 'elevation').map(m =>
                 `<option value="${m.id}" ${p.elevation_map_id === m.id ? 'selected' : ''}>${escapeHtml(m.name)}</option>`).join('')}
             </select>
           </div>
           <div class="field">Rotation on the plan (degrees)
             <input type="number" step="5" value="${source.rotation_deg || 0}" data-p="rotation_deg">
           </div>
           <p class="hint">The placed point is the <em>isocentre</em>. The orange arrow shows
             the chart's +y axis — usually the table. Rotate until it matches the drawing.</p>
           <div class="field">Source of the chart<input type="text" value="${escapeHtml(p.scatter_source || '')}" data-k="scatter_source"></div>`
        : p.scatter_method === 'isodose'
        ? `<div class="field">Isodose kerma at 1 m per procedure (mGy)<input type="number" step="0.0001" value="${p.isodose_kerma_mGy_at_1m ?? ''}" data-k="isodose_kerma_mGy_at_1m"></div>
           <div class="field">Source of the scatter data<input type="text" value="${escapeHtml(p.scatter_source || '')}" data-k="scatter_source"></div>
           <p class="hint">Isodose maps are scanner-specific, so the value and its source must be entered.</p>`
        : `<div class="field">Body region
             <select data-k="body_region">
               <option value="body" ${p.body_region !== 'head' ? 'selected' : ''}>Body (κ = 3×10⁻⁴ /cm, ×1.2)</option>
               <option value="head" ${p.body_region === 'head' ? 'selected' : ''}>Head (κ = 9×10⁻⁵ /cm)</option>
             </select>
           </div>
           <div class="field">DLP per procedure (mGy·cm)<input type="number" value="${p.dlp_per_procedure_mGy_cm ?? ''}" data-k="dlp_per_procedure_mGy_cm"></div>
           <div class="field">κ override (1/cm)<input type="number" step="0.00001" placeholder="${p.body_region === 'head' ? '9e-5' : '3e-4'}" value="${p.kappa_per_cm ?? ''}" data-k="kappa_per_cm"></div>
           <p class="hint">Leave κ blank to use the NCRP 147 value for the selected region.</p>`}
      ${chartBasis(p).includes('mAs')
        ? `<div class="field">Workload (mAs per week)<input type="number" value="${p.mas_per_week ?? ''}" data-k="mas_per_week"></div>
           <p class="hint">This chart is quoted per ${escapeHtml(chartBasis(p))}, so it scales with workload rather than a procedure count.</p>`
        : `<div class="field">Procedures per week<input type="number" value="${p.procedures_per_week ?? ''}" data-k="procedures_per_week"></div>`}
      <div class="field">kVp<input type="number" value="${p.kvp ?? 125}" data-k="kvp"></div>
    `}
    <button data-act="delete" class="wide">Delete source</button>`;

  wireInspector(box, `/api/sources/${source.id}`, source);
}

function renderPoiInspector(title, box) {
  const poi = state.project.pois.find(p => p.id === state.selection.id);
  if (!poi) return select(null);
  title.textContent = 'Point of interest';

  const presets = state.options?.occupancy || [];
  const matchesPreset = presets.some(o => Math.abs(o.factor - poi.occupancy) < 1e-9);
  const occupancyOptions = presets.map(o =>
    `<option value="${o.factor}" ${Math.abs(o.factor - poi.occupancy) < 1e-9 ? 'selected' : ''}>
       ${o.factor} — ${escapeHtml(o.description.slice(0, 60))}${o.description.length > 60 ? '…' : ''}
     </option>`).join('')
    // Only offer a custom entry when the value is not one of the presets,
    // otherwise the list shows the same factor twice.
    + (matchesPreset ? '' : `<option value="${poi.occupancy}" selected>${poi.occupancy} (custom)</option>`);

  // Distances come from /api/distances, keyed by source, for the linked sources.
  const distanceInfo = new Map(
    (state.distances?.points.find(p => p.poi_id === poi.id)?.links || [])
      .map(link => [link.source_id, link]));

  const barrierInfo = new Map(
    (state.barriers?.points.find(p => p.poi_id === poi.id)?.links || [])
      .map(link => [link.source_id, link]));

  const links = state.project.sources.map(source => {
    const floor = state.project.floors.find(f => f.id === source.floor_id);
    const checked = poi.linked_source_ids.includes(source.id);
    const info = distanceInfo.get(source.id);
    const override = poi.distance_overrides?.[source.id];
    const path = barrierInfo.get(source.id);

    let detail = '';
    if (checked && info) {
      if (info.error) {
        detail = `<div class="bad">${escapeHtml(info.error)}</div>`;
      } else {
        const parts = [];
        if (!info.same_floor || Math.abs(info.vertical_m) > 1e-6) {
          parts.push(`horizontal ${formatLength(info.horizontal_m)}`);
          parts.push(`vertical ${formatLength(Math.abs(info.vertical_m))}`);
        }
        const breakdown = parts.length ? ` (${parts.join(', ')})` : '';
        detail = override
          ? `<div class="dist overridden">Using entered ${formatLength(info.distance_m)} —
               drawing gives ${formatLength(info.geometric_m)}${breakdown}</div>`
          : `<div class="dist">From drawing: ${formatLength(info.distance_m)}${breakdown}</div>`;
        detail += `<div class="override">
            <input type="number" step="0.01" min="0" data-distance="${source.id}"
                   placeholder="${fromMetres(info.geometric_m).toFixed(2)}"
                   value="${override ? fromMetres(override).toFixed(2) : ''}">
            <span>${displayUnit()} — leave blank to use the drawing</span>
          </div>`;
      }
    }

    let barrierBlock = '';
    if (checked) {
      const found = path?.barriers || [];
      barrierBlock = found.length
        ? `<div class="barrier-list">${found.map(b => `
            <div class="barrier ${b.drawn ? '' : 'manual'}">
              ${escapeHtml(b.label)} — ${wallThicknessDisplay(b.effective_thickness_mm)}
              ${escapeHtml(b.material)}${b.drawn ? ` on ${escapeHtml(b.floor_name)}` : ' (named)'}
              ${b.oblique ? `<span class="oblique">· ${b.angle_deg}° oblique</span>` : ''}
            </div>`).join('')}</div>`
        : '<div class="no-barrier">No barrier on this path.</div>';
      barrierBlock += `<div class="override">
          <button data-add-barrier="${source.id}">Add named barrier</button>
          ${(poi.manual_barriers?.[source.id] || []).length
            ? `<button data-clear-barrier="${source.id}">Clear named</button>` : ''}
        </div>`;
    }

    return `<div class="link-row">
      <div class="top">
        <label><input type="checkbox" data-link="${source.id}" ${checked ? 'checked' : ''}>
          ${escapeHtml(source.label || source.id)}
          <span class="where">(${escapeHtml(floor?.name || '?')})</span></label>
      </div>${detail}${barrierBlock}</div>`;
  }).join('') || '<p class="hint">No sources placed yet.</p>';

  box.innerHTML = `
    <div class="field">Label<input type="text" value="${escapeHtml(poi.label)}" data-p="label"></div>
    <div class="field">Area classification
      <select data-p="area_class">
        <option value="uncontrolled" ${poi.area_class === 'uncontrolled' ? 'selected' : ''}>Uncontrolled</option>
        <option value="controlled" ${poi.area_class === 'controlled' ? 'selected' : ''}>Controlled</option>
      </select>
    </div>
    <div class="field">Occupancy factor T
      <select data-p="occupancy">${occupancyOptions}</select>
    </div>
    <div class="field inline">
      <input type="checkbox" data-p="auto_height" ${poi.auto_height ? 'checked' : ''}>
      Apply TG-108 height convention automatically
    </div>
    <p class="hint">0.5 m above the floor for a room above the source, 1.7 m for a room below.</p>
    ${poi.auto_height ? '' : `<div class="field">Height above floor (m)
      <input type="number" step="0.1" value="${poi.height_above_floor_m}" data-p="height_above_floor_m"></div>`}
    <div class="field inline">
      <input type="checkbox" data-p="offset_applied" ${poi.offset_applied ? 'checked' : ''}>
      NCRP standoff already applied
    </div>
    <p class="hint">Tick if the point you placed <em>is</em> the protected location. Leave unticked if you clicked the barrier — the app then adds the recommended 0.3 m.</p>
    <div class="field">Existing shielding in the structure
      <select data-p="existing_material">
        <option value="">none</option>
        ${(state.options?.materials || []).map(m =>
          `<option ${poi.existing_material === m ? 'selected' : ''}>${m}</option>`).join('')}
      </select>
    </div>
    ${poi.existing_material ? `<div class="field">Existing thickness (${poi.existing_material === 'lead' || poi.existing_material === 'concrete' || poi.existing_material === 'iron' ? 'cm for TG-108 materials, mm for NCRP 147' : 'mm'})
      <input type="number" step="0.1" value="${poi.existing_thickness}" data-p="existing_thickness"></div>` : ''}
    <h2 style="margin-top:14px">Contributing sources and distances</h2>
    <div>${links}</div>
    <button data-act="delete" class="wide">Delete point</button>`;

  wireInspector(box, `/api/pois/${poi.id}`, poi);

  box.querySelectorAll('[data-link]').forEach(input => {
    input.onchange = async () => {
      const chosen = [...box.querySelectorAll('[data-link]')]
        .filter(i => i.checked).map(i => i.dataset.link);
      setProject(await send(`/api/pois/${poi.id}`, 'PATCH', { linked_source_ids: chosen }));
    };
  });

  box.querySelectorAll('[data-add-barrier]').forEach(button => {
    button.onclick = async () => {
      const material = prompt(
        `Barrier material (${(state.options?.materials || []).join(', ')}):`, 'lead');
      if (!material) return;
      const entered = parseFloat(prompt(
        `Thickness in ${wallUnitLabel()}:`, displayUnit() === 'm' ? '2' : '0.08'));
      if (!entered || entered <= 0) return;
      const thicknessMm = displayUnit() === 'm' ? entered : entered * 25.4;
      const label = prompt('Name this barrier (optional):', '') || '';
      const declared = { ...(poi.manual_barriers || {}) };
      const sourceId = button.dataset.addBarrier;
      declared[sourceId] = [...(declared[sourceId] || []),
                            { material, thickness_mm: thicknessMm, label }];
      try { setProject(await send(`/api/pois/${poi.id}`, 'PATCH', { manual_barriers: declared })); }
      catch (error) { alert(error.message); }
    };
  });

  box.querySelectorAll('[data-clear-barrier]').forEach(button => {
    button.onclick = async () => {
      const declared = { ...(poi.manual_barriers || {}) };
      delete declared[button.dataset.clearBarrier];
      setProject(await send(`/api/pois/${poi.id}`, 'PATCH', { manual_barriers: declared }));
    };
  });

  box.querySelectorAll('[data-distance]').forEach(input => {
    input.onchange = async () => {
      // Overrides are stored in metres; the field is in the display unit.
      const overrides = { ...(poi.distance_overrides || {}) };
      const entered = parseFloat(input.value);
      if (input.value === '' || Number.isNaN(entered)) {
        delete overrides[input.dataset.distance];
      } else if (entered <= 0) {
        alert('Distance must be greater than zero.');
        return;
      } else {
        overrides[input.dataset.distance] = toMetres(entered);
      }
      try {
        setProject(await send(`/api/pois/${poi.id}`, 'PATCH', { distance_overrides: overrides }));
      } catch (error) { alert(error.message); }
    };
  });
}

function wireInspector(box, path, item) {
  const patch = async body => {
    try { setProject(await send(path, 'PATCH', body)); }
    catch (error) { alert(error.message); }
  };

  box.querySelectorAll('[data-p]').forEach(input => {
    input.onchange = () => {
      const key = input.dataset.p;
      const value = input.type === 'checkbox' ? input.checked
        : input.type === 'number' ? parseFloat(input.value) || 0
        : input.value;
      patch({ [key]: value });
    };
  });

  box.querySelectorAll('[data-k]').forEach(input => {
    input.onchange = () => {
      const params = { ...(item.params || {}) };
      const key = input.dataset.k;
      params[key] = input.type === 'number'
        ? (input.value === '' ? null : parseFloat(input.value))
        : input.value;
      patch({ params });
    };
  });

  const remove = box.querySelector('[data-act=delete]');
  if (remove) remove.onclick = async () => {
    setProject(await api(path, { method: 'DELETE' }));
    select(null);
  };
}

/* ------------------------------------------------------------------ results */

async function calculate() {
  const payload = await api('/api/results');
  state.results = payload;
  const box = document.getElementById('results');
  const note = document.getElementById('results-note');
  note.textContent = payload.problems.length
    ? `${payload.problems.length} issue(s) — see the left panel`
    : '';

  if (!payload.results.length) {
    box.innerHTML = '<p class="hint">No points of interest to evaluate.</p>';
    return;
  }

  const materials = payload.materials;
  let html = `<table><thead><tr>
    <th>Point</th><th>Floor</th><th>Source</th><th>Method</th>
    <th class="num">Distance (m)</th><th class="num">Weekly</th><th class="num">B</th>
    ${materials.map(m => `<th class="num">${m} (mm)</th>`).join('')}
    <th>Status</th></tr></thead><tbody>`;

  for (const result of payload.results) {
    for (const contribution of result.contributions) {
      html += `<tr class="contribution">
        <td>${escapeHtml(result.label)}</td><td>${escapeHtml(result.floor_name)}</td>
        <td>${escapeHtml(contribution.label)}</td><td>${contribution.method}</td>
        <td class="num">${contribution.distance_m.toFixed(2)}${
          contribution.geometric_distance_m
            ? ` <span class="tag need">entered</span>` : ''}</td>
        <td class="num">${fmt(contribution.value)} ${contribution.quantity.includes('uSv') ? 'µSv' : 'mGy'}</td>
        <td class="num">${contribution.path_transmission < 1
          ? `<span class="tag need">×${contribution.path_transmission.toFixed(3)}</span>` : ''}</td>
        ${materials.map(() => '<td></td>').join('')}
        <td>${contribution.barriers.length
          ? `${contribution.barriers.length} barrier(s), ${contribution.path_equivalent_mm.toFixed(1)} mm Pb eq`
          : ''}</td></tr>`;
    }
    for (const method of result.methods) {
      const transmission = method.required_transmission;
      html += `<tr class="total">
        <td>${escapeHtml(result.label)}</td><td>${escapeHtml(result.floor_name)}</td>
        <td>all sources</td><td>${method.method}</td><td class="num"></td>
        <td class="num">${fmt(method.total)}</td>
        <td class="num">${transmission === null || transmission > 1e6 ? '—' : transmission.toFixed(4)}</td>
        ${materials.map(m => `<td class="num">${(method.thickness_mm[m] ?? 0).toFixed(2)}</td>`).join('')}
        <td>${transmission > 1
          ? '<span class="tag ok">none needed</span>'
          : '<span class="tag need">shielding</span>'}</td></tr>`;
    }
    const messages = [
      ...result.errors.map(m => `<div class="msg error">${escapeHtml(m)}</div>`),
      ...result.warnings.map(m => `<div class="msg warn">${escapeHtml(m)}</div>`),
    ].join('');
    const audit = result.contributions.map(c =>
      `${c.label} at ${c.distance_m.toFixed(2)} m\n` +
      Object.entries(c.terms).map(([k, v]) => `    ${k} = ${fmt(v)}`).join('\n') +
      (c.notes.length ? `\n    ${c.notes.join('\n    ')}` : '')).join('\n\n');
    if (messages || audit) {
      html += `<tr><td colspan="${7 + materials.length}">${messages}
        <details><summary>Calculation detail for ${escapeHtml(result.label)}</summary>
        <pre>${escapeHtml(audit)}</pre></details></td></tr>`;
    }
  }
  box.innerHTML = html + '</tbody></table>';
}

const fmt = value =>
  typeof value !== 'number' ? '' :
  Math.abs(value) >= 1000 || (Math.abs(value) < 0.01 && value !== 0)
    ? value.toExponential(2) : value.toFixed(2);

const escapeHtml = text => String(text ?? '').replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* -------------------------------------------------------------------- setup */

document.querySelectorAll('.tool').forEach(button => {
  button.onclick = () => setTool(button.dataset.tool);
});
document.getElementById('btn-zoom-in').onclick = () => zoomBy(1.25);
document.getElementById('btn-zoom-out').onclick = () => zoomBy(1 / 1.25);
document.getElementById('btn-zoom-fit').onclick = () => { fitToView(); draw(); };
document.getElementById('ghost').onchange = event => { state.ghost = event.target.checked; draw(); };
document.getElementById('obliquity').onchange = async event => {
  setProject(await send('/api/project/obliquity', 'POST', { enabled: event.target.checked }));
  if (state.results) calculate();
};
document.getElementById('btn-calculate').onclick = () => calculate().catch(e => alert(e.message));

const mapDialog = document.getElementById('map-dialog');
document.getElementById('btn-add-map').onclick = () => mapDialog.showModal();
document.getElementById('map-import').onclick = async event => {
  const grid = document.getElementById('map-grid').value;
  if (!grid.trim()) return;
  event.preventDefault();
  try {
    setProject(await send('/api/scatter-maps', 'POST', {
      name: document.getElementById('map-name').value,
      plane: document.getElementById('map-plane').value,
      coordinate_unit: document.getElementById('map-coord-unit').value,
      value_unit: document.getElementById('map-value-unit').value,
      per: document.getElementById('map-per').value,
      source: document.getElementById('map-source').value,
      grid,
    }));
    document.getElementById('map-grid').value = '';
    document.getElementById('map-name').value = '';
    mapDialog.close();
  } catch (error) { alert(error.message); }
};

document.getElementById('btn-add-floor').onclick = () => document.getElementById('pdf-file').click();
document.getElementById('pdf-file').onchange = async event => {
  const file = event.target.files[0];
  if (!file) return;
  const body = new FormData();
  body.append('file', file);
  try {
    setProject(await api('/api/floors', { method: 'POST', body }));
  } catch (error) { alert(error.message); }
  event.target.value = '';
};

document.getElementById('btn-spacing').onclick = async () => {
  const raw = document.getElementById('spacing').value.split(/[,\s]+/).filter(Boolean);
  try {
    setProject(await send('/api/floors/spacing', 'POST', { heights: raw.map(Number) }));
  } catch (error) { alert(error.message); }
};

document.getElementById('display-unit').onchange = async event =>
  setProject(await send('/api/project/display-unit', 'POST', { unit: event.target.value }));

document.getElementById('project-name').onchange = async event =>
  setProject(await send('/api/project/name', 'POST', { name: event.target.value }));

document.getElementById('btn-new').onclick = async () => {
  if (!confirm('Discard the current project?')) return;
  state.images.clear();
  setProject(await send('/api/project/new', 'POST'));
};

document.getElementById('btn-save').onclick = async () => {
  const path = prompt('Save project to path:', state.project.path || 'project.rsproj');
  if (!path) return;
  try {
    const result = await send('/api/project/save', 'POST', { path });
    setStatus(`Saved to ${result.saved}`);
  } catch (error) { alert(error.message); }
};

document.getElementById('btn-open').onclick = () => document.getElementById('load-file').click();
document.getElementById('load-file').onchange = async event => {
  const file = event.target.files[0];
  if (!file) return;
  const body = new FormData();
  body.append('file', file);
  try {
    state.images.clear();
    state.floorId = null;
    setProject(await api('/api/project/upload', { method: 'POST', body }));
  } catch (error) { alert(error.message); }
  event.target.value = '';
};

window.addEventListener('resize', () => draw());

(async function start() {
  state.options = await api('/api/options');
  setProject(await api('/api/project'));
  document.getElementById('project-name').value = state.project.name;
  document.getElementById('display-unit').value = displayUnit();
  document.getElementById('obliquity').checked = !!state.project.apply_obliquity;
  setTool('select');
})();
