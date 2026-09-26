/** Typed client for the Memoria API. */

export interface PhotoIndex {
  ids: number[];
  ratio: number[];
  ts: number[];
  flags: number[];
  /** seconds, 0 for stills */
  dur?: number[];
  /** the user's stars, 0-5 */
  rating?: number[];
  /** on a stack cover: how many files the stack holds (0 otherwise) */
  stack?: number[];
  /** the user's rotation, clockwise degrees */
  rot?: number[];
  total: number;
}

/** The grid's items from the compact PhotoIndex payload (one place, so no page drops a field). */
export function gridItems(p: PhotoIndex | undefined | null) {
  if (!p) return [];
  return p.ids.map((id, i) => ({
    id, ratio: p.ratio[i], ts: p.ts[i], flags: p.flags?.[i] ?? 0, dur: p.dur?.[i] ?? 0,
    rating: p.rating?.[i] ?? 0, stack: p.stack?.[i] ?? 0, rot: p.rot?.[i] ?? 0,
  }));
}

/** Bit flags in PhotoIndex.flags. */
export const FLAG = { favorite: 1, faces: 2, video: 4, live: 8, stack: 16 } as const;

export interface Album {
  id: number;
  name: string;
  description: string | null;
  source: "user" | "takeout" | "icloud";
  kind: "manual" | "smart";
  /** a smart album's saved search */
  query: string | null;
  photo_count: number;
  cover_photo_id: number | null;
  start_ts: number | null;
  end_ts: number | null;
}

export interface NameSuggestion {
  person_id: number;
  name: string;
  label: string;
  cover_face_id: number | null;
  matched_photos: number;
  labelled_photos: number;
  photos_with_name: number;
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
  /** the person's age when the photo was taken, if their birthday is known */
  age: number | null;
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
  tags: { name: string; category: string; score: number; confidence: number; by_user: boolean }[];
  duplicates: { group_id: number; kind: string; count: number; relation: string; keep_photo_id: number }[];
  trip: { id: number; title: string } | null;
  event: { id: number; title: string; kind: string } | null;
  favorite: boolean;
  hidden: boolean;
  sha256: string | null;
  media_type: "image" | "video";
  duration: number | null;
  video_codec: string | null;
  /** a Live photo (paired video) or a motion photo (embedded video) */
  live: boolean;
  description: string | null;
  ocr_text: string | null;
  albums: { id: number; name: string; source: string }[];
  rating: number;
  stack: { id: number; members: number[] } | null;
  corrected: { date: boolean; location: boolean };
  rotation: number;
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
  photos: { id: number; ratio: number; ts: number; score: number | null; rot?: number }[];
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
  albums: number;
  videos: number;
  places: number;
  duplicate_groups: number;
  duplicate_photos: number;
  with_gps: number;
  favorites: number;
  errors: number;
  missing: number;
  /** files in the Trash */
  trash: number;
  pending: number;
  bytes: number;
  date_range: { from: number | null; to: number | null };
  top_places: any[];
  roots: { id: number; path: string; last_scan_at: number | null }[];
}

export interface Collection {
  key: string;
  title: string;
  group: "media" | "cleanup";
  count: number;
  cover_photo_id: number | null;
}

export interface FolderNode {
  root_id: number;
  name: string;
  path: string;
  count: number;
  cover_photo_id: number | null;
}

export interface Insights {
  year: number | null;
  years: number[];
  totals: { photos: number; videos: number; video_minutes: number; people: number; places: number;
    countries: number; trips: number; events: number };
  months: number[];
  busiest_day: { date: string; photos: number } | null;
  people: { id: number; label: string; cover_face_id: number | null; photos: number }[];
  constellation: { a: number; b: number; photos: number }[];
  new_people: { id: number; label: string; cover_face_id: number | null }[];
  places: { id: number; city: string | null; country: string | null; photos: number }[];
  countries: string[];
  furthest_from_home: { place_id: number; city: string | null; country: string | null; km: number; home: string } | null;
  cameras: { camera: string; photos: number }[];
  tags: { name: string; photos: number }[];
  trips: { id: number; title: string; start_ts: number; end_ts: number; photos: number; cover_photo_id: number | null }[];
  best_photo_ids: number[];
}

export interface GpxTrack {
  id: number;
  name: string;
  start_ts: number;
  end_ts: number;
  points: [number, number][];
}

export type Role = "owner" | "family" | "guest";

export interface Account {
  id: number;
  username: string;
  role: Role;
  disabled: number | boolean;
  created_at: number;
  last_login_at: number | null;
}

