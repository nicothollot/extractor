import { ReactNode, createContext, useCallback, useContext, useState } from "react";
import { MultiSelectionResponse, PreflightEstimate } from "./api";

/* One firm row in the Multi-Search flow. Mirrors the backend MultiSearchFirm
   shape (camelCase here; the screen maps to snake_case at the API boundary).
   deals=[] means "all discovered deals for the client". */
export interface FirmEntry {
  client: string;
  deals: string[];
  period: string;
  docTypes: string[];
  llmAssist: boolean;
  enhancedPeriodCheck: boolean;
  dealSearchModel: string;
  addedFolders: string[];
  removedDeals: string[];
}

/* New Run wizard state, lifted above the router so navigating to another tab
   and back keeps the analyst's progress (step, every field, the preflight job
   + estimate, the document-selection edits). It intentionally does NOT persist
   across a full page reload — a fresh load starts a clean run. */

export interface WizardState {
  step: number;
  // search mode — "single" keeps the existing single-firm wizard flow
  // byte-for-behavior unchanged; "multi" is purely additive.
  searchMode: "single" | "multi";
  // scope
  scope: "deal" | "client" | "all";
  client: string;
  deal: string;
  period: string;
  periods: string[]; // multi-period / expanded-range run (empty = the single `period`)
  docType: string;
  docTypes: string[]; // multi doc-type run (empty = the single `docType`)
  restrictClientSourced: boolean; // false = allow HL/non-client sources (rank-only)
  discoveryMode: "browse" | "search" | "llm";
  llmDiscoverModel: string;
  llmDiscoverEffort: string;
  // template
  template: string | null;
  templateInitialized: boolean;
  dryRunOnly: boolean;
  // ai / model
  llmEnabled: boolean;
  mode: "auto" | "manual";
  manualModel: string;
  manualEffort: string;
  budget: string;
  forceLlmAssist: boolean; // use the LLM as the primary extractor (escalate everything)
  aiInitialized: boolean;
  // preflight
  preflightJobId: string | null;
  estimate: PreflightEstimate | null;
  // confirm documents
  removedSlots: string[]; // slot_key values excluded from the launch
  docsConfirmed: boolean;
  // launch / review
  runJobId: string | null;
  runId: string | null;
  // ---- multi-search (additive; only used when searchMode === "multi") ----
  multiFirms: FirmEntry[];
  multiTemplate: string | null;
  multiDryRunOnly: boolean;
  multiConfirmed: boolean;
  multiSelection: MultiSelectionResponse | null; // last preview
  multiRunJobId: string | null;
  multiRunId: string | null;
}

export const initialWizardState: WizardState = {
  step: 0,
  searchMode: "single",
  scope: "deal",
  client: "",
  deal: "",
  period: "",
  periods: [],
  docType: "any_client_valuation_doc",
  docTypes: [],
  restrictClientSourced: true,
  discoveryMode: "browse",
  llmDiscoverModel: "sonnet",
  llmDiscoverEffort: "low",
  template: null,
  templateInitialized: false,
  dryRunOnly: false,
  llmEnabled: true,
  mode: "auto",
  manualModel: "sonnet",
  manualEffort: "low",
  budget: "",
  forceLlmAssist: false,
  aiInitialized: false,
  preflightJobId: null,
  estimate: null,
  removedSlots: [],
  docsConfirmed: false,
  runJobId: null,
  runId: null,
  multiFirms: [],
  multiTemplate: null,
  multiDryRunOnly: false,
  multiConfirmed: false,
  multiSelection: null,
  multiRunJobId: null,
  multiRunId: null,
};

type Patch = Partial<WizardState> | ((prev: WizardState) => Partial<WizardState>);

interface WizardContextValue {
  state: WizardState;
  patch: (p: Patch) => void;
  reset: () => void;
}

const WizardContext = createContext<WizardContextValue | null>(null);

export function WizardProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<WizardState>(initialWizardState);
  const patch = useCallback((p: Patch) => {
    setState((prev) => ({ ...prev, ...(typeof p === "function" ? p(prev) : p) }));
  }, []);
  const reset = useCallback(() => setState(initialWizardState), []);
  return <WizardContext.Provider value={{ state, patch, reset }}>{children}</WizardContext.Provider>;
}

export function useWizard(): WizardContextValue {
  const ctx = useContext(WizardContext);
  if (!ctx) throw new Error("useWizard must be used within a WizardProvider");
  return ctx;
}
