import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Check, Search, UserPlus, Users, X } from "lucide-react";
import { api, faceUrl, thumbUrl } from "../lib/api";
import { EmptyState, ErrorState, Spinner } from "../components/States";
import { useViewer } from "../components/ViewerContext";
import { useTitle } from "../lib/hooks";

/** Faces Memoria could not confidently group. Naming one here teaches it the person. */
export default function UnassignedFaces() {
  useTitle("Ungrouped faces");
  const qc = useQueryClient();
  const viewer = useViewer();
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [name, setName] = useState("");
  const [personFilter, setPersonFilter] = useState("");
  const [minQuality, setMinQuality] = useState(0.35);

  const faces = useQuery({
    queryKey: ["unassigned-faces", minQuality],
    queryFn: () => api.unassignedFaces({ limit: 400, min_quality: minQuality }),
  });
  const people = useQuery({ queryKey: ["people", { sort: "photos" }], queryFn: () => api.people({ sort: "photos" }) });

  const assign = useMutation({
    mutationFn: ({ ids, personId, newName }: { ids: number[]; personId?: number; newName?: string }) =>
      api.assignFaces(ids, personId, newName),
    onSuccess: () => {
      setSelected(new Set());
      setName("");
      qc.invalidateQueries();
    },
  });

  const candidates = useMemo(() => {
    const list = people.data?.people ?? [];
    if (!personFilter) return list.slice(0, 24);
    return list.filter((p) => p.label.toLowerCase().includes(personFilter.toLowerCase())).slice(0, 24);
  }, [people.data, personFilter]);

  if (faces.isError) return <ErrorState error={faces.error} onRetry={() => faces.refetch()} />;

  const list = faces.data?.faces ?? [];
  const toggle = (id: number) =>
    setSelected((s) => {
      const next = new Set(s);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  return (
    <div className="page">
      <Link to="/people" className="back-link"><ArrowLeft size={15} /> People</Link>
      <div className="page-head">
        <div>
          <h1 className="display">Ungrouped faces</h1>
          <p className="dim">
            Faces that appear too rarely, or too indistinctly, to become a person on their own.
            Name one and Memoria will look for that person everywhere else.
          </p>
        </div>
        <div className="segmented" role="group" aria-label="Quality filter">
          <button className={minQuality === 0.5 ? "on" : ""} onClick={() => setMinQuality(0.5)}>Clear</button>
          <button className={minQuality === 0.35 ? "on" : ""} onClick={() => setMinQuality(0.35)}>Most</button>
          <button className={minQuality === 0 ? "on" : ""} onClick={() => setMinQuality(0)}>All</button>
        </div>
      </div>

      {selected.size > 0 && (
        <div className="review-bar">
          <span>{selected.size} selected</span>
          <form
            className="assign-form"
            onSubmit={(e) => {
              e.preventDefault();
              if (name.trim()) assign.mutate({ ids: [...selected], newName: name.trim() });
            }}
          >
            <input className="field" placeholder="Name this person" value={name}
              onChange={(e) => setName(e.target.value)} aria-label="New person name" />
            <button className="btn btn-primary btn-sm" type="submit" disabled={!name.trim()}>
              <UserPlus size={14} /> Create
            </button>
          </form>
          <div className="input-inline">
            <Search size={13} className="dim" />
            <input value={personFilter} onChange={(e) => setPersonFilter(e.target.value)}
              placeholder="or add to someone" aria-label="Find existing person" />
          </div>
          <button className="btn btn-quiet btn-sm" onClick={() => setSelected(new Set())}>
            <X size={14} /> Clear
          </button>
        </div>
      )}

      {selected.size > 0 && personFilter && (
        <div className="assign-candidates">
          {candidates.map((p) => (
            <button key={p.id} className="face-chip"
              onClick={() => assign.mutate({ ids: [...selected], personId: p.id })}>
              <span className="face-chip-img">
                {p.cover_face_id ? <img src={faceUrl(p.cover_face_id, 120)} alt="" /> : <Users size={16} />}
              </span>
              <span className="face-chip-name">{p.label}</span>
            </button>
          ))}
          {candidates.length === 0 && <p className="dim">No one matches “{personFilter}”.</p>}
        </div>
      )}

      {faces.isLoading ? (
        <Spinner label="Loading faces" />
      ) : list.length === 0 ? (
        <EmptyState icon={<Users size={26} />} title="Every face is grouped"
          hint="Nothing left to review here." />
      ) : (
        <div className="face-review-grid">
          {list.map((f: any) => (
            <button key={f.id} className={`face-review${selected.has(f.id) ? " is-selected" : ""}`}
              onClick={(e) => (e.shiftKey ? viewer.open([f.photo_id], 0) : toggle(f.id))}
              title="Click to select · Shift-click to open the photo">
              <img src={faceUrl(f.id, 160)} alt="" loading="lazy" />
              {selected.has(f.id) && (
                <span className="face-review-check"><Check size={14} strokeWidth={3} /></span>
              )}
              <span className="face-review-conf tnum">{Math.round((f.quality ?? 0) * 100)}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
