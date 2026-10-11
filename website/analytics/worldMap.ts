type Ring = [number, number][];
type Polygon = Ring[];
type MultiPolygon = Polygon[];

interface Feature {
  geometry: {
    type: "Polygon" | "MultiPolygon";
    coordinates: Polygon | MultiPolygon;
  } | null;
}

export interface FeatureCollection {
  features: Feature[];
}

export const WORLD_BOUNDS = {
  west: -180,
  east: 180,
  south: -58,
  north: 84,
} as const;

function ringToPath(ring: Ring, width: number, height: number): string {
  if (ring.length < 2) return "";
  let path = "";
  for (let index = 0; index < ring.length; index++) {
    const [lng, lat] = ring[index]!;
    const x =
      ((lng - WORLD_BOUNDS.west) / (WORLD_BOUNDS.east - WORLD_BOUNDS.west)) *
      width;
    const y =
      ((WORLD_BOUNDS.north - lat) / (WORLD_BOUNDS.north - WORLD_BOUNDS.south)) *
      height;
    path += `${index === 0 ? "M" : "L"}${x.toFixed(2)} ${y.toFixed(2)}`;
  }
  return `${path}Z`;
}

export function landPathsForWorld(
  land: FeatureCollection,
  width: number,
  height: number,
): string[] {
  const paths: string[] = [];
  for (const feature of land.features) {
    const geometry = feature.geometry;
    if (!geometry) continue;
    const polygons: Polygon[] =
      geometry.type === "Polygon"
        ? [geometry.coordinates as Polygon]
        : (geometry.coordinates as MultiPolygon);
    for (const polygon of polygons) {
      const outer = polygon[0];
      if (!outer) continue;
      let path = ringToPath(outer, width, height);
      for (let index = 1; index < polygon.length; index++) {
        path += ringToPath(polygon[index]!, width, height);
      }
      if (path) paths.push(path);
    }
  }
  return paths;
}
