/** Typed client for the Memoria API. */

export interface PhotoIndex {
  ids: number[];
  ratio: number[];
  ts: number[];
  flags: number[];
  total: number;
}

export interface FaceBox {
  id: number;
  box: [number, number, number, number];
  person_id: number | null;
  label: string | null;
  confidence: number | null;
  assign_source: string | null;
  quality: number;
  det_score: number;
}

export interface PhotoDetail {
  id: number;
  filename: string;
  folder: string;
  path: string;
  root: string;
  ext: string;
  size: number;
  width: number | null;
  height: number | null;
  format: string | null;
  orientation: number | null;
  status: string;
  error: string | null;
  taken_ts: number | null;
  taken_local: string | null;
  date_source: string | null;
  date_confidence: string | null;
  camera: {
    make: string | null; model: string | null; lens: string | null; focal_length: number | null;
    aperture: number | null; exposure_time: number | null; iso: number | null; software: string | null;
  };
  gps: { lat: number; lon: number; alt: number | null } | null;
  place: {
    id: number; name: string; city: string | null; admin1: string | null; country: string | null;
    lat: number | null; lon: number | null; label: string; confidence: string | null; source: string | null;
  } | null;
  landmark: string | null;
  source_kind: string | null;
  quality: { score: number | null; blur: number | null; brightness: number | null; contrast: number | null;
    clipped: number | null; aesthetic: number | null };
  caption: string | null;
  faces: FaceBox[];
  tags: { name: string; category: string; score: number; confidence: number }[];
  duplicates: { group_id: number; kind: string; count: number; relation: string; keep_photo_id: number }[];
  trip: { id: number; title: string } | null;
  event: { id: number; title: string; kind: string } | null;
  favorite: boolean;
  hidden: boolean;
  sha256: string | null;
}

export interface Person {
  id: number;
  label: string;
  name: string | null;
  display_no: number | null;
  photo_count: number;
  face_count: number;
  cover_face_id: number | null;
  first_seen_ts: number | null;
  last_seen_ts: number | null;
  confidence: number | null;
  hidden: boolean;
  ignored: boolean;
  named: boolean;
}

export interface EventItem {
  id: number;
  kind: "event" | "trip";
  title: string;
  auto_title: string;
  user_title: string | null;
  category: string | null;
  start_ts: number;
  end_ts: number;
  date_label: string;
  photo_count: number;
  people_count: number;
  cover_photo_id: number | null;
  summary: string | null;
  place: { id: number; label: string; city: string | null; country: string | null; lat: number | null; lon: number | null } | null;
  location_confidence: string | null;
  parent_id: number | null;
  lat: number | null;
  lon: number | null;
  year: number;
}

export interface SearchResponse {
  query: string;
  interpretation: { kind: string; label: string; detail: string | null }[];
  explanation: string;
  result_type: string;
  total: number;
  took_ms: number;
  photos: { id: number; ratio: number; ts: number; score: number | null }[];
  events: any[];
  people: any[];
  places: any[];
}

export interface Stats {
  photos: number;
  photos_by_status: Record<string, number>;
  people: number;
  named_people: number;
  faces: number;
  events: number;
  trips: number;
  places: number;
  duplicate_groups: number;
  duplicate_photos: number;
  with_gps: number;
  favorites: number;
  errors: number;
  missing: number;
  pending: number;
  bytes: number;
  date_range: { from: number | null; to: number | null };
  top_places: any[];
  roots: { id: number; path: string; last_scan_at: number | null }[];
}

const BASE = "/api";

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: init?.body ? { "Content-Type": "application/json" } : undefined,
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || body.error || detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(detail, res.status);
  }
  return res.json() as Promise<T>;
}

const qs = (params: Record<string, any>) => {
  const s = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "" || v === false) continue;
    if (Array.isArray(v)) v.forEach((x) => s.append(k, String(x)));
    else s.append(k, String(v));
  }
  const str = s.toString();
  return str ? `?${str}` : "";
};

