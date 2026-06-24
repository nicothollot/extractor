import type { JobInfo } from "./api.ts";

export type PollingErrorKind = "not_found" | "transient";

export interface JobPollingSnapshot {
  jobId: string | null;
  job: JobInfo | null;
  initialLoading: boolean;
  polling: boolean;
  lastError: string | null;
  errorKind: PollingErrorKind | null;
  retryCount: number;
  degraded: boolean;
  notFound: boolean;
  terminal: boolean;
  terminalFailure: boolean;
}

export interface JobPollingOptions {
  intervalMs?: number;
  minBackoffMs?: number;
  maxBackoffMs?: number;
  degradedAfterRetries?: number;
  scheduler?: PollingScheduler;
}

export interface PollingScheduler {
  setTimeout: (fn: () => void, ms: number) => unknown;
  clearTimeout: (handle: unknown) => void;
}

export type FetchJob = (jobId: string, signal: AbortSignal) => Promise<JobInfo | null>;

const ACTIVE_STATUSES = new Set(["queued", "running", "cancelling"]);
const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled", "interrupted"]);

const defaultScheduler: PollingScheduler = {
  setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
  clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
};

export function isActiveJobStatus(status: string | null | undefined): boolean {
  return Boolean(status && ACTIVE_STATUSES.has(status));
}

export function isTerminalJobStatus(status: string | null | undefined): boolean {
  return Boolean(status && TERMINAL_STATUSES.has(status));
}

export function initialJobPollingSnapshot(jobId: string | null): JobPollingSnapshot {
  return {
    jobId,
    job: null,
    initialLoading: Boolean(jobId),
    polling: Boolean(jobId),
    lastError: null,
    errorKind: null,
    retryCount: 0,
    degraded: false,
    notFound: false,
    terminal: false,
    terminalFailure: false,
  };
}

export function pollingErrorMessage(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  if (typeof error === "string" && error.trim()) return error;
  return "job status request failed";
}

function isNotFoundError(error: unknown): boolean {
  return typeof error === "object" && error !== null && "status" in error && Number((error as { status: unknown }).status) === 404;
}

function isAbortError(error: unknown): boolean {
  return typeof error === "object" && error !== null && (error as { name?: unknown }).name === "AbortError";
}

export class JobPollingMachine {
  private readonly fetchJob: FetchJob;
  private readonly onChange: (snapshot: JobPollingSnapshot) => void;
  private readonly intervalMs: number;
  private readonly minBackoffMs: number;
  private readonly maxBackoffMs: number;
  private readonly degradedAfterRetries: number;
  private readonly scheduler: PollingScheduler;

  private snapshot: JobPollingSnapshot = initialJobPollingSnapshot(null);
  private timer: unknown = null;
  private controller: AbortController | null = null;
  private stopped = false;
  private attemptToken = 0;

  constructor(fetchJob: FetchJob, onChange: (snapshot: JobPollingSnapshot) => void, options: JobPollingOptions = {}) {
    this.fetchJob = fetchJob;
    this.onChange = onChange;
    this.intervalMs = options.intervalMs ?? 1200;
    this.minBackoffMs = options.minBackoffMs ?? 1000;
    this.maxBackoffMs = options.maxBackoffMs ?? 5000;
    this.degradedAfterRetries = options.degradedAfterRetries ?? 2;
    this.scheduler = options.scheduler ?? defaultScheduler;
  }

  current(): JobPollingSnapshot {
    return this.snapshot;
  }

  setJobId(jobId: string | null): void {
    this.stopLoop();
    this.stopped = false;
    this.snapshot = initialJobPollingSnapshot(jobId);
    this.emit();
    if (jobId) this.pollNow();
  }

  refresh(): void {
    const jobId = this.snapshot.jobId;
    if (!jobId) return;
    this.stopLoop();
    this.stopped = false;
    this.snapshot = {
      ...this.snapshot,
      initialLoading: this.snapshot.job === null,
      polling: true,
      lastError: null,
      errorKind: null,
      retryCount: 0,
      degraded: false,
      notFound: false,
      terminal: false,
      terminalFailure: false,
    };
    this.emit();
    this.pollNow();
  }

