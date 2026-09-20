import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Copy, EyeOff, Info, ShieldCheck, Star } from "lucide-react";
import { api, thumbUrl } from "../lib/api";
import { EmptyState, ErrorState, Spinner } from "../components/States";
import { useViewer } from "../components/ViewerContext";
import { formatBytes, formatDate } from "../lib/format";
import { useTitle } from "../lib/hooks";

const KIND_HELP: Record<string, string> = {
  exact: "Byte-for-byte identical files.",
  near: "The same image resaved, resized or recompressed.",
  likely: "Very probably the same photo — edited, cropped or screenshotted.",
  similar: "Different shots of the same moment (a burst). Keep the best one.",
};

export default function Duplicates() {
  useTitle("Duplicates");
  const qc = useQueryClient();
  const viewer = useViewer();
  const [kind, setKind] = useState<string>("");
  const [status, setStatus] = useState<"pending" | "reviewed" | "all">("pending");

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["duplicates", kind, status],
    queryFn: () => api.duplicates({ kind: kind || undefined, status, limit: 120 }),
  });

  const review = useMutation({
    mutationFn: ({ id, body }: { id: number; body: any }) => api.reviewDuplicate(id, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["duplicates"] }),
  });
  const hide = useMutation({
    mutationFn: ({ id, ids }: { id: number; ids: number[] }) => api.hideCopies(id, ids),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["duplicates"] });
      qc.invalidateQueries({ queryKey: ["photos"] });
      qc.invalidateQueries({ queryKey: ["stats"] });
    },
  });

  if (isError) return <ErrorState error={error} onRetry={() => refetch()} />;
  if (isLoading) return <Spinner full label="Finding duplicates" />;

  const groups = data?.groups ?? [];
  const counts = data?.counts ?? {};

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="display">Duplicates</h1>
          <p className="dim">
            {(data?.total ?? 0).toLocaleString()} groups
            {data?.reclaimable_bytes ? ` · ${formatBytes(data.reclaimable_bytes)} in redundant copies` : ""}
          </p>
        </div>
        <div className="segmented" role="group" aria-label="Review status">
          <button className={status === "pending" ? "on" : ""} onClick={() => setStatus("pending")}>To review</button>
          <button className={status === "reviewed" ? "on" : ""} onClick={() => setStatus("reviewed")}>Reviewed</button>
          <button className={status === "all" ? "on" : ""} onClick={() => setStatus("all")}>All</button>
        </div>
      </div>

      <div className="filter-bar">
        <button className={`chip chip-button${!kind ? " chip-accent" : ""}`} onClick={() => setKind("")}>
          All kinds
        </button>
        {["exact", "near", "likely", "similar"].map((k) => (
          <button key={k} className={`chip chip-button${kind === k ? " chip-accent" : ""}`}
            onClick={() => setKind(k)} title={KIND_HELP[k]}>
            {k} <span className="dim tnum">{counts[k] ?? 0}</span>
          </button>
        ))}
      </div>

      <p className="notice notice-quiet">
        <ShieldCheck size={14} /> Memoria never deletes or edits your files. “Hide copies” only removes them from
        your library views — the originals stay exactly where they are.
      </p>

      {groups.length === 0 ? (
        <EmptyState icon={<Copy size={26} />} title="Nothing to review"
          hint={status === "pending" ? "No duplicate groups are waiting for review." : "No groups match this filter."} />
      ) : (
        <div className="dup-list">
          {groups.map((g: any) => (
            <div key={g.id} className="dup-group card">
              <div className="dup-head">
                <span className={`chip chip-${g.kind}`} title={KIND_HELP[g.kind]}>{g.kind}</span>
                <span className="dim">{g.member_count} copies</span>
                {g.reclaimable_bytes > 0 && (
                  <span className="dim">· {formatBytes(g.reclaimable_bytes)} redundant</span>
                )}
                <div className="dup-head-actions">
                  <button className="btn btn-ghost btn-sm"
                    onClick={() => hide.mutate({
                      id: g.id,
                      ids: g.members.filter((m: any) => !m.is_keeper).map((m: any) => m.photo_id),
                    })}>
                    <EyeOff size={13} /> Hide {g.member_count - 1} {g.member_count === 2 ? "copy" : "copies"}
                  </button>
                  <button className="btn btn-quiet btn-sm"
                    onClick={() => review.mutate({ id: g.id, body: { status: "not_duplicate" } })}>
                    Not duplicates
                  </button>
                  <button className="btn btn-quiet btn-sm"
                    onClick={() => review.mutate({ id: g.id, body: { status: "reviewed" } })}>
                    <Check size={13} /> Mark reviewed
                  </button>
                </div>
              </div>
              <div className="dup-members">
                {g.members.map((m: any) => (
                  <div key={m.photo_id} className={`dup-member${m.is_keeper ? " is-keeper" : ""}`}>
                    <button className="dup-thumb"
                      onClick={() => viewer.open(g.members.map((x: any) => x.photo_id),
                        g.members.findIndex((x: any) => x.photo_id === m.photo_id))}>
                      <img src={thumbUrl(m.photo_id, "sm")} alt="" loading="lazy" />
                      {m.is_keeper && <span className="dup-keeper-badge"><Star size={11} fill="currentColor" /> keep</span>}
                    </button>
                    <div className="dup-member-info">
                      <div className="dup-relation">{m.relation_label}</div>
                      <div className="dim dup-small ellipsis" title={`${m.folder}/${m.filename}`}>{m.filename}</div>
                      <div className="dim dup-small tnum">
                        {m.width}×{m.height} · {formatBytes(m.size)}
                      </div>
                      <div className="dim dup-small">{formatDate(m.taken_ts)}</div>
                      {!m.is_keeper && (
                        <button className="btn btn-quiet btn-sm dup-keep-btn"
                          onClick={() => review.mutate({ id: g.id, body: { keep_photo_id: m.photo_id } })}>
                          Keep this one instead
                        </button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
