import mapboxgl from 'mapbox-gl';
import {MapboxOverlay} from '@deck.gl/mapbox';
import {BitmapLayer, IconLayer} from 'deck.gl';
import routes from '../../analysis/data/infrastructure/routes_from_api.json';
import levels from '../../analysis/data/infrastructure/levels_from_api.json';

// Wind speed color ramp (m/s) — shared by legend, BitmapLayer, and IconLayer
const WIND_COLOR_STOPS = [
  { ms: 0,  rgb: [30,  64,  175], label: '0'    },
  { ms: 5,  rgb: [14,  165, 233], label: '5 m/s' },
  { ms: 10, rgb: [6,   182, 212], label: '10'   },
  { ms: 15, rgb: [163, 230, 53],  label: '15'   },
  { ms: 20, rgb: [250, 204, 21],  label: '20'   },
  { ms: 25, rgb: [249, 115, 22],  label: '25'   },
  { ms: 30, rgb: [239, 68,  68],  label: '30+'  },
];

function speedToRgb(speed) {
  for (let i = 0; i < WIND_COLOR_STOPS.length - 1; i++) {
    const {ms: s0, rgb: c0} = WIND_COLOR_STOPS[i];
    const {ms: s1, rgb: c1} = WIND_COLOR_STOPS[i + 1];
    if (speed <= s1) {
      const t = (speed - s0) / (s1 - s0);
      return c0.map((v, j) => Math.round(v + t * (c1[j] - v)));
    }
  }
  return WIND_COLOR_STOPS[WIND_COLOR_STOPS.length - 1].rgb;
}

// Arrow SVG pointing south — getAngle = -direction maps met convention correctly
const ARROW_SVG_URL =
  'data:image/svg+xml;charset=utf-8,' +
  encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="32" height="32">' +
    '<polygon points="16,29 24,13 19,13 19,3 13,3 13,13 8,13" fill="white"/>' +
    '</svg>',
  );

mapboxgl.accessToken = process.env.MAPBOX_TOKEN;

const LEVELS = {
  1: { label: '69 kV',      color: '#8AC690' },
  2: { label: '115–138 kV', color: '#A1D09C' },
  3: { label: '230 kV',     color: '#B7DAA9' },
  4: { label: '345 kV',     color: '#CEE4B5' },
  5: { label: '500+ kV',    color: '#E5EEC1' },
};

const visible = { 1: true, 2: true, 3: true, 4: true, 5: true };

// ── Map ───────────────────────────────────────────────────────────────────────
const map = new mapboxgl.Map({
  container: 'map',
  style: 'mapbox://styles/jacksonkoehler11/cmqs965gr002s01qohn60d8lh',
  center: [-71.8, 42.5],
  zoom: 7,
});

map.addControl(new mapboxgl.NavigationControl({ showCompass: false }), 'top-right');

// ── DOM refs ──────────────────────────────────────────────────────────────────
const $legend      = document.getElementById('legend');
const $stats       = document.getElementById('stats');
const $tooltip     = document.getElementById('tooltip');
const $playBtn     = document.getElementById('radar-play');
const $slider      = document.getElementById('radar-slider');
const $timestamp   = document.getElementById('radar-timestamp');
const $windToggle  = document.getElementById('wind-toggle');
const $windOpacity = document.getElementById('wind-opacity');

// ── Wind state ────────────────────────────────────────────────────────────────
let WIND_FRAMES      = [];
let windCache        = {};
let windVisible      = true;
let windFrameIdx     = 0;
let windFeatures     = [];   // all grid points (obs + interpolated)
let windArrowFeatures = [];  // obs points only — arrow layer
let windBitmapUrl    = null; // data URL for BitmapLayer
let windBounds       = null; // [W, S, E, N]
let deckOverlay      = null;

