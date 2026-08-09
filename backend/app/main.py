from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import crowd_conditions, live_crowd, low_crowd_routing, network, routing
from app.core.db import connect_pool, disconnect_pool


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: open the DB pool once.
    await connect_pool()
    yield
    # Shutdown: close it cleanly.
    await disconnect_pool()


app = FastAPI(
    title="CityFlow API",
    description="Sensory-aware wayfinding data for Melbourne CBD.",
    version="0.1.0",
    lifespan=lifespan,
)

# Loosen this to the actual Vercel/local frontend origin once known.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(routing.router)
app.include_router(low_crowd_routing.router)
app.include_router(network.router)
app.include_router(crowd_conditions.router)
app.include_router(live_crowd.router)


@app.get("/health", tags=["health"])
def health_check() -> dict:
    return {"status": "ok"}
