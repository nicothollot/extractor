import { Component, ErrorInfo, ReactNode } from "react";

/* Screen-level error boundary. Without one, ANY render exception in a screen
   unmounts the whole React tree and the window goes blank. This catches it,
   keeps the nav shell alive (the boundary wraps only the routed content), and
   shows the actual error so it can be reported + fixed — switching tabs or
   reloading recovers. */

interface Props {
  children: ReactNode;
}
interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Surface to the console so it shows in dev tools and can be copied.
    // eslint-disable-next-line no-console
    console.error("Screen render error:", error, info.componentStack);
  }

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="max-w-2xl space-y-3">
        <div className="bg-paper border border-err/40 rounded-[var(--hl-radius)] p-5 space-y-3">
          <p className="text-[15px] font-semibold text-err">This screen hit an error</p>
          <p className="text-[13px] text-ink-700">
            The rest of the app is fine — switch tabs to keep working, or reload. If this keeps
            happening, copy the details below so it can be fixed.
          </p>
          <pre className="text-[12px] bg-surface border border-line rounded-[var(--hl-radius)] p-3 max-h-48 overflow-auto whitespace-pre-wrap text-ink-700">
            {error.message || String(error)}
          </pre>
          <div className="flex gap-2">
            <button
              type="button"
              className="px-3 py-1.5 text-[13px] font-medium rounded-[var(--hl-radius)] bg-[var(--hl-blue)] text-white"
              onClick={() => this.setState({ error: null })}
            >
              Try again
            </button>
            <button
              type="button"
              className="px-3 py-1.5 text-[13px] font-medium rounded-[var(--hl-radius)] border border-line-strong text-ink-800 hover:bg-surface"
              onClick={() => window.location.reload()}
            >
              Reload app
            </button>
          </div>
        </div>
      </div>
    );
  }
}