// ── Bitmap builder ────────────────────────────────────────────────────────────
// Renders gust strength as a smooth color field. The canvas is 8× the source
// grid in each dimension; every pixel is an exact bilinear blend of its four
// surrounding grid values, so there are no visible cell boundaries at any zoom.
function buildBitmapUrl(features) {
  if (!features.length) return null;

  const latSet = new Set(), lonSet = new Set();
  for (const f of features) {
    latSet.add(f.geometry.coordinates[1]);
    lonSet.add(f.geometry.coordinates[0]);
  }
  const lats = [...latSet].sort((a, b) => a - b);
  const lons = [...lonSet].sort((a, b) => a - b);
  const nLat = lats.length, nLon = lons.length;

  const latIdx = new Map(lats.map((v, i) => [v, i]));
  const lonIdx = new Map(lons.map((v, i) => [v, i]));

  // Use gust; fall back to speed if gust is null/zero
  const gustGrid = new Float32Array(nLat * nLon);
  for (const f of features) {
    const [lon, lat] = f.geometry.coordinates;
    const i = latIdx.get(lat), j = lonIdx.get(lon);
    if (i !== undefined && j !== undefined) {
      const p = f.properties;
      gustGrid[i * nLon + j] = (p.gust != null && p.gust > 0) ? p.gust : (p.speed ?? 0);
    }
  }

  // Render at 8× grid resolution — each output pixel is a bilinear blend of
  // the 4 surrounding source grid cells, giving a perfectly smooth field.
  const SCALE = 8;
  const W = (nLon - 1) * SCALE + 1;
  const H = (nLat - 1) * SCALE + 1;

  const canvas = document.createElement('canvas');
  canvas.width  = W;
  canvas.height = H;
  const ctx  = canvas.getContext('2d');
  const img  = ctx.createImageData(W, H);
  const data = img.data;

  for (let py = 0; py < H; py++) {
    // py=0 = canvas top = north; gustGrid row 0 = south — flip
    const fy = (H - 1 - py) / (H - 1) * (nLat - 1);
    const i0 = Math.min(Math.floor(fy), nLat - 2);
    const ty = fy - i0;

    for (let px = 0; px < W; px++) {
      const fx = px / (W - 1) * (nLon - 1);
      const j0 = Math.min(Math.floor(fx), nLon - 2);
      const tx = fx - j0;

      const v =
        gustGrid[ i0      * nLon + j0    ] * (1 - tx) * (1 - ty) +
        gustGrid[ i0      * nLon + j0 + 1] *      tx  * (1 - ty) +
        gustGrid[(i0 + 1) * nLon + j0    ] * (1 - tx) *      ty  +
        gustGrid[(i0 + 1) * nLon + j0 + 1] *      tx  *      ty;

      const [r, g, b] = speedToRgb(v);
      const idx = (py * W + px) * 4;
      data[idx]     = r;
      data[idx + 1] = g;
      data[idx + 2] = b;
      data[idx + 3] = 210;
    }
  }
  ctx.putImageData(img, 0, 0);

  windBounds = [lons[0], lats[0], lons[nLon - 1], lats[nLat - 1]];
  return canvas.toDataURL('image/png');
}

// ── Wind layers ───────────────────────────────────────────────────────────────
function buildWindLayers(opacity) {
  if (!windVisible || !windBitmapUrl || !windBounds) return [];
  return [
    new BitmapLayer({
      id:      'wind-bitmap',
      bounds:  windBounds,
      image:   windBitmapUrl,
      opacity: opacity * 0.75,
    }),
    new IconLayer({
      id:          'wind-arrows',
      data:        windArrowFeatures,
      getPosition: (d) => d.geometry.coordinates,
      getIcon:     () => ({
        url: ARROW_SVG_URL, width: 32, height: 32,
        anchorX: 16, anchorY: 16, mask: true,
      }),
      getAngle:   (d) => -d.properties.direction,
      getSize:    (d) => Math.max(18, d.properties.speed * 3.5),
      getColor:   (d) => speedToRgb(d.properties.speed),
      billboard:  false,
      opacity:    opacity * 0.95,
      sizeScale:  1,
      sizeUnits:  'pixels',
      pickable:   true,
      onHover: ({object, x, y}) => {
        if (object) {
          const p = object.properties;
          $tooltip.innerHTML =
            `<strong>Wind</strong>` +
            (p.gust ? `<span>Gust ${p.gust.toFixed(1)} m/s</span>` : '') +
            `<span>${p.speed.toFixed(1)} m/s sustained · ${Math.round(p.direction)}° from</span>`;
          $tooltip.classList.add('show');
          $tooltip.style.transform =
            `translate(${Math.min(x + 14, window.innerWidth - 340)}px, ${y + 14}px)`;
        } else {
          $tooltip.classList.remove('show');
        }
      },
    }),
  ];
}

