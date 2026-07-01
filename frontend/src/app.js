import mapboxgl from 'mapbox-gl';
import {MapboxOverlay} from '@deck.gl/mapbox';
import {IconLayer, PathLayer} from 'deck.gl';
import routes from '../../analysis/data/infrastructure/routes_from_api.json';
import levels from '../../analysis/data/infrastructure/levels_from_api.json';

// Precipitation color ramp (mm/hr) — violet/purple palette
const PRECIP_COLOR_STOPS = [
  { mm: 0,   rgb: [245, 243, 255], label: '0'       },
  { mm: 1,   rgb: [221, 214, 254], label: '1 mm/hr' },
  { mm: 3,   rgb: [167, 139, 250], label: '3'       },
  { mm: 7,   rgb: [124, 58,  237], label: '7'       },
  { mm: 15,  rgb: [91,  33,  182], label: '15'      },
  { mm: 30,  rgb: [46,  16,  101], label: '30+'     },
];

function precipToRgb(mm) {
  for (let i = 0; i < PRECIP_COLOR_STOPS.length - 1; i++) {
    const {mm: m0, rgb: c0} = PRECIP_COLOR_STOPS[i];
    const {mm: m1, rgb: c1} = PRECIP_COLOR_STOPS[i + 1];
    if (mm <= m1) {
      const t = (mm - m0) / (m1 - m0);
      return c0.map((v, j) => Math.round(v + t * (c1[j] - v)));
    }
  }
  return PRECIP_COLOR_STOPS[PRECIP_COLOR_STOPS.length - 1].rgb;
}

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

function hexToRgb(hex) {
  return [parseInt(hex.slice(1, 3), 16), parseInt(hex.slice(3, 5), 16), parseInt(hex.slice(5, 7), 16)];
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
  0: { label: '< 69 kV',   color: '#4a6b67' },
  1: { label: '69 kV',     color: '#a3e635' },
  2: { label: '115 kV',    color: '#4ade80' },
  3: { label: '138 kV',    color: '#10b981' },
  4: { label: '230 kV',    color: '#2dd4bf' },
  5: { label: '345+ kV',   color: '#d9f99d' },
};

const visible = { 0: true, 1: true, 2: true, 3: true, 4: true, 5: true };

// Altitude (m) per level: log10(kV) normalised to 0–6000 m
// Keeps 115/138 kV close together; big kV jumps get proportional vertical space
const LEVEL_ALTITUDE = (() => {
  const kv = [12, 69, 115, 138, 230, 345];
  const lo = Math.log10(kv[0]), hi = Math.log10(kv[5]);
  return Object.fromEntries(kv.map((v, i) => [i, Math.round((Math.log10(v) - lo) / (hi - lo) * 6000)]));
})();

// Pre-build route features with level-split logic applied
const routeFeatures = routes.map((route) => {
  const assignedLevel = levels[route.id] ?? route.level;
  const primaryVolt   = parseInt((route.voltage ?? '0').split(';')[0], 10);
  const displayLevel  = (assignedLevel === 1 && primaryVolt > 0 && primaryVolt < 69000) ? 0 : assignedLevel;
  return {
    id: route.id,
    level: displayLevel,
    name: route.name,
    voltage: route.voltage,
    operator: route.operator,
    number: route.number,
    path: route.path,
  };
});
let filteredRouteFeatures = routeFeatures;

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
const $stormName   = document.getElementById('radar-storm-name');
const $windToggle    = document.getElementById('wind-toggle');
const $windOpacity   = document.getElementById('wind-opacity');
const $precipToggle  = document.getElementById('precip-toggle');
const $precipOpacity = document.getElementById('precip-opacity');

// ── Wind state ────────────────────────────────────────────────────────────────
let WIND_FRAMES      = [];
let windCache        = {};
let windVisible      = true;
let windFrameIdx     = 0;
let windFeatures     = [];
let windArrowFeatures = [];
let windBitmapUrl    = null;
let windBounds       = null;
let deckOverlay      = null;

// ── Precipitation state ───────────────────────────────────────────────────────
let PRECIP_FRAMES    = [];
let precipCache      = {};
let precipVisible    = true;
let precipFrameIdx   = 0;
let precipBitmapUrl  = null;
let precipBounds     = null;

// ── Elevation state ───────────────────────────────────────────────────────────
let elevated3D = false;

