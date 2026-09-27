import { useEffect } from "react";
import { CloudOff } from "lucide-react";
import { useOffline } from "../lib/offline";

/** Shown while the app is running on this device's offline copy (the server cannot be reached). */
export function OfflineBanner() {
  const offline = useOffline();
  useEffect(() => {
    document.documentElement.classList.toggle("is-offline", offline);   // moves the year scrubber down
  }, [offline]);
  if (!offline) return null;
  return (
    <div className="offline-banner" role="status"
      title="Your library can't be reached from here. Changes need the connection.">
      <CloudOff size={14} aria-hidden />
      <span className="ellipsis">Offline — showing what's on this device</span>
    </div>
  );
}
