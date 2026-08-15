from contextlib import asynccontextmanager
import threading

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api import agent_runs, monthly_reads, papers, profile, wiki
from backend.agent_recovery import recover_agent_runs


@asynccontextmanager
async def lifespan(_: FastAPI):
    recover_agent_runs()
    stop_recovery = threading.Event()

    def recovery_loop() -> None:
        while not stop_recovery.wait(30):
            recover_agent_runs()

    recovery_thread = threading.Thread(
        target=recovery_loop, name="agent-recovery-monitor", daemon=True
    )
    recovery_thread.start()
    try:
        yield
    finally:
        stop_recovery.set()
        recovery_thread.join(timeout=2)


app = FastAPI(
    title="LLM-WIKI API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(monthly_reads.router, prefix="/api/monthly-reads", tags=["monthly-reads"])
app.include_router(papers.router, prefix="/api/papers", tags=["papers"])
app.include_router(wiki.router, prefix="/api/wiki", tags=["wiki"])
app.include_router(agent_runs.router, prefix="/api/agent-runs", tags=["agent-runs"])
app.include_router(profile.router, prefix="/api/profile", tags=["profile"])


@app.get("/api/health")
def health():
    return {"status": "ok"}
