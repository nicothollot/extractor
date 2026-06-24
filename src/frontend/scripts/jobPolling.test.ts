import assert from "node:assert/strict";
import test from "node:test";

import {
  JobPollingMachine,
} from "../src/lib/jobPolling.ts";
import type { JobPollingSnapshot, PollingScheduler } from "../src/lib/jobPolling.ts";
import type { JobInfo } from "../src/lib/api.ts";

class FakeClock implements PollingScheduler {
  now = 0;
  delays: number[] = [];
  private nextId = 1;
  private timers = new Map<number, { at: number; fn: () => void }>();

  setTimeout(fn: () => void, ms: number): number {
    const id = this.nextId++;
    this.delays.push(ms);
    this.timers.set(id, { at: this.now + ms, fn });
    return id;
  }

  clearTimeout(handle: unknown): void {
    this.timers.delete(Number(handle));
  }

  tick(ms: number): void {
    this.now += ms;
    const due = [...this.timers.entries()]
      .filter(([, timer]) => timer.at <= this.now)
      .sort((a, b) => a[1].at - b[1].at);
    for (const [id, timer] of due) {
      if (!this.timers.has(id)) continue;
      this.timers.delete(id);
      timer.fn();
    }
  }

  get pending(): number {
    return this.timers.size;
  }
}

const flush = () => new Promise<void>((resolve) => setImmediate(resolve));