export interface AuthStatus {
  protected: boolean;
  accounts: boolean;
  logged_in: boolean;
  user: { id: number | null; username: string | null; role: Role } | null;
}

export interface ShareLink {
  token: string;
  allow_download: number | boolean;
  allow_upload?: number | boolean;
  expires_at: number | null;
  created_at: number;
  last_used_at: number | null;
}

/** What to export. Exactly the photos, or everything matching album/event/people/period. */
export interface ExportSpec {
  photo_ids?: number[];
  album_id?: number;
  event_id?: number;
  person_ids?: number[];
  person_mode?: "each" | "together" | "any";
  year?: number;
  month?: number;
  layout?: "date" | "flat" | "original";
  include_live?: boolean;
  include_stack_frames?: boolean;
  xmp?: boolean;
  folder?: string;
}

export interface TrashItem {
  photo_id: number;
  filename: string;
  size: number;
  trashed_at: number;
  expires_at: number;
  original_path: string;
  ratio: number;
  ts: number;
  video: boolean;
  duration: number | null;
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
    // A password was set (or the session ran out) while the app was open.
    if (res.status === 401 && !path.startsWith("/auth/") && !path.startsWith("/share/")) {
      window.dispatchEvent(new Event("memoria:login-required"));
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
  setDescription: (id: number, description: string | null) =>
    request(`/photos/${id}/description`, { method: "POST", body: JSON.stringify({ description }) }),
  addTag: (photo_ids: number[], name: string) =>
    request(`/photos/tags`, { method: "POST", body: JSON.stringify({ photo_ids, name }) }),
  removeTag: (photo_ids: number[], name: string) =>
    request(`/photos/tags/remove`, { method: "POST", body: JSON.stringify({ photo_ids, name }) }),
  rotate: (photo_ids: number[], degrees: number) =>
    request<{ rotated: number }>(`/photos/rotate`, { method: "POST", body: JSON.stringify({ photo_ids, degrees }) }),
  rate: (photo_ids: number[], rating: number) =>
    request(`/photos/rate`, { method: "POST", body: JSON.stringify({ photo_ids, rating }) }),
  correctDate: (photo_ids: number[], body: { taken_local?: string; shift_seconds?: number }) =>
    request<{ corrected: number }>(`/photos/correct-date`, { method: "POST", body: JSON.stringify({ photo_ids, ...body }) }),
  correctLocation: (photo_ids: number[], body: { lat?: number; lon?: number; place_id?: number }) =>
    request<{ corrected: number }>(`/photos/correct-location`, { method: "POST", body: JSON.stringify({ photo_ids, ...body }) }),
  clearCorrections: (photo_ids: number[]) =>
    request(`/photos/corrections/clear`, { method: "POST", body: JSON.stringify({ photo_ids }) }),
  stack: (id: number) => request<{ id: number; members: number[] }>(`/stacks/${id}`),
  stackCover: (stackId: number, photo_id: number) =>
    request(`/stacks/${stackId}/cover`, { method: "POST", body: JSON.stringify({ photo_id }) }),
  unstack: (stackId: number) => request(`/stacks/${stackId}/unstack`, { method: "POST" }),
  exportXmp: (folder: string, include_auto_tags = false) =>
    request<{ written: number; folder: string }>(`/export/xmp`, {
      method: "POST", body: JSON.stringify({ folder, include_auto_tags }),
    }),
  tags: () => request<{ tags: { name: string; category: string; count: number; user_count: number }[] }>("/tags"),

  albums: () => request<{ albums: Album[] }>("/albums"),
  album: (id: number) => request<Album & { photos: PhotoIndex }>(`/albums/${id}`),
  createAlbum: (name: string, photo_ids: number[] = []) =>
    request<{ id: number }>(`/albums`, { method: "POST", body: JSON.stringify({ name, photo_ids }) }),
  createSmartAlbum: (name: string, query: string) =>
    request<{ id: number }>(`/albums`, { method: "POST", body: JSON.stringify({ name, query }) }),
  updateAlbum: (id: number, body: { name?: string; description?: string; cover_photo_id?: number; query?: string }) =>
    request(`/albums/${id}`, { method: "POST", body: JSON.stringify(body) }),
  deleteAlbum: (id: number) => request(`/albums/${id}`, { method: "DELETE" }),
  addToAlbum: (id: number, photo_ids: number[]) =>
    request<{ added: number }>(`/albums/${id}/photos`, { method: "POST", body: JSON.stringify({ photo_ids }) }),
  removeFromAlbum: (id: number, photo_ids: number[]) =>
    request(`/albums/${id}/photos/remove`, { method: "POST", body: JSON.stringify({ photo_ids }) }),

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
  personFlags: (id: number, body: { hidden?: boolean; ignored?: boolean; is_me?: boolean; birth_date?: string }) =>
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
  nameSuggestions: () => request<{ suggestions: NameSuggestion[] }>("/people/suggestions/names"),
  dismissName: (person_id: number, name: string) =>
    request(`/people/suggestions/names/dismiss`, { method: "POST", body: JSON.stringify({ person_id, name }) }),
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
  hide: (photo_ids: number[], hidden = true) =>
    request<{ changed: number }>(`/photos/hide`, { method: "POST", body: JSON.stringify({ photo_ids, hidden }) }),
  collections: () => request<{ collections: Collection[]; recently_added_cover: number | null; duplicate_groups: number }>(
    "/collections"),
  browseFolders: (root_id?: number, path = "") =>
    request<{ root_id: number | null; path: string; folders: FolderNode[]; direct_count: number }>(
      `/folders/browse${qs({ root_id, path })}`),
  /** photos directly in one folder ("" is the root itself, which qs() would drop) */
  photosInFolder: (root_id: number, folder: string) =>
    request<PhotoIndex>(`/photos/index?${new URLSearchParams({ root_id: String(root_id), folder, folder_exact: "true",
      order: "date_asc" })}`),
  insights: (year?: number) => request<Insights>(`/insights${qs({ year })}`),
  random: (params: Record<string, any> = {}) => request<{ ids: number[] }>(`/random${qs(params)}`),
  gpxTracks: (params: { start_ts?: number; end_ts?: number } = {}) =>
    request<{ tracks: GpxTrack[] }>(`/gpx/tracks${qs(params)}`),
  uploadGpx: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<{ id: number }>(`/gpx`, { method: "POST", body: form, headers: {} });
  },
  exportAlbumHtml: (id: number, folder: string) =>
    request<{ exported: number; skipped: number; folder: string }>(`/albums/${id}/export-html`, {
      method: "POST", body: JSON.stringify({ folder }),
    }),

