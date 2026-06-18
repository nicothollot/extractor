import { ReactNode, useMemo, useState } from "react";
import { EmptyState, ErrorState, SkeletonRows } from "./ui";

export interface Column<T> {
  key: string;
  header: string;
  render: (row: T) => ReactNode;
  sortValue?: (row: T) => string | number | null;
  /** Plain text used for filtering this column. Falls back to sortValue. */
  filterValue?: (row: T) => string | number | null;
  width?: string;
  align?: "left" | "right";
}

/** Dense, sortable table with designed loading/empty/error states
    (skeleton rows — never a spinner). Pass `filterable` for spreadsheet-style
    filtering: a global free-text box plus per-column filter inputs (any
    column with a sortValue/filterValue is filterable). */
export function DataTable<T>({
  columns,
  rows,
  loading = false,
  error = null,
  onRetry,
  emptyTitle = "Nothing here yet",
  emptyHint,
  rowKey,
  onRowClick,
  selectedKey,
  maxHeight,
  filterable = false,
}: {
  columns: Column<T>[];
  rows: T[] | null;
  loading?: boolean;
  error?: string | null;
  onRetry?: () => void;
  emptyTitle?: string;
  emptyHint?: string;
  rowKey: (row: T) => string;
  onRowClick?: (row: T) => void;
  selectedKey?: string | null;
  maxHeight?: string;
  filterable?: boolean;
}) {
  const [sort, setSort] = useState<{ key: string; dir: 1 | -1 } | null>(null);
  const [globalFilter, setGlobalFilter] = useState("");
  const [colFilters, setColFilters] = useState<Record<string, string>>({});

  const filterText = (col: Column<T>, row: T): string => {
    const fn = col.filterValue ?? col.sortValue;
    return fn ? String(fn(row) ?? "") : "";
  };

  const filtered = useMemo(() => {
    if (!rows) return [];
    const g = globalFilter.trim().toLowerCase();
    const active = Object.entries(colFilters).filter(([, v]) => v.trim() !== "");
    if (!filterable || (!g && active.length === 0)) return rows;
    return rows.filter((row) => {
      if (g && !columns.some((c) => filterText(c, row).toLowerCase().includes(g))) return false;
      for (const [key, value] of active) {
        const col = columns.find((c) => c.key === key);
        if (!col) continue;
        if (!filterText(col, row).toLowerCase().includes(value.trim().toLowerCase())) return false;
      }
      return true;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, globalFilter, colFilters, filterable, columns]);

  const sorted = useMemo(() => {
    if (!sort) return filtered;
    const col = columns.find((c) => c.key === sort.key);
    if (!col?.sortValue) return filtered;
    return [...filtered].sort((a, b) => {
      const va = col.sortValue!(a);
      const vb = col.sortValue!(b);
      if (va === null && vb === null) return 0;
      if (va === null) return 1;
      if (vb === null) return -1;
      return (va < vb ? -1 : va > vb ? 1 : 0) * sort.dir;
    });
  }, [filtered, sort, columns]);

  if (loading) return <SkeletonRows rows={6} cols={Math.min(columns.length, 6)} />;
  if (error) return <ErrorState message={error} onRetry={onRetry} />;
  if (!rows || rows.length === 0) return <EmptyState title={emptyTitle} hint={emptyHint} />;

  const anyColFilters = Object.values(colFilters).some((v) => v.trim() !== "");

  return (
    <div>
      {filterable && (
        <div className="flex items-center gap-2 px-3 py-2 border-b border-line">
          <input
            className="w-64 px-2 py-1 text-[12.5px] bg-paper border border-line-strong rounded-[var(--hl-radius)] focus:outline-none focus:border-accent"
            value={globalFilter}
            placeholder="Filter rows…"
            onChange={(e) => setGlobalFilter(e.target.value)}
          />
          <span className="text-[11.5px] text-ink-400">
            {sorted.length} of {rows.length}
          </span>
          {(globalFilter || anyColFilters) && (
            <button
              type="button"
              className="text-[11.5px] text-[var(--hl-blue)] underline"
              onClick={() => {
                setGlobalFilter("");
                setColFilters({});
              }}
            >
              clear
            </button>
          )}
        </div>
      )}
      <div className="overflow-auto" style={maxHeight ? { maxHeight } : undefined}>
        <table className="w-full text-[13px] border-collapse">
          <thead>
            <tr className="sticky top-0 bg-paper z-10">
              {columns.map((col) => (
                <th
                  key={col.key}
                  style={col.width ? { width: col.width } : undefined}
                  className={`text-left font-semibold text-[11px] uppercase tracking-wide text-ink-500 border-b border-line px-3 py-2 select-none ${
                    col.sortValue ? "cursor-pointer hover:text-ink-800" : ""
                  } ${col.align === "right" ? "text-right" : ""}`}
                  onClick={() =>
                    col.sortValue &&
                    setSort((s) =>
                      s?.key === col.key ? { key: col.key, dir: s.dir === 1 ? -1 : 1 } : { key: col.key, dir: 1 },
                    )
                  }
                >
                  {col.header}
                  {sort?.key === col.key && <span className="ml-1">{sort.dir === 1 ? "▲" : "▼"}</span>}
                </th>
              ))}
            </tr>
            {filterable && (
              <tr className="sticky top-[33px] bg-paper z-10">
                {columns.map((col) => {
                  const canFilter = Boolean(col.filterValue ?? col.sortValue);
                  return (
                    <th key={col.key} className="border-b border-line px-2 py-1 font-normal">
                      {canFilter && (
                        <input
                          className="w-full px-1.5 py-0.5 text-[11.5px] bg-surface border border-line rounded focus:outline-none focus:border-accent"
                          value={colFilters[col.key] ?? ""}
                          placeholder="filter"
                          onClick={(e) => e.stopPropagation()}
                          onChange={(e) =>
                            setColFilters((f) => ({ ...f, [col.key]: e.target.value }))
                          }
                        />
                      )}
                    </th>
                  );
                })}
              </tr>
            )}
          </thead>
          <tbody>
            {sorted.map((row) => {
              const key = rowKey(row);
              const selected = selectedKey === key;
              return (
                <tr
                  key={key}
                  onClick={() => onRowClick?.(row)}
                  className={`border-b border-line last:border-0 ${
                    onRowClick ? "cursor-pointer" : ""
                  } ${selected ? "bg-info-soft" : onRowClick ? "hover:bg-ink-50" : ""}`}
                >
                  {columns.map((col) => (
                    <td
                      key={col.key}
                      className={`px-3 py-2 align-top text-ink-800 ${col.align === "right" ? "text-right" : ""}`}
                    >
                      {col.render(row)}
                    </td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
        {sorted.length === 0 && <EmptyState title="No matching rows" hint="Adjust or clear the filters." />}
      </div>
    </div>
  );
}