// ── Wind loading ──────────────────────────────────────────────────────────────
async function loadWindFrame(idx) {
  if (!WIND_FRAMES.length || !deckOverlay) return;
  const frame = WIND_FRAMES[idx];
  if (!frame) return;
  const url = `./data/wind/${frame.file}`;
  if (!windCache[url]) {
    const resp = await fetch(url);
    windCache[url] = await resp.json();
  }
  windFeatures      = windCache[url].features;
  windArrowFeatures = windFeatures.filter((f) => f.properties.kind !== 'interp');
  windBitmapUrl     = buildBitmapUrl(windFeatures);
  deckOverlay.setProps({ layers: buildWindLayers(Number($windOpacity.value) / 100) });
}

async function loadWindManifest() {
  try {
    const resp = await fetch('./data/wind/manifest.json');
    if (!resp.ok) return;
    const mf = await resp.json();
    WIND_FRAMES = mf.frames;
    $slider.max = WIND_FRAMES.length - 1;
    $timestamp.textContent = WIND_FRAMES[0].label;
    addWindLegend();
    await loadWindFrame(0);
  } catch (e) {
    console.warn('Wind: no manifest — run wind_agent.py first', e);
    $windToggle.style.display  = 'none';
    document.getElementById('wind-opacity-label').style.display = 'none';
  }
}

function addWindLegend() {
  const section = document.createElement('div');
  section.id = 'wind-legend';
  section.innerHTML =
    '<h2 style="margin-top:14px">Wind Gust (m/s)</h2>' +
    WIND_COLOR_STOPS.map(
      (s) => `<div class="legend-item" style="cursor:default">
        <span class="wind-speed-swatch" style="background:rgb(${s.rgb})"></span>
        <span>${s.label}</span>
      </div>`,
    ).join('');
  $legend.appendChild(section);
}

// ── Voltage legend ────────────────────────────────────────────────────────────
$legend.innerHTML =
  '<h2>Voltage</h2>' +
  Object.entries(LEVELS)
    .map(
      ([lvl, { label, color }]) =>
        `<div class="legend-item" data-level="${lvl}">
          <span class="legend-swatch" style="background:${color}"></span>
          <span>${label}</span>
        </div>`,
    )
    .join('');

$legend.querySelectorAll('.legend-item').forEach((el) => {
  el.addEventListener('click', () => {
    const lvl = Number(el.dataset.level);
    visible[lvl] = !visible[lvl];
    el.classList.toggle('hidden', !visible[lvl]);
    applyFilter();
  });
});

function visibleFilter() {
  const shown = Object.entries(visible).filter(([, v]) => v).map(([k]) => Number(k));
  return ['in', ['get', 'level'], ['literal', shown]];
}

function applyFilter() {
  if (!map.getLayer('routes-line')) return;
  const f = visibleFilter();
  map.setFilter('routes-line', f);
  map.setFilter('routes-hit',  f);
}

