import { useMemo } from "react";
import { MapContainer, TileLayer, GeoJSON } from "react-leaflet";
import { score100Colour } from "../api.js";

// Bowie, MD — centers the map on the pilot geography (ADR-011) without
// waiting on the first fitBounds pass.
const BOWIE_CENTER = [38.9425, -76.7716];

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
      <MapContainer center={BOWIE_CENTER} zoom={13} style={{ width: "100%", height: "100%" }}>
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
          url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
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