export const api = {
  stats: () => request<Stats>("/stats"),
  health: () => request<any>("/health"),
  memories: () => request<{ sections: any[] }>("/memories"),

  photos: (params: Record<string, any> = {}) => request<PhotoIndex>(`/photos/index${qs(params)}`),
  photo: (id: number) => request<PhotoDetail>(`/photos/${id}`),
  describe: (id: number, detailed = false) =>
    request<{ caption: string; cached: boolean }>(`/photos/${id}/describe?detailed=${detailed}`, { method: "POST" }),
  similar: (id: number, limit = 24) =>
    request<{ photos: { id: number; score: number }[] }>(`/photos/${id}/similar${qs({ limit })}`),
  setFlags: (id: number, body: { favorite?: boolean; hidden?: boolean }) =>
    request(`/photos/${id}/flags`, { method: "POST", body: JSON.stringify(body) }),

  timeline: (params: Record<string, any> = {}) => request<any>(`/timeline${qs(params)}`),
  folders: () => request<{ folders: { path: string; count: number }[] }>("/folders"),

  people: (params: Record<string, any> = {}) =>
    request<{ people: Person[]; unassigned_faces: number; me_person_id: number | null }>(`/people${qs(params)}`),
  person: (id: number) => request<any>(`/people/${id}`),
  personFaces: (id: number, params: Record<string, any> = {}) =>
    request<{ faces: any[] }>(`/people/${id}/faces${qs(params)}`),
  unassignedFaces: (params: Record<string, any> = {}) =>
    request<{ faces: any[] }>(`/faces/unassigned${qs(params)}`),
  renamePerson: (id: number, name: string | null) =>
    request(`/people/${id}/rename`, { method: "POST", body: JSON.stringify({ name }) }),
  personFlags: (id: number, body: { hidden?: boolean; ignored?: boolean; is_me?: boolean }) =>
    request(`/people/${id}/flags`, { method: "POST", body: JSON.stringify(body) }),
  mergePeople: (target_id: number, source_ids: number[]) =>
    request(`/people/merge`, { method: "POST", body: JSON.stringify({ target_id, source_ids }) }),
  splitPerson: (id: number, face_ids: number[], name?: string) =>
    request(`/people/${id}/split`, { method: "POST", body: JSON.stringify({ face_ids, name }) }),
  assignFaces: (face_ids: number[], person_id?: number, name?: string) =>
    request<{ person_id: number }>(`/faces/assign`, {
      method: "POST",
      body: JSON.stringify({ face_ids, person_id, name }),
    }),
  rejectFaces: (face_ids: number[], person_id: number) =>
    request(`/faces/reject`, { method: "POST", body: JSON.stringify({ face_ids, person_id }) }),
  mergeSuggestions: () => request<{ suggestions: any[] }>("/people/suggestions/merges"),
  notSame: (a: number, b: number) =>
    request(`/people/not-same`, { method: "POST", body: JSON.stringify({ a, b }) }),

  events: (params: Record<string, any> = {}) => request<{ events: EventItem[] }>(`/events${qs(params)}`),
  event: (id: number) => request<any>(`/events/${id}`),
  renameEvent: (id: number, title: string | null) =>
    request(`/events/${id}`, { method: "POST", body: JSON.stringify({ title }) }),

  places: () => request<{ places: any[]; hierarchy: any[] }>("/places"),
  place: (id: number) => request<any>(`/places/${id}`),
  mapPoints: (params: Record<string, any> = {}) =>
    request<{ points: { id: number; lat: number; lon: number; place_id: number | null; ts: number }[] }>(
      `/map/points${qs(params)}`,
    ),

  duplicates: (params: Record<string, any> = {}) => request<any>(`/duplicates${qs(params)}`),
  reviewDuplicate: (id: number, body: { status?: string; keep_photo_id?: number }) =>
    request(`/duplicates/${id}/review`, { method: "POST", body: JSON.stringify(body) }),
  hideCopies: (id: number, photo_ids: number[]) =>
    request<{ hidden: number }>(`/duplicates/${id}/hide-copies`, {
      method: "POST",
      body: JSON.stringify({ photo_ids }),
    }),

  search: (q: string, params: Record<string, any> = {}) => request<SearchResponse>(`/search${qs({ q, ...params })}`),
  suggestions: (q: string) => request<{ suggestions: any[] }>(`/search/suggestions${qs({ q })}`),

  settings: () => request<any>("/settings"),
  updateSettings: (body: Record<string, any>) =>
    request(`/settings`, { method: "POST", body: JSON.stringify(body) }),
  addRoot: (path: string) => request<any>(`/roots`, { method: "POST", body: JSON.stringify({ path }) }),
  removeRoot: (id: number) => request(`/roots/${id}`, { method: "DELETE" }),
  browse: (path?: string) => request<any>(`/browse${qs({ path })}`),
  jobs: () => request<any>("/jobs"),
  startJob: (body: Record<string, any>) => request<any>(`/jobs`, { method: "POST", body: JSON.stringify(body) }),
  cancelJob: (id: number) => request(`/jobs/${id}/cancel`, { method: "POST" }),
  models: () => request<any>("/models"),
  errors: () => request<any>("/errors"),
  audit: (params: Record<string, any> = {}) => request<any>(`/audit${qs(params)}`),
  clearCache: (kind: string) => request<any>(`/cache/clear${qs({ kind })}`, { method: "POST" }),
};

export const thumbUrl = (id: number, size: "sm" | "m" | "l" = "m") => `${BASE}/thumb/${id}?s=${size}`;
export const originalUrl = (id: number) => `${BASE}/photos/${id}/original`;
export const downloadUrl = (id: number) => `${BASE}/photos/${id}/download`;
export const faceUrl = (id: number, size = 200) => `${BASE}/faces/${id}/crop?size=${size}`;