// ── Outage state ──────────────────────────────────────────────────────────────
let OUTAGE_DATA      = null;
let OUTAGE_FRAMES    = [];
let outageFrameIdx   = 0;
let activeOutageIds  = new Set();
let fipsToLines      = new Map();   // fips (number) → [routeId, ...]

// ── Mercator helpers ──────────────────────────────────────────────────────────
// The bitmap must be built in Mercator Y space so pixels align with the map
// at every zoom level. Without this, equirectangular pixel rows drift relative
// to geographic features when zooming.
function latToMercY(lat) {
  return Math.log(Math.tan(Math.PI / 4 + lat * Math.PI / 360));
}
function mercYToLat(y) {
  return (2 * Math.atan(Math.exp(y)) - Math.PI / 2) * 180 / Math.PI;
}

// ── Bitmap builder ────────────────────────────────────────────────────────────
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

  const gustGrid = new Float32Array(nLat * nLon);
  for (const f of features) {
    const [lon, lat] = f.geometry.coordinates;
    const i = latIdx.get(lat), j = lonIdx.get(lon);
    if (i !== undefined && j !== undefined) {
      const p = f.properties;
      gustGrid[i * nLon + j] = (p.gust != null && p.gust > 0) ? p.gust : (p.speed ?? 0);
    }
  }

  const SCALE = 8;
  const W = (nLon - 1) * SCALE + 1;
  const H = (nLat - 1) * SCALE + 1;

  const canvas = document.createElement('canvas');
  canvas.width  = W;
  canvas.height = H;
  const ctx  = canvas.getContext('2d');
  const img  = ctx.createImageData(W, H);
  const data = img.data;

  // Precompute Mercator Y bounds so each row maps to the correct geographic lat
  const mercYNorth = latToMercY(lats[nLat - 1]);
  const mercYSouth = latToMercY(lats[0]);
  const latSpan    = lats[nLat - 1] - lats[0];

  for (let py = 0; py < H; py++) {
    // Linear in Mercator Y (north at top) → geographic lat → grid index
    const mercY  = mercYNorth - (py / (H - 1)) * (mercYNorth - mercYSouth);
    const geoLat = mercYToLat(mercY);
    const fy     = (geoLat - lats[0]) / latSpan * (nLat - 1);
    const i0     = Math.min(Math.max(Math.floor(fy), 0), nLat - 2);
    const ty     = fy - i0;

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
      data[idx + 3] = Math.min(210, (v / 10) ** 2 * 210);
    }
  }
  ctx.putImageData(img, 0, 0);

  windBounds = [lons[0], lats[0], lons[nLon - 1], lats[nLat - 1]];
  return canvas.toDataURL('image/png');
}

