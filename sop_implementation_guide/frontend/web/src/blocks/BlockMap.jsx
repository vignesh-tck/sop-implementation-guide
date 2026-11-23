import { useEffect, useMemo, useState } from "react";
import { MapContainer, TileLayer, GeoJSON, Marker, Popup, useMap } from "react-leaflet";
import L from "leaflet";
import { score100Colour } from "../api.js";

// Bowie, MD — centers the map on the pilot geography (ADR-011) without
// waiting on the first fitBounds pass.
const BOWIE_CENTER = [38.9425, -76.7716];

// Keeps the fitted bounds clear of the floating list/detail panels, which sit
// on top of the map rather than squeezing it — plain fitBounds would happily
// center a block right underneath one of them.
const PANEL_CLEARANCE = { topLeft: [300, 40], bottomRight: [560, 40] };

// Esri's free tile endpoints occasionally drop a request outright (seen as
// ERR_CONNECTION_RESET), and Leaflet never retries on its own — a dropped tile
// just stays a blank square until some unrelated pan/zoom happens to reload it.
// Re-requesting with a cache-busting param, a few times with backoff, clears
// most of these without the user ever noticing.
const MAX_TILE_RETRIES = 4;
function retryTileError(e) {
  const tile = e.tile;
  if (!tile) return;
  const attempt = (tile._retryCount || 0) + 1;
  if (attempt > MAX_TILE_RETRIES) return;
  tile._retryCount = attempt;
  setTimeout(() => {
    const url = new URL(tile.src, window.location.href);
    url.searchParams.set("retry", attempt);
    tile.src = url.toString();
  }, 400 * attempt);
}

function FlyToSelection({ feature }) {
  const map = useMap();
  useEffect(() => {
    if (!feature) return;
    const bounds = L.geoJSON(feature).getBounds();
    if (!bounds.isValid()) return;
    map.flyToBounds(bounds, {
      paddingTopLeft: PANEL_CLEARANCE.topLeft,
      paddingBottomRight: PANEL_CLEARANCE.bottomRight,
      maxZoom: 17,
      duration: 0.6,
    });
  }, [feature, map]);
  return null;
}

// ── The assistant's answer on the map ─────────────────────────────────────────

function linePoints(geometry) {
  if (geometry?.type === "LineString") return geometry.coordinates;
  if (geometry?.type === "MultiLineString") return geometry.coordinates.flat();
  return [];
}

// n points spread evenly along a block's line, as [lat, lng].
function pointsAlong(geometry, n) {
  const pts = linePoints(geometry).map(([x, y]) => [y, x]);
  if (pts.length < 2) return pts.length ? Array(n).fill(pts[0]) : [];
  const seg = pts.slice(1).map((p, i) => Math.hypot(p[0] - pts[i][0], p[1] - pts[i][1]));
  const total = seg.reduce((a, b) => a + b, 0);
  return Array.from({ length: n }, (_, k) => {
    let left = (total * (k + 1)) / (n + 1);
    for (let i = 0; i < seg.length; i++) {
      if (left <= seg[i] || i === seg.length - 1) {
        const t = seg[i] ? Math.min(1, left / seg[i]) : 0;
        return [pts[i][0] + (pts[i + 1][0] - pts[i][0]) * t, pts[i][1] + (pts[i + 1][1] - pts[i][1]) * t];
      }
      left -= seg[i];
    }
    return pts[pts.length - 1];
  });
}

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// Labels alternate sides of the street so neighbours don't overlap.
const pinIcon = (label, side) =>
  L.divIcon({ className: "ans-pin-wrap", iconSize: [0, 0], html: `<span class="ans-pin ${side}"><i></i>${esc(label)}</span>` });

function FitAnswer({ features }) {
  const map = useMap();
  useEffect(() => {
    if (!features.length) return;
    const bounds = L.geoJSON({ type: "FeatureCollection", features }).getBounds();
    if (bounds.isValid()) {
      map.flyToBounds(bounds, { paddingTopLeft: PANEL_CLEARANCE.topLeft, paddingBottomRight: PANEL_CLEARANCE.bottomRight, maxZoom: 16, duration: 0.6 });
    }
  }, [features, map]);
  return null;
}

