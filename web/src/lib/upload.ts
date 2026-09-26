/** Sending files one by one with progress (fetch cannot report upload progress). */

export type UploadStatus = "waiting" | "uploading" | "added" | "duplicate" | "rejected" | "failed";

export function sendFile(url: string, file: File, onProgress: (p: number) => void):
    Promise<{ status: UploadStatus; reason?: string }> {
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    const form = new FormData();
    form.append("files", file, file.name);
    xhr.open("POST", url);
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
    xhr.onload = () => {
      if (xhr.status === 200) {
        const r = JSON.parse(xhr.responseText).results[0];
        resolve({ status: r.status, reason: r.reason ?? undefined });
      } else if (xhr.status === 401) {
        window.dispatchEvent(new Event("memoria:login-required"));
        resolve({ status: "failed", reason: "sign in again" });
      } else resolve({ status: "failed", reason: `server said ${xhr.status}` });
    };
    xhr.onerror = () => resolve({ status: "failed", reason: "connection lost" });
    xhr.send(form);
  });
}

/** Upload `files` two at a time, reporting each file's progress and outcome. */
export async function uploadAll(url: string, files: File[],
    onUpdate: (index: number, patch: { status: UploadStatus; progress?: number; reason?: string }) => void,
    parallel = 2): Promise<void> {
  let next = 0;
  const worker = async () => {
    while (next < files.length) {
      const i = next++;
      onUpdate(i, { status: "uploading", progress: 0 });
      const r = await sendFile(url, files[i], (p) => onUpdate(i, { status: "uploading", progress: p }));
      onUpdate(i, { status: r.status, reason: r.reason, progress: 1 });
    }
  };
  await Promise.all(Array.from({ length: parallel }, worker));
}
