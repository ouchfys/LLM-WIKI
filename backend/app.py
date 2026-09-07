from contextlib import asynccontextmanager
import threading

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api import agent_runs, monthly_reads, papers, profile, wiki
from backend.agent_recovery import recover_agent_runs
from backend.deps import get_wiki_embeddings, get_wiki_store
from backend.task_executor import shutdown_task_executor, task_executor_snapshot
from backend.web_frontend import mount_frontend


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
    stop_indexer = threading.Event()

    def search_index_loop() -> None:
        # Embedding is a derived, resumable index. It never blocks page commits
        # or API startup and retries pending units after transient API failures.
        if stop_indexer.wait(5):
            return
        while not stop_indexer.is_set():
            processed = 0
            try:
                embedder = get_wiki_embeddings()
                if embedder is not None:
                    index = get_wiki_store().search_index
                    index.embedder = embedder
                    processed = index.backfill_embeddings(limit=32)
            except Exception as exc:
                print(f"[wiki-indexer] embedding backfill deferred: {exc}")
            stop_indexer.wait(2 if processed else 60)

    index_thread = threading.Thread(
        target=search_index_loop, name="wiki-search-indexer", daemon=True
    )
    index_thread.start()
    try:
        yield
    finally:
        stop_recovery.set()
        stop_indexer.set()
        recovery_thread.join(timeout=2)
        index_thread.join(timeout=2)
        shutdown_task_executor(wait=False)


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
    return {"status": "ok", "agent_tasks": task_executor_snapshot()}


mount_frontend(app)
