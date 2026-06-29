"""Phase-4 local GUI backend: FastAPI on 127.0.0.1 wrapping the SAME
functions the CLI calls (run.run, locator.locate, system checks, the
writer's Phase-4 entry points). No pipeline logic lives here — only job
orchestration, serialization and the evidence renderer."""
