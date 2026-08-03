// Lets the user drag a rectangle on the map to pick a US bbox, choose a
// date range, and submit — replaces the old fixed NE/MA preset-view buttons.
// A plain mousedown/mousemove/mouseup rectangle (no mapbox-gl-draw
// dependency) is all this needs: one shape, drawn once per submission.

const EMPTY_FC = { type: 'FeatureCollection', features: [] };

function rectFeature(bbox) {
  const [w, s, e, n] = bbox;
  return {
    type: 'FeatureCollection',
    features: [{
      type: 'Feature',
      properties: {},
      geometry: { type: 'Polygon', coordinates: [[[w, s], [e, s], [e, n], [w, n], [w, s]]] },
    }],
  };
}

export function initAreaPicker(map, { onSubmit, defaultBbox, defaultStart, defaultEnd }) {
  const $drawBtn = document.getElementById('draw-area-btn');
  const $start   = document.getElementById('date-start');
  const $end     = document.getElementById('date-end');
  const $goBtn   = document.getElementById('go-btn');
  const $status  = document.getElementById('area-status');

  $start.value = defaultStart;
  $end.value   = defaultEnd;

  let bbox = defaultBbox;
  let drawing = false;
  let dragging = false;
  let startLngLat = null;

  function setStatus(text) {
    $status.textContent = text;
  }

  map.on('load', () => {
    map.addSource('bbox-picker', { type: 'geojson', data: rectFeature(bbox) });
    map.addLayer({
      id: 'bbox-picker-fill', type: 'fill', source: 'bbox-picker',
      paint: { 'fill-color': '#4ade80', 'fill-opacity': 0.12 },
    });
    map.addLayer({
      id: 'bbox-picker-line', type: 'line', source: 'bbox-picker',
      paint: { 'line-color': '#4ade80', 'line-width': 2, 'line-dasharray': [2, 1] },
    });
  });

  function setBbox(b) {
    bbox = b;
    if (map.getSource('bbox-picker')) {
      map.getSource('bbox-picker').setData(b ? rectFeature(b) : EMPTY_FC);
    }
  }

  function enterDrawMode() {
    drawing = true;
    $drawBtn.classList.add('active');
    map.dragPan.disable();
    map.getCanvas().style.cursor = 'crosshair';
  }

  function exitDrawMode() {
    drawing = false;
    $drawBtn.classList.remove('active');
    map.dragPan.enable();
    map.getCanvas().style.cursor = '';
  }

  $drawBtn.addEventListener('click', () => (drawing ? exitDrawMode() : enterDrawMode()));

  map.getCanvas().addEventListener('mousedown', (e) => {
    if (!drawing) return;
    dragging = true;
    startLngLat = map.unproject([e.offsetX, e.offsetY]);
    e.preventDefault();
  });

  map.on('mousemove', (e) => {
    if (!drawing || !dragging) return;
    const cur = e.lngLat;
    setBbox([
      Math.min(startLngLat.lng, cur.lng), Math.min(startLngLat.lat, cur.lat),
      Math.max(startLngLat.lng, cur.lng), Math.max(startLngLat.lat, cur.lat),
    ]);
  });

  window.addEventListener('mouseup', () => {
    if (!drawing || !dragging) return;
    dragging = false;
    exitDrawMode();
  });

  $goBtn.addEventListener('click', () => {
    if (!bbox) {
      setStatus('Draw an area on the map first.');
      return;
    }
    if (!$start.value || !$end.value) {
      setStatus('Pick a start and end date.');
      return;
    }
    if ($end.value <= $start.value) {
      setStatus('End date must be after start date.');
      return;
    }
    onSubmit(bbox, $start.value, $end.value);
  });

  function setBusy(busy) {
    $goBtn.disabled = busy;
    $drawBtn.disabled = busy;
  }

  return { setStatus, setBusy, getBbox: () => bbox };
}
