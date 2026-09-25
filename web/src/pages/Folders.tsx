/** Browse the library the way it sits on disk: roots, subfolders, then the photos in one folder. */
import { useMemo } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight, Folder, FolderOpen, HardDrive } from "lucide-react";
import { api, thumbUrl } from "../lib/api";
import { PhotoGrid } from "../components/PhotoGrid";
import { EmptyState, ErrorState, SectionHeader, Spinner } from "../components/States";
import { useViewer } from "../components/ViewerContext";
import { useTitle } from "../lib/hooks";

/** A root is stored as a full path; show its last part, keep the path for the tooltip. */
const baseName = (path: string) => path.split(/[\\/]/).filter(Boolean).pop() ?? path;

export default function Folders() {
  const [params] = useSearchParams();
  const viewer = useViewer();
  const rootId = params.get("root") ? Number(params.get("root")) : undefined;
  const path = params.get("path") ?? "";
  const tree = useQuery({ queryKey: ["folders-browse", rootId, path], queryFn: () => api.browseFolders(rootId, path) });
  const roots = useQuery({ queryKey: ["folders-browse", undefined, ""], queryFn: () => api.browseFolders() });
  const direct = useQuery({
    queryKey: ["photos", { root: rootId, folder: path, exact: true }],
    queryFn: () => api.photosInFolder(rootId!, path),
    enabled: rootId !== undefined && (tree.data?.direct_count ?? 0) > 0,
  });
  const rootName = roots.data?.folders.find((r) => r.root_id === rootId)?.name ?? "";
  const here = path ? path.split("/").pop()! : baseName(rootName);
  useTitle(here || "Folders");

  const items = useMemo(() => {
    if (!direct.data) return [];
    const { ids, ratio, ts, flags, dur, rating } = direct.data;
    return ids.map((id, i) => ({ id, ratio: ratio[i], ts: ts[i], flags: flags[i], dur: dur?.[i] ?? 0, rating: rating?.[i] ?? 0 }));
  }, [direct.data]);

  if (tree.isError) return <ErrorState error={tree.error} onRetry={() => tree.refetch()} />;
  if (tree.isLoading || !tree.data) return <Spinner full label="Loading folders" />;

  const parts = path ? path.split("/") : [];
  const link = (upto: number) => `/folders?root=${rootId}&path=${encodeURIComponent(parts.slice(0, upto).join("/"))}`;

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="display">{rootId === undefined ? "Folders" : here || "Folders"}</h1>
          <nav className="crumbs" aria-label="Folder path">
            <Link to="/folders">All folders</Link>
            {rootId !== undefined && <><ChevronRight size={13} /><Link to={`/folders?root=${rootId}`}
              className="ellipsis" title={rootName}>{baseName(rootName)}</Link></>}
            {parts.map((p, i) => (
              <span key={i} className="crumb">
                <ChevronRight size={13} /><Link to={link(i + 1)}>{p}</Link>
              </span>
            ))}
          </nav>
        </div>
      </div>

      {tree.data.folders.length === 0 && tree.data.direct_count === 0 ? (
        <EmptyState icon={<Folder size={26} />} title="No photos in this folder" />
      ) : (
        <div className="folder-grid">
          {tree.data.folders.map((f) => (
            <Link key={`${f.root_id}:${f.path}`} className="folder-tile"
              to={`/folders?root=${f.root_id}${f.path ? `&path=${encodeURIComponent(f.path)}` : ""}`}>
              <div className="folder-img">
                {f.cover_photo_id ? <img src={thumbUrl(f.cover_photo_id, "sm")} alt="" loading="lazy" />
                  : <FolderOpen size={28} className="dim" />}
              </div>
              <div className="folder-body">
                {rootId === undefined ? <HardDrive size={14} className="dim" /> : <Folder size={14} className="dim" />}
                <span className="ellipsis" title={f.name}>{rootId === undefined ? baseName(f.name) : f.name}</span>
                <span className="dim tnum">{f.count.toLocaleString()}</span>
              </div>
            </Link>
          ))}
        </div>
      )}

      {tree.data.direct_count > 0 && (
        <section style={{ marginTop: 28 }}>
          <SectionHeader title="In this folder" count={tree.data.direct_count.toLocaleString()} />
          {direct.isLoading ? <Spinner /> : (
            <PhotoGrid items={items} grouping="none" targetHeight={200} scrubber={false}
              onOpen={(_, index) => viewer.open(items.map((i) => i.id), index)} />
          )}
        </section>
      )}
    </div>
  );
}
