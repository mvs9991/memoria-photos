import { createContext, useCallback, useContext, useMemo, useState } from "react";
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
