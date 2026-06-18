import {
  Dispatch,
  ReactNode,
  SetStateAction,
  createContext,
  useCallback,
  useContext,
  useRef,
  useState,
} from "react";

/* A tiny key/value store mounted ABOVE the router, so per-screen UI state
   survives navigating to another tab and back (the screen unmounts, but this
   store does not). `useStickyState` is a drop-in for `useState` that reads its
   initial value from the store and writes every change back to it. Like the
   wizard/scanJob contexts, it intentionally does NOT persist across a full page
   reload — a fresh load starts clean. Keys are global, so namespace them per
   screen (e.g. "settings.quickRescan"). */

const UIStateContext = createContext<Map<string, unknown> | null>(null);

export function UIStateProvider({ children }: { children: ReactNode }) {
  const store = useRef<Map<string, unknown>>(new Map()).current;
  return <UIStateContext.Provider value={store}>{children}</UIStateContext.Provider>;
}

export function useStickyState<T>(key: string, initial: T): [T, Dispatch<SetStateAction<T>>] {
  const store = useContext(UIStateContext);
  if (!store) throw new Error("useStickyState must be used within UIStateProvider");
  const [value, setValue] = useState<T>(() => (store.has(key) ? (store.get(key) as T) : initial));
  const set = useCallback<Dispatch<SetStateAction<T>>>(
    (v) => {
      setValue((prev) => {
        const next = typeof v === "function" ? (v as (p: T) => T)(prev) : v;
        store.set(key, next);
        return next;
      });
    },
    [key, store],
  );
  return [value, set];
}
