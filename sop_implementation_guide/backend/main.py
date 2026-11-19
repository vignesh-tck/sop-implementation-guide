from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.blocks import router as blocks_router
from backend.api.datasets import router as datasets_router
from backend.api.pathways import router as pathways_router
from backend.api.pipelines import router as pipelines_router
from backend.api.plans import router as plans_router
from backend.api.threads import router as threads_router
from backend.api.zoning import router as zoning_router

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
app.include_router(plans_router)
app.include_router(pathways_router)
app.include_router(zoning_router)
app.include_router(datasets_router)
app.include_router(pipelines_router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "sop-implementation-engine"}


@app.get("/")
def root():
    # No real content lives here — this exists so platform health checks
    # (Render pings "/" by default) get a 200 instead of a 404.
    return {"status": "ok", "service": "sop-implementation-engine", "docs": "/docs"}
