from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="CityFlow API",
    description="Sensory-aware wayfinding data for Melbourne CBD.",
    version="0.1.0",
)

# Loosen this to the actual Vercel/local frontend origin once known.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["health"])
def health_check() -> dict:
    return {"status": "ok"}