  authStatus: () => request<AuthStatus>("/auth/status"),
  login: (password: string, username?: string) =>
    request(`/auth/login`, { method: "POST", body: JSON.stringify({ password, username }) }),
  accounts: () => request<{ enabled: boolean; accounts: Account[] }>("/accounts"),
  enableAccounts: (username: string) =>
    request<Account>(`/accounts/enable`, { method: "POST", body: JSON.stringify({ username }) }),
  createAccount: (username: string, password: string, role: Role) =>
    request<Account>(`/accounts`, { method: "POST", body: JSON.stringify({ username, password, role }) }),
  updateAccount: (id: number, body: { role?: Role; password?: string; disabled?: boolean }) =>
    request<Account>(`/accounts/${id}`, { method: "POST", body: JSON.stringify(body) }),
  removeAccount: (id: number) => request(`/accounts/${id}`, { method: "DELETE" }),
  changeMyPassword: (current: string, next: string) =>
    request(`/accounts/me/password`, { method: "POST", body: JSON.stringify({ current, new: next }) }),

  lockedStatus: () => request<{ pin_set: boolean; open: boolean; count: number }>("/locked/status"),
  setLockedPin: (next: string, current?: string) =>
    request(`/locked/pin`, { method: "POST", body: JSON.stringify({ new: next, current }) }),
  openLocked: (pin: string) => request<{ open: boolean; minutes: number }>(`/locked/open`, {
    method: "POST", body: JSON.stringify({ pin }),
  }),
  closeLocked: () => request(`/locked/close`, { method: "POST" }),
  lockedPhotos: () => request<PhotoIndex>("/locked/photos"),
  lock: (photo_ids: number[]) =>
    request<{ locked: number }>(`/photos/lock`, { method: "POST", body: JSON.stringify({ photo_ids }) }),
  unlock: (photo_ids: number[]) =>
    request<{ unlocked: number }>(`/photos/unlock`, { method: "POST", body: JSON.stringify({ photo_ids }) }),
  archive: (photo_ids: number[], archived = true) =>
    request<{ changed: number }>(`/photos/archive`, { method: "POST", body: JSON.stringify({ photo_ids, archived }) }),
  logout: () => request(`/auth/logout`, { method: "POST" }),
  setPassword: (current: string, next: string) =>
    request<{ protected: boolean }>(`/auth/password`, { method: "POST", body: JSON.stringify({ current, new: next }) }),
  shareAlbum: (id: number, allow_download: boolean, expires_days: number | null, allow_upload = false) =>
    request<ShareLink>(`/albums/${id}/share`, {
      method: "POST", body: JSON.stringify({ allow_download, expires_days, allow_upload }),
    }),
  shares: (id: number) => request<{ shares: ShareLink[] }>(`/albums/${id}/shares`),
  revokeShare: (token: string) => request(`/shares/${token}`, { method: "DELETE" }),
  shared: (token: string) =>
    request<{ name: string; allow_download: boolean; allow_upload: boolean; photos: PhotoIndex }>(`/share/${token}`),

