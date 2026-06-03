from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api import monthly_reads, papers, profile, wiki


app = FastAPI(
    title="Personal Research Agent API",
    version="0.1.0",
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
app.include_router(profile.router, prefix="/api/profile", tags=["profile"])


@app.get("/api/health")
def health():
    return {"status": "ok"}