// ── Map load ──────────────────────────────────────────────────────────────────
map.on('load', () => {
  // ── deck.gl overlay (non-interleaved = separate canvas, always on top) ─────
  // interleaved:false is a separate WebGL canvas rendered over the Mapbox canvas.
  // This guarantees the wind layer is visible regardless of Mapbox layer order.
  deckOverlay = new MapboxOverlay({ interleaved: false, layers: [] });
  map.addControl(deckOverlay);

  loadWindManifest();

  $windToggle.addEventListener('click', () => {
    windVisible = !windVisible;
    $windToggle.classList.toggle('off', !windVisible);
    deckOverlay.setProps({ layers: buildWindLayers(Number($windOpacity.value) / 100) });
    const s = document.getElementById('wind-legend');
    if (s) s.style.opacity = windVisible ? '1' : '0.4';
  });

  $windOpacity.addEventListener('input', () => {
    deckOverlay.setProps({ layers: buildWindLayers(Number($windOpacity.value) / 100) });
  });

  // ── Routes ────────────────────────────────────────────────────────────────
  const firstSymbolId = map.getStyle().layers.find((l) => l.type === 'symbol')?.id;

  map.addSource('routes', {
    type: 'geojson', tolerance: 1, buffer: 0, promoteId: 'id',
    data: { type: 'FeatureCollection', features: [] },
  });

  map.addLayer(
    {
      id: 'routes-line', type: 'line', source: 'routes',
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: {
        'line-color': [
          'match', ['get', 'level'],
          1, LEVELS[1].color, 2, LEVELS[2].color, 3, LEVELS[3].color,
          4, LEVELS[4].color, 5, LEVELS[5].color, '#94a3b8',
        ],
        'line-width': [
          'interpolate', ['linear'], ['zoom'],
          4,  ['match', ['get', 'level'], 1, 0.4, 2, 0.6, 3, 0.9, 4, 1.4, 5, 2.0, 0.4],
          9,  ['match', ['get', 'level'], 1, 0.7, 2, 1.2, 3, 1.8, 4, 2.5, 5, 3.5, 0.7],
          14, ['match', ['get', 'level'], 1, 1.5, 2, 2.5, 3, 3.5, 4, 5.0, 5, 7.0, 1.5],
        ],
        'line-opacity': ['case', ['boolean', ['feature-state', 'hover'], false], 1, 0.85],
      },
    },
    firstSymbolId,
  );

  map.addLayer({
    id: 'routes-hit', type: 'line', source: 'routes',
    layout: { 'line-cap': 'round' },
    paint: { 'line-width': 16, 'line-opacity': 0 },
  });

  map.getSource('routes').setData({
    type: 'FeatureCollection',
    features: routes.map((route) => ({
      type: 'Feature',
      id: route.id,
      properties: {
        id: route.id, level: levels[route.id] ?? route.level,
        name: route.name, voltage: route.voltage,
        operator: route.operator, number: route.number,
      },
      geometry: {
        type: 'LineString',
        coordinates: route.path.map(([lon, lat]) => [lon, lat]),
      },
    })),
  });

  $stats.textContent = `${routes.length.toLocaleString()} transmission lines`;

  // ── Route hover tooltip ───────────────────────────────────────────────────
  let hoveredId = null;

  map.on('mousemove', 'routes-hit', (e) => {
    if (!e.features.length) return;
    const feat = e.features[0];
    if (hoveredId !== null && hoveredId !== feat.id) {
      map.setFeatureState({ source: 'routes', id: hoveredId }, { hover: false });
    }
    hoveredId = feat.id;
    map.setFeatureState({ source: 'routes', id: hoveredId }, { hover: true });
    map.getCanvas().style.cursor = 'pointer';
    const { name, voltage, operator } = feat.properties;
    const kv = Math.round(parseInt(voltage.split(';')[0], 10) / 1000);
    $tooltip.innerHTML = `<strong>${name}</strong><span>${kv} kV · ${operator}</span>`;
    $tooltip.classList.add('show');
    $tooltip.style.transform =
      `translate(${Math.min(e.point.x + 14, window.innerWidth - 340)}px, ${e.point.y + 14}px)`;
  });

  map.on('mouseleave', 'routes-hit', () => {
    if (hoveredId !== null) {
      map.setFeatureState({ source: 'routes', id: hoveredId }, { hover: false });
    }
    hoveredId = null;
    map.getCanvas().style.cursor = '';
    $tooltip.classList.remove('show');
  });

  // ── Wind timeline controls ────────────────────────────────────────────────
  let playing = false;
  let timer   = null;

  async function setFrame(i) {
    windFrameIdx = Math.max(0, Math.min(i, WIND_FRAMES.length - 1));
    const frame = WIND_FRAMES[windFrameIdx];
    if (!frame) return;
    $timestamp.textContent = frame.label;
    $slider.value = windFrameIdx;
    await loadWindFrame(windFrameIdx);
  }

  function pause() {
    playing = false;
    clearInterval(timer);
    timer = null;
    $playBtn.innerHTML = '&#9654;';
  }

  function play() {
    playing = true;
    $playBtn.innerHTML = '&#9646;&#9646;';
    if (windFrameIdx >= WIND_FRAMES.length - 1) setFrame(0);
    timer = setInterval(() => {
      if (windFrameIdx >= WIND_FRAMES.length - 1) { pause(); return; }
      setFrame(windFrameIdx + 1);
    }, 300);
  }

  $playBtn.addEventListener('click',  () => (playing ? pause() : play()));
  $slider.addEventListener('input',   () => { pause(); setFrame(Number($slider.value)); });
});
