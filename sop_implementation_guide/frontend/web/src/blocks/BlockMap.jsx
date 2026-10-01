import { useEffect, useMemo } from "react";
import { MapContainer, TileLayer, GeoJSON, useMap } from "react-leaflet";
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

export default function BlockMap({ blocks, selectedId, profiles, onSelect }) {
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
              layer.on("click", () => onSelect(feature.properties.id));
            }}
          />
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
              style={{ color: "var(--orange)", weight: 6, opacity: 1, className: "block-highlight-pulse" }}
              onEachFeature={(feature, layer) => {
                layer.bindTooltip(feature.properties.street_name || `#${feature.properties.id}`, {
                  permanent: true,
                  direction: "top",
                  className: "block-label",
                });
                layer.on("click", () => onSelect(feature.properties.id));
              }}
            />
          </>
        )}
      </MapContainer>
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
