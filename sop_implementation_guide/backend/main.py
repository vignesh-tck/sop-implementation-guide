from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.blocks import router as blocks_router
from backend.api.discovery import router as discovery_router
from backend.api.threads import router as threads_router

app = FastAPI(
    title="SoP Implementation Engine",
    description="Implementation layer for State of Place recommendations — Bowie, MD",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    # localhost and 127.0.0.1 are distinct origins to the browser — allow both so
    # the dev console works whichever one you open it on.
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(blocks_router)
app.include_router(threads_router)
app.include_router(discovery_router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "sop-implementation-engine"}
