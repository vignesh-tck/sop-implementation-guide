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
    # the dev console and local Vite dev server work whichever one you open.
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
    ],
    # Covers both the production domain and every preview deploy without an edit
    # per deploy — Vercel assigns a new subdomain to each one.
    allow_origin_regex=r"https://.*\.vercel\.app",
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