const job = (status: string, id = "JOB_1"): JobInfo => ({
  id,
  kind: "run",
  status,
  created_at: "2026-01-01T00:00:00+00:00",
  started_at: null,
  finished_at: null,
  run_id: status === "completed" ? "RUN_1" : null,
  params: {},
  result: null,
  error: status === "failed" ? "safe failure" : null,
  last_seq: 0,
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

test("one failed GET is retried and followed by success", async () => {
  const clock = new FakeClock();
  const snapshots: JobPollingSnapshot[] = [];
  const responses: Array<Promise<JobInfo | null>> = [
    Promise.reject(new Error("network down")),
    Promise.resolve(job("running")),
    Promise.resolve(job("completed")),
  ];
  const machine = new JobPollingMachine(
    () => responses.shift() ?? Promise.resolve(job("completed")),
    (s) => snapshots.push(s),
    { scheduler: clock, intervalMs: 1000, minBackoffMs: 1000, maxBackoffMs: 5000 },
  );

  machine.setJobId("JOB_1");
  await flush();
  assert.equal(snapshots.at(-1)?.lastError, "network down");
  assert.equal(snapshots.at(-1)?.retryCount, 1);
  assert.equal(clock.pending, 1);

  clock.tick(1000);
  await flush();
  assert.equal(snapshots.at(-1)?.job?.status, "running");
  assert.equal(snapshots.at(-1)?.retryCount, 0);
  assert.equal(snapshots.at(-1)?.polling, true);

  clock.tick(1000);
  await flush();
  assert.equal(snapshots.at(-1)?.job?.status, "completed");
  assert.equal(snapshots.at(-1)?.terminal, true);
  assert.equal(snapshots.at(-1)?.polling, false);
});

test("repeated transient failures keep polling with bounded backoff", async () => {
  const clock = new FakeClock();
  const snapshots: JobPollingSnapshot[] = [];
  const machine = new JobPollingMachine(
    () => Promise.reject(new Error("bad gateway")),
    (s) => snapshots.push(s),
    { scheduler: clock, minBackoffMs: 1000, maxBackoffMs: 5000, degradedAfterRetries: 2 },
  );

  machine.setJobId("JOB_1");
  await flush();
  for (const ms of [1000, 2000, 4000, 5000]) {
    clock.tick(ms);
    await flush();
  }

  assert.equal(snapshots.at(-1)?.retryCount, 5);
  assert.equal(snapshots.at(-1)?.degraded, true);
  assert.deepEqual(clock.delays.slice(0, 5), [1000, 2000, 4000, 5000, 5000]);
});

test("changing job id aborts stale work and resets state", async () => {
  const clock = new FakeClock();
  const first = deferred<JobInfo | null>();
  const snapshots: JobPollingSnapshot[] = [];
  const signals: AbortSignal[] = [];
  const machine = new JobPollingMachine(
    (id, signal) => {
      signals.push(signal);
      return id === "JOB_A" ? first.promise : Promise.resolve(job("running", "JOB_B"));
    },
    (s) => snapshots.push(s),
    { scheduler: clock },
  );

  machine.setJobId("JOB_A");
  assert.equal(signals[0].aborted, false);
  machine.setJobId("JOB_B");
  assert.equal(signals[0].aborted, true);
  await flush();
  first.resolve(job("completed", "JOB_A"));
  await flush();

  assert.equal(snapshots.at(-1)?.jobId, "JOB_B");
  assert.equal(snapshots.at(-1)?.job?.id, "JOB_B");
  assert.equal(snapshots.at(-1)?.retryCount, 0);
});

test("unmount stops timers and ignores late responses", async () => {
  const clock = new FakeClock();
  const pending = deferred<JobInfo | null>();
  const snapshots: JobPollingSnapshot[] = [];
  const machine = new JobPollingMachine(
    () => pending.promise,
    (s) => snapshots.push(s),
    { scheduler: clock },
  );

  machine.setJobId("JOB_1");
  machine.stop();
  pending.resolve(job("running"));
  await flush();
  clock.tick(10_000);
  await flush();

  assert.equal(snapshots.at(-1)?.job, null);
  assert.equal(clock.pending, 0);
});

for (const status of ["completed", "failed", "cancelled", "interrupted"]) {
  test(`terminal status ${status} stops polling`, async () => {
    const clock = new FakeClock();
    const snapshots: JobPollingSnapshot[] = [];
    const machine = new JobPollingMachine(
      () => Promise.resolve(job(status)),
      (s) => snapshots.push(s),
      { scheduler: clock },
    );

    machine.setJobId("JOB_1");
    await flush();

    assert.equal(snapshots.at(-1)?.job?.status, status);
    assert.equal(snapshots.at(-1)?.terminal, true);
    assert.equal(snapshots.at(-1)?.terminalFailure, status === "failed");
    assert.equal(snapshots.at(-1)?.polling, false);
    assert.equal(clock.pending, 0);
  });
}

test("manual retry clears transient error and polls immediately", async () => {
  const clock = new FakeClock();
  const snapshots: JobPollingSnapshot[] = [];
  const responses: Array<Promise<JobInfo | null>> = [
    Promise.reject(new Error("temporary")),
    Promise.resolve(job("running")),
  ];
  const machine = new JobPollingMachine(
    () => responses.shift() ?? Promise.resolve(job("running")),
    (s) => snapshots.push(s),
    { scheduler: clock, minBackoffMs: 1000 },
  );

  machine.setJobId("JOB_1");
  await flush();
  assert.equal(snapshots.at(-1)?.lastError, "temporary");

  machine.refresh();
  await flush();

  assert.equal(snapshots.at(-1)?.job?.status, "running");
  assert.equal(snapshots.at(-1)?.lastError, null);
  assert.equal(snapshots.at(-1)?.retryCount, 0);
});

test("404 is exposed as not found and does not loop", async () => {
  const clock = new FakeClock();
  const snapshots: JobPollingSnapshot[] = [];
  const notFound = Object.assign(new Error("unknown job"), { status: 404 });
  const machine = new JobPollingMachine(
    () => Promise.reject(notFound),
    (s) => snapshots.push(s),
    { scheduler: clock },
  );

  machine.setJobId("JOB_MISSING");
  await flush();

  assert.equal(snapshots.at(-1)?.notFound, true);
  assert.equal(snapshots.at(-1)?.errorKind, "not_found");
  assert.equal(snapshots.at(-1)?.polling, false);
  assert.equal(clock.pending, 0);
});
