/**
 * Small map. Uses an offline vector world outline by default; online OSM tiles
 * are opt-in (Settings) because fetching tiles tells a third party which places
 * you are looking at.
 */
import { useEffect, useRef, useState } from "react";
import L from "leaflet";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";

export interface MapPoint {
  lat: number;
  lon: number;
  id?: number;
  count?: number;
  label?: string;
}

let worldPromise: Promise<any> | null = null;
function loadWorld() {
  if (!worldPromise) {
    worldPromise = fetch("/world-110m.json")
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null);
  }
  return worldPromise;
}

export function MiniMap({ points, height = 220, interactive = true, onSelect, zoomOverride, tracks, onPick, picked }: {
  points: MapPoint[];
  height?: number;
  interactive?: boolean;
  onSelect?: (p: MapPoint) => void;
  zoomOverride?: number;
  /** GPS tracks drawn as lines, each a list of [lat, lon] */
  tracks?: [number, number][][];
  /** clicking the map picks a position (the place-correction dialog) */
  onPick?: (lat: number, lon: number) => void;
  picked?: { lat: number; lon: number } | null;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const mapRef = useRef<L.Map | null>(null);
  const layerRef = useRef<L.LayerGroup | null>(null);
  const extraRef = useRef<L.LayerGroup | null>(null);
  const pickRef = useRef(onPick);
  pickRef.current = onPick;
  const { data: settings } = useQuery({ queryKey: ["settings"], queryFn: api.settings, staleTime: 300_000 });
  const online = settings?.settings?.allow_online_map_tiles ?? false;
  const [worldReady, setWorldReady] = useState(false);

  useEffect(() => {
    if (!ref.current || mapRef.current) return;
    const map = L.map(ref.current, {
      zoomControl: interactive,
      attributionControl: online,
      dragging: interactive,
      scrollWheelZoom: interactive,
      doubleClickZoom: interactive,
      boxZoom: interactive,
      keyboard: interactive,
      preferCanvas: true,
    });
    mapRef.current = map;
    layerRef.current = L.layerGroup().addTo(map);
    extraRef.current = L.layerGroup().addTo(map);
    map.setView([20, 0], 1);
    map.on("click", (e: L.LeafletMouseEvent) => {
      pickRef.current?.(Number(e.latlng.lat.toFixed(6)), Number(L.Util.wrapNum(e.latlng.lng, [-180, 180], true).toFixed(6)));
    });

    if (online) {
      L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: "© OpenStreetMap",
      }).addTo(map);
    } else {
      loadWorld().then((geo) => {
        if (!geo || !mapRef.current) return;
        // Neutral grey works on both themes; the accent dots stay the only colour.
        L.geoJSON(geo, {
          style: {
            color: "rgba(128,130,140,0.55)",
            weight: 0.7,
            fillColor: "rgba(128,130,140,0.14)",
            fillOpacity: 1,
          },
          interactive: false,
        }).addTo(mapRef.current);
        setWorldReady(true);
      });
    }
    return () => {
      map.remove();
      mapRef.current = null;
    };
  }, [online, interactive]);

  useEffect(() => {
    const map = mapRef.current;
    const layer = layerRef.current;
    if (!map || !layer) return;
    layer.clearLayers();
    const bounds = L.latLngBounds([]);
    for (const line of tracks ?? []) {
      if (line.length < 2) continue;
      L.polyline(line, { color: "#7fb6f0", weight: 2.5, opacity: 0.85, interactive: false }).addTo(layer);
      line.forEach((pt) => bounds.extend(pt));
    }
    if (!points.length && !bounds.isValid()) return;
    for (const p of points) {
      const radius = p.count ? Math.min(26, 5 + Math.log2(p.count + 1) * 2.6) : 5;
      const marker = L.circleMarker([p.lat, p.lon], {
        radius,
        color: "#e2b062",
        weight: 1.5,
        fillColor: "#e2b062",
        fillOpacity: p.count ? 0.34 : 0.75,
      });
      if (p.label) marker.bindTooltip(`${p.label}${p.count ? ` · ${p.count.toLocaleString()} photos` : ""}`);
      if (onSelect) marker.on("click", () => onSelect(p));
      marker.addTo(layer);
      bounds.extend([p.lat, p.lon]);
    }
    if (bounds.isValid()) {
      // The offline outline is coarse, so it is not zoomed past country level — except to
      // show a GPS track, whose shape is the point even with no streets underneath.
      const hasTracks = (tracks ?? []).some((t) => t.length > 1);
      const maxZoom = online ? (zoomOverride ?? 14) : hasTracks ? 13 : Math.min(zoomOverride ?? 6, 6);
      map.fitBounds(bounds, { padding: [28, 28], maxZoom });
      if (!online && points.length === 1 && !hasTracks) {
        // A lone marker on a world outline needs context, not a blank grey square.
        map.setView([points[0].lat, points[0].lon], 5);
      }
    }
  }, [points, worldReady, onSelect, online, zoomOverride, tracks]);

  useEffect(() => {
    const layer = extraRef.current;
    if (!layer) return;
    layer.clearLayers();
    if (picked) {
      L.circleMarker([picked.lat, picked.lon], { radius: 8, color: "#fff", weight: 2, fillColor: "#e2b062",
        fillOpacity: 1 }).addTo(layer);
    }
  }, [picked]);

  return (
    <div className={`minimap${onPick ? " is-picking" : ""}`} style={{ height }}>
      <div ref={ref} className="minimap-canvas" />
      {!online && <span className="minimap-note dim">offline map</span>}
    </div>
  );
}
