import { AlertTriangle, ImageOff, Loader2, RefreshCw } from "lucide-react";
import { Link } from "react-router-dom";

export function Spinner({ label, full }: { label?: string; full?: boolean }) {
  return (
    <div className={`state${full ? " state-full" : ""}`}>
      <Loader2 className="spin" size={22} />
      {label && <span className="dim">{label}</span>}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message = error instanceof Error ? error.message : String(error ?? "Unknown error");
  return (
    <div className="state state-full">
      <AlertTriangle size={24} className="danger-text" />
      <h3>Something went wrong</h3>
      <p className="dim" style={{ maxWidth: 420, textAlign: "center" }}>{message}</p>
      {onRetry && (
        <button className="btn btn-ghost" onClick={onRetry}>
          <RefreshCw size={15} /> Try again
        </button>
      )}
    </div>
  );
}

export function EmptyState({ icon, title, hint, action }: {
  icon?: React.ReactNode; title: string; hint?: string; action?: React.ReactNode;
}) {
  return (
    <div className="state state-full empty">
      <div className="empty-icon">{icon ?? <ImageOff size={26} />}</div>
      <h3>{title}</h3>
      {hint && <p className="dim empty-hint">{hint}</p>}
      {action}
    </div>
  );
}

export function NoLibrary() {
  return (
    <EmptyState
      title="No photos yet"
      hint="Point Memoria at a folder of photos and it will index them locally — nothing leaves this machine."
      action={<Link to="/settings" className="btn btn-primary">Choose a photo folder</Link>}
    />
  );
}

export function SkeletonGrid({ count = 18 }: { count?: number }) {
  return (
    <div className="skeleton-grid">
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} className="skeleton" style={{ aspectRatio: i % 3 === 0 ? "3/4" : "4/3" }} />
      ))}
    </div>
  );
}

export function SectionHeader({ title, count, action, sub }: {
  title: string; count?: number | string; action?: React.ReactNode; sub?: string;
}) {
  return (
    <div className="section-head">
      <div>
        <h2>
          {title}
          {count !== undefined && <span className="section-count tnum dim">{count}</span>}
        </h2>
        {sub && <p className="dim section-sub">{sub}</p>}
      </div>
      {action}
    </div>
  );
}
