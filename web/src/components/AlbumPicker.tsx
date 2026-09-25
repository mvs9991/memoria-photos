/** "Add to album" dialog: pick an existing album or create one with these photos. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookImage, Check, Plus, X } from "lucide-react";
import { Portal } from "./Portal";
import { api, thumbUrl } from "../lib/api";
import { Spinner } from "./States";

export function AlbumPicker({ photoIds, onClose, onDone }: {
  photoIds: number[];
  onClose: () => void;
  onDone?: (albumId: number) => void;
}) {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const albums = useQuery({ queryKey: ["albums"], queryFn: api.albums });

  const finish = (id: number) => {
    qc.invalidateQueries({ queryKey: ["albums"] });
    qc.invalidateQueries({ queryKey: ["album", id] });
    photoIds.forEach((p) => qc.invalidateQueries({ queryKey: ["photo", p] }));
    onDone?.(id);
    onClose();
  };
  const add = useMutation({
    mutationFn: (id: number) => api.addToAlbum(id, photoIds).then(() => id),
    onSuccess: finish,
  });
  const create = useMutation({
    mutationFn: () => api.createAlbum(name.trim(), photoIds).then((r) => r.id),
    onSuccess: finish,
  });

  const count = photoIds.length === 1 ? "this photo" : `${photoIds.length.toLocaleString()} photos`;
  return (
    <Portal>
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Add to album">
        <div className="modal-head">
          <h3><BookImage size={16} /> Add {count} to an album</h3>
          <button className="btn btn-quiet btn-icon btn-sm" onClick={onClose} aria-label="Close"><X size={16} /></button>
        </div>
        <form className="modal-path" onSubmit={(e) => { e.preventDefault(); if (name.trim()) create.mutate(); }}>
          <input className="field" autoFocus placeholder="New album name" value={name}
            onChange={(e) => setName(e.target.value)} aria-label="New album name" style={{ flex: 1 }} />
          <button className="btn btn-primary btn-sm" type="submit" disabled={!name.trim() || create.isPending}>
            <Plus size={14} /> Create
          </button>
        </form>
        <div className="modal-body">
          {albums.isLoading ? <Spinner /> : (
            <ul className="browse-list">
              {(albums.data?.albums ?? []).map((a) => (
                <li key={a.id}>
                  <button className="browse-row album-pick-row" onClick={() => add.mutate(a.id)}
                    disabled={add.isPending}>
                    <span className="album-pick-thumb">
                      {a.cover_photo_id ? <img src={thumbUrl(a.cover_photo_id, "sm")} alt="" /> : <BookImage size={15} />}
                    </span>
                    <span className="ellipsis" style={{ flex: 1 }}>{a.name}</span>
                    <span className="dim tnum">{a.photo_count.toLocaleString()}</span>
                    <Check size={14} className="dim" />
                  </button>
                </li>
              ))}
              {(albums.data?.albums ?? []).length === 0 && (
                <li className="dim" style={{ padding: 12 }}>No albums yet — name one above.</li>
              )}
            </ul>
          )}
        </div>
      </div>
    </div>
    </Portal>
  );
}