export default function BlockMap({ blocks, selectedId, profiles, onSelect, answer, onClearAnswer, onAskStreet }) {
  // The street last clicked, for the "Ask about this street" popup.
  const [pointed, setPointed] = useState(null);
  const point = (feature, e) => {
    onSelect(feature.properties.id);
    if (onAskStreet) setPointed({ id: feature.properties.id, name: feature.properties.street_name, latlng: e.latlng, at: Date.now() });
  };
  // Keyed by id so each block's color recomputes only when its own score changes,
  // not on every render of the whole collection.
  const features = useMemo(
    () =>
      blocks
        .filter((b) => b.geometry_geojson)
        .map((b) => ({
          type: "Feature",
          id: b.id,
          properties: { id: b.id, street_name: b.street_name },
          geometry: b.geometry_geojson,
        })),
    [blocks]
  );

  const selectedFeature = useMemo(
    () => features.find((f) => f.id === selectedId) || null,
    [features, selectedId]
  );

  const highlighted = useMemo(
    () => features.filter((f) => answer?.block_ids?.includes(f.id)),
    [features, answer]
  );
  const answerFeature = answer?.recs?.length ? features.find((f) => f.id === answer.block_id) : null;
  const pins = answerFeature ? pointsAlong(answerFeature.geometry, answer.recs.length) : [];
  const showingAnswer = highlighted.length > 0 || pins.length > 0;

  const scoreFor = (id) => profiles[id]?.feasibility_score ?? blocks.find((b) => b.id === id)?.sop_index_norm;

  const style = (feature) => {
    const id = feature.properties.id;
    const selected = id === selectedId;
    return {
      color: score100Colour(scoreFor(id)),
      weight: selected ? 6 : 3,
      opacity: selected ? 1 : 0.75,
    };
  };

  return (
    <div className="map-pane">
      <MapContainer center={BOWIE_CENTER} zoom={13} zoomControl={false} style={{ width: "100%", height: "100%" }}>
        <FlyToSelection feature={selectedFeature} />
        {/* Hybrid basemap: Esri satellite imagery with a transparent roads/place-name
            layer on top, so the ground is visible but streets are still legible. Both
            are free, keyless tile services — swapping back to a plain street map is
            just restoring the single OSM TileLayer this replaced. */}
        <TileLayer
          attribution="Tiles &copy; Esri — Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community"
          url="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
          eventHandlers={{ tileerror: retryTileError }}
        />
        <TileLayer
          attribution="Labels &copy; Esri"
          url="https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}"
          eventHandlers={{ tileerror: retryTileError }}
        />
        {features.length > 0 && (
          <GeoJSON
            key={features.length + ":" + selectedId + ":" + Object.keys(profiles).length}
            data={{ type: "FeatureCollection", features }}
            style={style}
            onEachFeature={(feature, layer) => {
              layer.bindTooltip(feature.properties.street_name || `#${feature.properties.id}`);
              layer.on("click", (e) => point(feature, e));
            }}
          />
        )}
        {/* Blocks an across-blocks answer named: a white casing under a navy line, with the block number. */}
        {highlighted.length > 0 && (
          <>
            <FitAnswer features={highlighted} />
            <GeoJSON
              key={"ans-halo:" + answer.block_ids.join(",")}
              data={{ type: "FeatureCollection", features: highlighted }}
              style={{ color: "#ffffff", weight: 10, opacity: 0.9 }}
              interactive={false}
            />
            <GeoJSON
              key={"ans:" + answer.block_ids.join(",")}
              data={{ type: "FeatureCollection", features: highlighted }}
              style={{ color: "var(--navy)", weight: 5, opacity: 1 }}
              onEachFeature={(feature, layer) => {
                layer.bindTooltip(`#${feature.properties.id}`, { permanent: true, direction: "center", className: "ans-block-label" });
                layer.on("click", (e) => point(feature, e));
              }}
            />
          </>
        )}
        {/* A separate top layer for the selection: a white casing underneath a
            pulsing accent line, plus an always-open label — three independent
            cues so the selected block reads at a glance instead of blending
            into same-colored neighbours. */}
        {selectedFeature && (
          <>
            <GeoJSON
              key={"halo:" + selectedId}
              data={selectedFeature}
              style={{ color: "#ffffff", weight: 11, opacity: 0.9 }}
              interactive={false}
            />
            <GeoJSON
              key={"highlight:" + selectedId}
              data={selectedFeature}
              style={{ color: "var(--navy)", weight: 6, opacity: 1, className: "block-highlight-pulse" }}
              onEachFeature={(feature, layer) => {
                layer.bindTooltip(feature.properties.street_name || `#${feature.properties.id}`, {
                  permanent: true,
                  direction: "top",
                  className: "block-label",
                });
                layer.on("click", (e) => point(feature, e));
              }}
            />
          </>
        )}
        {/* The recommendations a block answer is about, labelled along its street. */}
        {pins.map((p, i) => (
          <Marker key={`pin:${answer.block_id}:${answer.recs[i].rec_id}`} position={p} icon={pinIcon(answer.recs[i].label, i % 2 ? "r" : "l")} interactive={false} />
        ))}
        {pointed && (
          <Popup key={pointed.at} position={pointed.latlng} className="street-pop" closeButton={false}>
            <div className="street-pop-name">{pointed.name || `Block ${pointed.id}`}</div>
            <button type="button" className="primary" onClick={() => { onAskStreet(pointed.id, null); setPointed(null); }}>
              ✧ Ask about this street
            </button>
            <button type="button" onClick={() => {
              onAskStreet(pointed.id, "Which recommendations touch the street itself, and which are about the buildings or land beside it?");
              setPointed(null);
            }}>
              Which recommendations touch it?
            </button>
          </Popup>
        )}
      </MapContainer>
      {showingAnswer && (
        <div className="map-answer-bar">
          <span className="spark" aria-hidden="true">✧</span> Showing the assistant's answer on the map
          <button type="button" className="link" onClick={onClearAnswer}>Clear</button>
        </div>
      )}
      <div className="map-legend">
        <span>
          <span className="swatch" style={{ background: "var(--teal)" }} />
          strong
        </span>
        <span>
          <span className="swatch" style={{ background: "var(--amber)" }} />
          marginal
        </span>
        <span>
          <span className="swatch" style={{ background: "var(--red)" }} />
          weak
        </span>
        <span style={{ marginLeft: ".3rem" }}>— SoP index, or feasibility score once analyzed</span>
      </div>
    </div>
  );
}
