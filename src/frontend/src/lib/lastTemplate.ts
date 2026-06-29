// Remembers the analyst's most recently used reference workbook across full
// page reloads. uiState/useStickyState is in-memory only (resets on reload),
// so this small localStorage helper persists the last non-empty template path
// the analyst picked. New Run / Direct Run prefill from it when the wizard's
// template is still empty.
const KEY = "pv.lastTemplate";

export function getLastTemplate(): string {
  try {
    return window.localStorage.getItem(KEY) ?? "";
  } catch {
    return "";
  }
}

export function setLastTemplate(path: string): void {
  if (!path) return;
  try {
    window.localStorage.setItem(KEY, path);
  } catch {
    // localStorage unavailable (private mode / disabled) — degrade silently.
  }
}