// ── Wind arrow layer (deck.gl — heatmap is now a Mapbox image source) ─────────
function buildArrowLayer(opacity) {
  if (!windVisible || !windArrowFeatures.length) return [];
  return [
    new IconLayer({
      id:          'wind-arrows',
      data:        windArrowFeatures,
      getPosition: (d) => d.geometry.coordinates,
      getIcon:     () => ({
        url: ARROW_SVG_URL, width: 32, height: 32,
        anchorX: 16, anchorY: 16, mask: true,
      }),
      getAngle:   (d) => -d.properties.direction,
      getSize:    (d) => Math.pow(d.properties.speed, 1.3) * 0.6,
      getColor:   (d) => { const v = d.properties.gust ?? d.properties.speed; return [...speedToRgb(v), Math.min(160, (v / 10) ** 2 * 160)]; },
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

// ── Transmission line deck.gl layers ─────────────────────────────────────────
function buildRouteLayers() {
  return [
    new PathLayer({
      id: 'transmission-routes',
      data: filteredRouteFeatures,
      getPath: (d) => d.path.map(([lon, lat]) => [lon, lat, elevated3D ? (LEVEL_ALTITUDE[d.level] ?? 0) : 0]),
      getColor: (d) => {
        if (activeOutageIds.has(String(d.id))) return [239, 68, 68, 242];
        return [...hexToRgb(LEVELS[d.level]?.color ?? '#94a3b8'), 217];
      },
      getWidth: (d) => [1.5, 2, 2.5, 3.2, 4.2, 5.5][d.level] ?? 1.5,
      widthUnits: 'pixels',
      widthMinPixels: 0.5,
      capRounded: true,
      jointRounded: true,
      pickable: false,
      parameters: { depthTest: false },
      updateTriggers: {
        getPath: [elevated3D],
        getColor: [activeOutageIds],
      },
    }),
  ];
}

function buildAllLayers() {
  return [...buildRouteLayers(), ...buildArrowLayer(Number($windOpacity.value) / 100)];
}

// ── Wind loading ──────────────────────────────────────────────────────────────
function _imageCoords(bounds) {
  const [w, s, e, n] = bounds;
  return [[w, n], [e, n], [e, s], [w, s]];
}

async function loadWindFrame(idx) {
  if (!WIND_FRAMES.length) return;
  const frame = WIND_FRAMES[idx];
  if (!frame) return;
  const url = `./data/wind/${frame.file}`;
  if (!windCache[url]) {
    const resp = await fetch(url);
    windCache[url] = await resp.json();
  }
  windFeatures      = windCache[url].features;
  windArrowFeatures = windFeatures.filter((f) => f.properties.kind !== 'interp');
  const dataUrl = buildBitmapUrl(windFeatures);  // also sets windBounds
  if (dataUrl && windBounds && map.getSource('wind-image')) {
    map.getSource('wind-image').updateImage({ url: dataUrl, coordinates: _imageCoords(windBounds) });
    if (map.getLayer('wind-raster')) {
      map.setPaintProperty('wind-raster', 'raster-opacity',
        windVisible ? Number($windOpacity.value) / 100 * 0.75 : 0);
    }
  }
  if (deckOverlay) {
    deckOverlay.setProps({ layers: buildAllLayers() });
  }
}

async function loadWindManifest() {
  try {
    const resp = await fetch('./data/wind/manifest.json');
    if (!resp.ok) return;
    const mf = await resp.json();
    WIND_FRAMES = mf.frames;
    addWindLegend();
    // Load first frame as a static backdrop — slider is owned by outage data
    await loadWindFrame(0);
  } catch (e) {
    console.warn('Wind: no manifest — run wind_agent.py first', e);
    $windToggle.style.display  = 'none';
    document.getElementById('wind-opacity-label').style.display = 'none';
  }
}

// ── Precipitation bitmap & layers ─────────────────────────────────────────────
function buildPrecipBitmapUrl(features) {
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

  const precipGrid = new Float32Array(nLat * nLon);
  for (const f of features) {
    const [lon, lat] = f.geometry.coordinates;
    const i = latIdx.get(lat), j = lonIdx.get(lon);
    if (i !== undefined && j !== undefined) {
      precipGrid[i * nLon + j] = f.properties.precipitation ?? 0;
    }
  }

  const SCALE = 8;
  const W = (nLon - 1) * SCALE + 1;
  const H = (nLat - 1) * SCALE + 1;

  const canvas = document.createElement('canvas');
  canvas.width  = W;
  canvas.height = H;
  const ctx  = canvas.getContext('2d');
  const img  = ctx.createImageData(W, H);
  const data = img.data;

  const mercYNorth = latToMercY(lats[nLat - 1]);
  const mercYSouth = latToMercY(lats[0]);
  const latSpan    = lats[nLat - 1] - lats[0];

  for (let py = 0; py < H; py++) {
    const mercY  = mercYNorth - (py / (H - 1)) * (mercYNorth - mercYSouth);
    const geoLat = mercYToLat(mercY);
    const fy     = (geoLat - lats[0]) / latSpan * (nLat - 1);
    const i0     = Math.min(Math.max(Math.floor(fy), 0), nLat - 2);
    const ty     = fy - i0;

    for (let px = 0; px < W; px++) {
      const fx = px / (W - 1) * (nLon - 1);
      const j0 = Math.min(Math.floor(fx), nLon - 2);
      const tx = fx - j0;

      const v =
        precipGrid[ i0      * nLon + j0    ] * (1 - tx) * (1 - ty) +
        precipGrid[ i0      * nLon + j0 + 1] *      tx  * (1 - ty) +
        precipGrid[(i0 + 1) * nLon + j0    ] * (1 - tx) *      ty  +
        precipGrid[(i0 + 1) * nLon + j0 + 1] *      tx  *      ty;

      const [r, g, b] = precipToRgb(v);
      const idx = (py * W + px) * 4;
      data[idx]     = r;
      data[idx + 1] = g;
      data[idx + 2] = b;
      data[idx + 3] = Math.min(210, (v / 5) ** 1.5 * 210);
    }
  }
  ctx.putImageData(img, 0, 0);

  precipBounds = [lons[0], lats[0], lons[nLon - 1], lats[nLat - 1]];
  return canvas.toDataURL('image/png');
}

async function loadPrecipFrame(idx) {
  if (!PRECIP_FRAMES.length) return;
  const frame = PRECIP_FRAMES[idx];
  if (!frame) return;
  const url = `./data/precip/${frame.file}`;
  if (!precipCache[url]) {
    const resp = await fetch(url);
    precipCache[url] = await resp.json();
  }
  const dataUrl = buildPrecipBitmapUrl(precipCache[url].features);  // sets precipBounds
  if (dataUrl && precipBounds && map.getSource('precip-image')) {
    map.getSource('precip-image').updateImage({ url: dataUrl, coordinates: _imageCoords(precipBounds) });
    if (map.getLayer('precip-raster')) {
      map.setPaintProperty('precip-raster', 'raster-opacity',
        precipVisible ? Number($precipOpacity.value) / 100 * 0.80 : 0);
    }
  }
}

async function loadPrecipManifest() {
  try {
    const resp = await fetch('./data/precip/manifest.json');
    if (!resp.ok) return;
    const mf = await resp.json();
    PRECIP_FRAMES = mf.frames;
    addPrecipLegend();
    await loadPrecipFrame(0);
  } catch (e) {
    console.warn('Precip: no manifest — run precip_agent.py first', e);
    $precipToggle.style.display       = 'none';
    document.getElementById('precip-opacity-label').style.display = 'none';
  }
}

function addPrecipLegend() {
  const section = document.createElement('div');
  section.id = 'precip-legend';
  section.innerHTML =
    '<h2 style="margin-top:14px">Precipitation (mm/hr)</h2>' +
    PRECIP_COLOR_STOPS.map(
      (s) => `<div class="legend-item" style="cursor:default">
        <span class="wind-speed-swatch" style="background:rgb(${s.rgb})"></span>
        <span>${s.label}</span>
      </div>`,
    ).join('');
  $legend.appendChild(section);
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

// ── Outage loading & visualization ────────────────────────────────────────────
function outageLineCount(customers, totalLines) {
  const maxCust = OUTAGE_DATA?.max_customers ?? 351202;
  const fraction = Math.log10(customers + 1) / Math.log10(maxCust + 1);
  return Math.max(1, Math.ceil(fraction * totalLines));
}

function applyOutageFrame(idx) {
  if (!OUTAGE_DATA) return;
  const frame = OUTAGE_FRAMES[idx];
  if (!frame) return;

  $timestamp.textContent = frame.label;
  $slider.value = String(idx);

  const newActiveIds = new Set();
  for (const { fips, customers } of frame.counties) {
    const ids = fipsToLines.get(fips) ?? [];
    const n = outageLineCount(customers, ids.length);
    for (let i = 0; i < n && i < ids.length; i++) {
      newActiveIds.add(String(ids[i]));
    }
  }
  activeOutageIds = newActiveIds;

  if (deckOverlay) deckOverlay.setProps({ layers: buildAllLayers() });

  const totalCustomers = frame.counties.reduce((s, c) => s + c.customers, 0);
  if (totalCustomers > 0) {
    $stats.textContent =
      `${routes.length.toLocaleString()} lines · ` +
      `${activeOutageIds.size.toLocaleString()} down · ` +
      `${totalCustomers.toLocaleString()} customers affected`;
  } else {
    $stats.textContent = `${routes.length.toLocaleString()} transmission lines · No active outages`;
  }
}

async function loadOutageData() {
  try {
    const resp = await fetch('./data/outages/storm_oct2021.json');
    if (!resp.ok) return;
    OUTAGE_DATA   = await resp.json();
    OUTAGE_FRAMES = OUTAGE_DATA.frames;

    for (const [fipsStr, ids] of Object.entries(OUTAGE_DATA.county_lines)) {
      fipsToLines.set(Number(fipsStr), ids);
    }

    $stormName.textContent = OUTAGE_DATA.storm;
    $slider.max   = String(OUTAGE_FRAMES.length - 1);
    $slider.value = '0';

    addOutageLegend();
    applyOutageFrame(0);
  } catch (e) {
    console.warn('Outage data not found:', e);
  }
}

function addOutageLegend() {
  const section = document.createElement('div');
  section.id = 'outage-legend';
  section.innerHTML =
    '<h2 style="margin-top:14px">Power Outages</h2>' +
    `<div class="legend-item" style="cursor:default">
      <span class="legend-swatch" style="background:#ef4444"></span>
      <span>Line down (scaled to customers out)</span>
    </div>`;
  $legend.appendChild(section);
}

// ── Voltage legend ────────────────────────────────────────────────────────────
$legend.innerHTML =
  '<h2>Voltage</h2>' +
  Object.entries(LEVELS)
    .map(
      ([lvl, { label, color }]) =>
        `<div class="legend-item" data-level="${lvl}">
          <span class="legend-swatch" style="background:${color};border:1px solid rgba(0,0,0,0.08)"></span>
          <span>${label}</span>
        </div>`,
    )
    .join('') +
  '<div id="elevation-toggle" class="legend-item elevation-row" style="cursor:pointer;margin-top:8px;padding:4px 4px">' +
  '<span class="legend-swatch elevation-swatch" id="elevation-indicator"></span>' +
  '<span>3D Elevation</span></div>';

$legend.querySelectorAll('.legend-item').forEach((el) => {
  el.addEventListener('click', () => {
    const lvl = Number(el.dataset.level);
    visible[lvl] = !visible[lvl];
    el.classList.toggle('hidden', !visible[lvl]);
    applyFilter();
  });
});

document.getElementById('elevation-toggle').addEventListener('click', () => {
  elevated3D = !elevated3D;
  document.getElementById('elevation-toggle').classList.toggle('active', elevated3D);
  document.getElementById('elevation-indicator').style.background = elevated3D ? '#6366f1' : '';
  if (elevated3D) {
    map.easeTo({ pitch: 50, bearing: -15, duration: 900 });
  } else {
    map.easeTo({ pitch: 0, bearing: 0, duration: 900 });
  }
  if (deckOverlay) deckOverlay.setProps({ layers: buildAllLayers() });
});

function visibleFilter() {
  const shown = Object.entries(visible).filter(([, v]) => v).map(([k]) => Number(k));
  return ['in', ['get', 'level'], ['literal', shown]];
}

function applyFilter() {
  const shown = new Set(Object.entries(visible).filter(([, v]) => v).map(([k]) => Number(k)));
  filteredRouteFeatures = routeFeatures.filter((f) => shown.has(f.level));
  if (deckOverlay) deckOverlay.setProps({ layers: buildAllLayers() });
  if (map.getLayer('routes-hit')) map.setFilter('routes-hit', visibleFilter());
}

// ── Map load ──────────────────────────────────────────────────────────────────
map.on('load', () => {
  deckOverlay = new MapboxOverlay({ interleaved: false, layers: buildAllLayers() });
  map.addControl(deckOverlay);

  loadWindManifest();
  loadPrecipManifest();

  function refreshLayers() {
    const windOp   = Number($windOpacity.value) / 100;
    const precipOp = Number($precipOpacity.value) / 100;
    if (map.getLayer('wind-raster'))
      map.setPaintProperty('wind-raster',   'raster-opacity', windVisible   ? windOp * 0.75   : 0);
    if (map.getLayer('precip-raster'))
      map.setPaintProperty('precip-raster', 'raster-opacity', precipVisible ? precipOp * 0.80 : 0);
    if (deckOverlay)
      deckOverlay.setProps({ layers: buildAllLayers() });
  }

  $windToggle.addEventListener('click', () => {
    windVisible = !windVisible;
    $windToggle.classList.toggle('off', !windVisible);
    refreshLayers();
    const s = document.getElementById('wind-legend');
    if (s) s.style.opacity = windVisible ? '1' : '0.4';
  });

  $windOpacity.addEventListener('input', refreshLayers);

  $precipToggle.addEventListener('click', () => {
    precipVisible = !precipVisible;
    $precipToggle.classList.toggle('off', !precipVisible);
    refreshLayers();
    const s = document.getElementById('precip-legend');
    if (s) s.style.opacity = precipVisible ? '1' : '0.4';
  });

  $precipOpacity.addEventListener('input', refreshLayers);

  // ── Weather image sources (Mapbox-native, below deck.gl canvas) ───────────
  const TRANSPARENT_PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVQI12NgAAIABQAABjE+ibYAAAAASUVORK5CYII=';
  const INIT_COORDS     = [[-80, 46], [-68, 46], [-68, 36], [-80, 36]];

  const firstSymbolId = map.getStyle().layers.find((l) => l.type === 'symbol')?.id;

  map.addSource('wind-image',   { type: 'image', url: TRANSPARENT_PNG, coordinates: INIT_COORDS });
  map.addSource('precip-image', { type: 'image', url: TRANSPARENT_PNG, coordinates: INIT_COORDS });
  map.addLayer({ id: 'wind-raster',   type: 'raster', source: 'wind-image',   paint: { 'raster-opacity': 0 } }, firstSymbolId);
  map.addLayer({ id: 'precip-raster', type: 'raster', source: 'precip-image', paint: { 'raster-opacity': 0 } }, firstSymbolId);

  // Routes source — invisible hit target only; lines rendered by deck.gl PathLayer
  map.addSource('routes', {
    type: 'geojson', tolerance: 1, buffer: 0, promoteId: 'id',
    data: { type: 'FeatureCollection', features: [] },
  });
  map.addLayer({
    id: 'routes-hit', type: 'line', source: 'routes',
    layout: { 'line-cap': 'round' },
    paint: { 'line-width': 16, 'line-opacity': 0 },
  });

  map.getSource('routes').setData({
    type: 'FeatureCollection',
    features: routeFeatures.map((rf) => ({
      type: 'Feature',
      id: rf.id,
      properties: { id: rf.id, level: rf.level, name: rf.name, voltage: rf.voltage, operator: rf.operator, number: rf.number },
      geometry: { type: 'LineString', coordinates: rf.path.map(([lon, lat]) => [lon, lat]) },
    })),
  });

  $stats.textContent = `${routes.length.toLocaleString()} transmission lines`;

  loadOutageData();

  // ── Route hover tooltip ───────────────────────────────────────────────────
  map.on('mousemove', 'routes-hit', (e) => {
    if (!e.features.length) return;
    const feat = e.features[0];
    map.getCanvas().style.cursor = 'pointer';
    const { name, voltage, operator } = feat.properties;
    const kv = Math.round(parseInt(voltage.split(';')[0], 10) / 1000);
    const isDown = activeOutageIds.has(String(feat.id));
    $tooltip.innerHTML =
      `<strong>${name}</strong>` +
      `<span>${kv} kV · ${operator}</span>` +
      (isDown ? '<span style="color:#ef4444;font-weight:600">⚡ Outage reported</span>' : '');
    $tooltip.classList.add('show');
    $tooltip.style.transform =
      `translate(${Math.min(e.point.x + 14, window.innerWidth - 340)}px, ${e.point.y + 14}px)`;
  });

  map.on('mouseleave', 'routes-hit', () => {
    map.getCanvas().style.cursor = '';
    $tooltip.classList.remove('show');
  });

  // ── Timeline controls ─────────────────────────────────────────────────────
  let playing = false;
  let timer   = null;

  function pause() {
    playing = false;
    clearInterval(timer);
    timer = null;
    $playBtn.innerHTML = '&#9654;';
  }

  function advanceFrame(i) {
    outageFrameIdx = Math.max(0, Math.min(i, OUTAGE_FRAMES.length - 1));
    applyOutageFrame(outageFrameIdx);
    const t = OUTAGE_FRAMES.length > 1 ? outageFrameIdx / (OUTAGE_FRAMES.length - 1) : 0;
    if (WIND_FRAMES.length) {
      windFrameIdx = Math.round(t * (WIND_FRAMES.length - 1));
      loadWindFrame(windFrameIdx);
    }
    if (PRECIP_FRAMES.length) {
      precipFrameIdx = Math.round(t * (PRECIP_FRAMES.length - 1));
      loadPrecipFrame(precipFrameIdx);
    }
  }

  function play() {
    playing = true;
    $playBtn.innerHTML = '&#9646;&#9646;';
    if (outageFrameIdx >= OUTAGE_FRAMES.length - 1) advanceFrame(0);
    timer = setInterval(() => {
      if (outageFrameIdx >= OUTAGE_FRAMES.length - 1) { pause(); return; }
      advanceFrame(outageFrameIdx + 1);
    }, 200);
  }

  $playBtn.addEventListener('click',  () => (playing ? pause() : play()));
  $slider.addEventListener('input',   () => {
    pause();
    advanceFrame(Number($slider.value));
  });
});
