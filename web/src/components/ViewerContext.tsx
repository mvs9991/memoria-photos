import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { useLocation } from "react-router-dom";
import { PhotoViewer } from "./PhotoViewer";

export interface ViewerItem {
  id: number;
}

interface ViewerState {
  open: (ids: number[], index: number) => void;
  close: () => void;
}

const Ctx = createContext<ViewerState>({ open: () => {}, close: () => {} });

export const useViewer = () => useContext(Ctx);

export function ViewerProvider({ children }: { children: React.ReactNode }) {
  const [ids, setIds] = useState<number[]>([]);
  const [index, setIndex] = useState(0);

  const open = useCallback((list: number[], i: number) => {
    setIds(list);
    setIndex(Math.max(0, Math.min(i, list.length - 1)));
    document.body.style.overflow = "hidden";
  }, []);

  const close = useCallback(() => {
    setIds([]);
    document.body.style.overflow = "";
  }, []);

  // Going anywhere closes it: a person, place or album link in its own details panel changed the page
  // underneath while the viewer stayed on top (it looked as if the click did nothing), and a phone's Back
  // button moved the page behind it.
  const { pathname, search } = useLocation();
  const where = useRef(pathname + search);
  useEffect(() => {
    if (where.current === pathname + search) return;
    where.current = pathname + search;
    close();
  }, [pathname, search, close]);

  const value = useMemo(() => ({ open, close }), [open, close]);

  return (
    <Ctx.Provider value={value}>
      {children}
      {ids.length > 0 && (
        <PhotoViewer ids={ids} index={index} onIndex={setIndex} onClose={close} />
      )}
    </Ctx.Provider>
  );
}
