import { useEffect, useState } from "react";
import { get } from "../lib/api";
import { Button, inputCls } from "./ui";

interface FsEntry {
  name: string;
  path: string;
}

interface FsList {
  path: string;
  parent: string | null;
  dirs: FsEntry[];
  files: FsEntry[];
  home: string;
}

/** Modal browser backed by GET /api/fs/list (read-only server-side scandir).
    The backend runs on the analyst's own machine, so this browses the same
    drives/mounts a native picker would — including UNC shares. With
    pickFiles, files are listed too and the selection is a file path (the
    "Add a missed file" picker in New Run > Confirm documents). */
export function FolderPicker({
  title,
  initial,
  onSelect,
  onSelectMany,
  onClose,
  pickFiles = false,
  multiple = false,
}: {
  title: string;
  initial: string;
  onSelect: (path: string) => void;
  onSelectMany?: (paths: string[]) => void;
  onClose: () => void;
  pickFiles?: boolean;
  /** Multi-file selection (pickFiles only): clicking toggles files; the footer
      commits the whole set via onSelectMany. Single-select behavior (onSelect)
      is unchanged when multiple is falsy. */
  multiple?: boolean;
}) {
  const multi = multiple && pickFiles;
  const [listing, setListing] = useState<FsList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pathInput, setPathInput] = useState(initial);
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);

  const toggle = (path: string) =>
    setChecked((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });

  const load = async (path: string, fallbackToRoots = false) => {
    setBusy(true);
    setError(null);
    setSelectedFile(null);
    try {
      const q = pickFiles ? "&files=true" : "";
      const l = await get<FsList>(`/api/fs/list?path=${encodeURIComponent(path)}${q}`);
      setListing(l);
      setPathInput(l.path || "");
    } catch (e) {
      if (fallbackToRoots && path !== "") {
        await load("");
      } else {
        setError((e as Error).message);
      }
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    // The configured path may not exist on this machine (e.g. a UNC path
    // while running under WSL) — fall back to the filesystem roots.
    load(initial, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const atRoots = listing !== null && listing.path === "";
  const canSelect = pickFiles ? selectedFile !== null : Boolean(pathInput);
  const selectTarget = pickFiles ? selectedFile : pathInput;

  return (
    <div className="fixed inset-0 z-50 bg-black/30 flex items-center justify-center p-6" onClick={onClose}>
      <div
        className="bg-paper border border-line rounded-[var(--hl-radius)] shadow-lift w-[600px] max-h-[80vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-4 py-3 border-b border-line flex items-center justify-between">
          <p className="text-[13.5px] font-semibold text-ink-900">{title}</p>
          <button className="text-ink-400 hover:text-ink-700 text-[14px]" onClick={onClose}>
            ✕
          </button>
        </div>

        <div className="px-4 py-3 space-y-2 border-b border-line">
          <div className="flex gap-2">
            <input
              className={`${inputCls} flex-1 font-mono text-[12px]`}
              value={pathInput}
              placeholder="type a path, or browse below"
              onChange={(e) => setPathInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") load(pathInput);
              }}
            />
            <Button kind="secondary" onClick={() => load(pathInput)} disabled={busy}>
              Go
            </Button>
          </div>
          <div className="flex gap-2">
            <Button kind="ghost" onClick={() => listing?.parent != null && load(listing.parent)} disabled={busy || !listing || listing.parent === null}>
              ↑ Up
            </Button>
            <Button kind="ghost" onClick={() => listing && load(listing.home)} disabled={busy || !listing}>
              Home
            </Button>
            <Button kind="ghost" onClick={() => load("")} disabled={busy || atRoots}>
              Drives / roots
            </Button>
          </div>
          {error && <p className="text-[12px] text-err break-all">{error}</p>}
        </div>

        <div className="flex-1 overflow-y-auto min-h-[200px]">
          {listing && listing.dirs.length === 0 && (!pickFiles || listing.files.length === 0) && (
            <p className="px-4 py-6 text-[12.5px] text-ink-400">{pickFiles ? "nothing here" : "no subfolders"}</p>
          )}
          {listing?.dirs.map((d) => (
            <button
              key={d.path}
              className="w-full text-left px-4 py-1.5 text-[12.5px] text-ink-800 hover:bg-surface flex items-center gap-2"
              onDoubleClick={() => load(d.path)}
              onClick={() => {
                setPathInput(d.path);
                setSelectedFile(null);
              }}
            >
              <span className="text-ink-400">📁</span>
              <span className="truncate">{d.name}</span>
            </button>
          ))}
          {pickFiles &&
            listing?.files.map((f) => {
              const on = multi ? checked.has(f.path) : selectedFile === f.path;
              return (
                <button
                  key={f.path}
                  className={`w-full text-left px-4 py-1.5 text-[12.5px] hover:bg-surface flex items-center gap-2 ${
                    on ? "bg-info-soft text-ink-900" : "text-ink-700"
                  }`}
                  onDoubleClick={() => !multi && onSelect(f.path)}
                  onClick={() => {
                    if (multi) {
                      toggle(f.path);
                    } else {
                      setSelectedFile(f.path);
                      setPathInput(f.path);
                    }
                  }}
                >
                  <span className="text-ink-400">{multi ? (on ? "☑" : "☐") : "📄"}</span>
                  <span className="truncate">{f.name}</span>
                </button>
              );
            })}
        </div>

        <div className="px-4 py-3 border-t border-line flex items-center justify-between gap-3">
          <p className="text-[11px] text-ink-500 truncate font-mono" title={pathInput}>
            {pathInput || "—"}
          </p>
          <div className="flex gap-2 shrink-0">
            <Button kind="ghost" onClick={onClose}>
              Cancel
            </Button>
            {multi ? (
              <Button
                kind="primary"
                onClick={() => onSelectMany?.([...checked])}
                disabled={checked.size === 0}
              >
                Add {checked.size} file{checked.size === 1 ? "" : "s"}
              </Button>
            ) : (
              <Button kind="primary" onClick={() => selectTarget && onSelect(selectTarget)} disabled={!canSelect}>
                {pickFiles ? "Select this file" : "Select this folder"}
              </Button>
            )}
          </div>
        </div>
        <p className="px-4 pb-2 text-[10.5px] text-ink-400">
          {multi
            ? "click to check/uncheck files · double click a folder to open · then Add"
            : pickFiles
            ? "single click = select · double click = open folder / pick file"
            : "single click = select · double click = open"}
        </p>
      </div>
    </div>
  );
}