  stop(): void {
    this.stopped = true;
    this.stopLoop();
  }

  private stopLoop(): void {
    if (this.timer !== null) {
      this.scheduler.clearTimeout(this.timer);
      this.timer = null;
    }
    if (this.controller) {
      this.controller.abort();
      this.controller = null;
    }
    this.attemptToken += 1;
  }

  private emit(): void {
    this.onChange({ ...this.snapshot });
  }

  private pollNow(): void {
    const jobId = this.snapshot.jobId;
    if (this.stopped || !jobId) return;
    const token = this.attemptToken + 1;
    this.attemptToken = token;
    const controller = new AbortController();
    this.controller = controller;
    this.runAttempt(jobId, token, controller).catch((error: unknown) => {
      if (!isAbortError(error)) {
        // runAttempt handles request failures; this guard keeps a coding error
        // from silently killing the loop.
        this.handleTransient(error);
        this.scheduleNext(this.backoffDelay());
      }
    });
  }

  private async runAttempt(jobId: string, token: number, controller: AbortController): Promise<void> {
    let nextDelay: number | null = null;
    try {
      const job = await this.fetchJob(jobId, controller.signal);
      if (!this.isCurrent(token, controller)) return;
      if (job === null) {
        this.handleTransient(new Error("job status response was empty"));
        nextDelay = this.backoffDelay();
        return;
      }
      const terminal = isTerminalJobStatus(job.status);
      this.snapshot = {
        ...this.snapshot,
        job,
        initialLoading: false,
        polling: !terminal,
        lastError: null,
        errorKind: null,
        retryCount: 0,
        degraded: false,
        notFound: false,
        terminal,
        terminalFailure: job.status === "failed",
      };
      this.emit();
      if (!terminal) nextDelay = this.intervalMs;
    } catch (error: unknown) {
      if (!this.isCurrent(token, controller) || isAbortError(error)) return;
      if (isNotFoundError(error)) {
        this.snapshot = {
          ...this.snapshot,
          initialLoading: false,
          polling: false,
          lastError: pollingErrorMessage(error),
          errorKind: "not_found",
          notFound: true,
          terminal: false,
          terminalFailure: false,
        };
        this.emit();
        return;
      }
      this.handleTransient(error);
      nextDelay = this.backoffDelay();
    } finally {
      if (this.controller === controller) this.controller = null;
      if (this.stopped || !this.isTokenCurrent(token)) return;
      if (nextDelay !== null) this.scheduleNext(nextDelay);
    }
  }

  private isCurrent(token: number, controller: AbortController): boolean {
    return !this.stopped && this.isTokenCurrent(token) && !controller.signal.aborted;
  }

  private isTokenCurrent(token: number): boolean {
    return token === this.attemptToken;
  }

  private handleTransient(error: unknown): void {
    const retryCount = this.snapshot.retryCount + 1;
    this.snapshot = {
      ...this.snapshot,
      initialLoading: this.snapshot.job === null,
      polling: Boolean(this.snapshot.jobId),
      lastError: pollingErrorMessage(error),
      errorKind: "transient",
      retryCount,
      degraded: retryCount >= this.degradedAfterRetries,
      notFound: false,
      terminal: false,
      terminalFailure: false,
    };
    this.emit();
  }

  private backoffDelay(): number {
    const retry = Math.max(1, this.snapshot.retryCount);
    return Math.min(this.maxBackoffMs, this.minBackoffMs * 2 ** (retry - 1));
  }

  private scheduleNext(delayMs: number): void {
    if (this.stopped || !this.snapshot.jobId || this.timer !== null) return;
    this.timer = this.scheduler.setTimeout(() => {
      this.timer = null;
      this.pollNow();
    }, delayMs);
  }
}