  exportPreview: (spec: ExportSpec) =>
    request<{ items: number; bytes: number; groups: { label: string; count: number; bytes: number }[] }>(
      `/export/preview`, { method: "POST", body: JSON.stringify(spec) }),
  startExport: (spec: ExportSpec) =>
    request<{ job_id: number }>(`/export`, { method: "POST", body: JSON.stringify(spec) }),

  finishUpload: () => request<{ job_id: number }>(`/upload/finish`, { method: "POST" }),
  startBackup: (folder?: string) =>
    request<{ job_id: number }>(`/backup`, { method: "POST", body: JSON.stringify({ folder }) }),
  backupStatus: () => request<{ last: any | null; folder: string; every_days: number }>("/backup"),

  trash: () => request<{ items: TrashItem[]; bytes: number; days: number; allow_delete: boolean }>("/trash"),
  moveToTrash: (photo_ids: number[], confirm: number) =>
    request<{ trashed: number; bytes: number; skipped: { photo_id: number; reason: string }[]; expires_at: number }>(
      `/trash`, { method: "POST", body: JSON.stringify({ photo_ids, confirm }) }),
  restoreFromTrash: (photo_ids: number[]) =>
    request<{ restored: number; failed: { photo_id: number; reason: string }[] }>(`/trash/restore`, {
      method: "POST", body: JSON.stringify({ photo_ids }),
    }),
  eraseFromTrash: (photo_ids: number[], confirm: number) =>
    request<{ erased: number; bytes: number }>(`/trash/erase`, { method: "POST", body: JSON.stringify({ photo_ids, confirm }) }),
  emptyTrash: (confirm: number) =>
    request<{ erased: number; bytes: number }>(`/trash/empty`, { method: "POST", body: JSON.stringify({ confirm }) }),

  clearCache: (kind: string) => request<any>(`/cache/clear${qs({ kind })}`, { method: "POST" }),
};

/** `rot` is part of the URL because thumbnails are cached for good: a turned photo needs a new address. */
export const thumbUrl = (id: number, size: "sm" | "m" | "l" = "m", rot = 0) =>
  `${BASE}/thumb/${id}?s=${size}${rot ? `&r=${rot}` : ""}`;
export const originalUrl = (id: number, rot = 0) => `${BASE}/photos/${id}/original${rot ? `?r=${rot}` : ""}`;
export const downloadUrl = (id: number) => `${BASE}/photos/${id}/download`;
export const videoUrl = (id: number) => `${BASE}/photos/${id}/video`;
export const motionUrl = (id: number) => `${BASE}/photos/${id}/motion`;
export const sharedThumbUrl = (token: string, id: number, size: "sm" | "m" | "l" = "m") =>
  `${BASE}/share/${token}/thumb/${id}?s=${size}`;
export const sharedVideoUrl = (token: string, id: number) => `${BASE}/share/${token}/video/${id}`;
export const sharedDownloadUrl = (token: string, id: number) => `${BASE}/share/${token}/download/${id}`;
export const randomImageUrl = (params: Record<string, any> = {}) => `${BASE}/random/image${qs(params)}`;
/** Let the browser download the zip itself (a form post streams straight to disk). */
export function downloadZip(spec: ExportSpec) {
  const form = document.createElement("form");
  form.method = "POST";
  form.action = `${BASE}/export/zip`;
  form.style.display = "none";
  const field = document.createElement("input");
  field.type = "hidden";
  field.name = "spec";
  field.value = JSON.stringify(spec);
  form.appendChild(field);
  document.body.appendChild(form);
  form.submit();
  form.remove();
}

export const faceUrl = (id: number, size = 200) => `${BASE}/faces/${id}/crop?size=${size}`;
