/**
 * Render an overlay at the document root. A modal opened from inside a sticky bar would
 * otherwise be trapped by it: `backdrop-filter` (and transforms) make an element the
 * containing block for its `position: fixed` descendants, so the "full-screen" layer
 * shrinks to the bar.
 */
import { createPortal } from "react-dom";

export function Portal({ children }: { children: React.ReactNode }) {
  return createPortal(children, document.body);
}
