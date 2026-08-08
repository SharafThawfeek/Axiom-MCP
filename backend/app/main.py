from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.api import health_router, analysis_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"{settings.APP_NAME} starting...")
    yield
    print(f"{settings.APP_NAME} shutting down...")


app = FastAPI(
    title=settings.APP_NAME,
    version="0.1.0",
    lifespan=lifespan,
)

# The playground frontend is browser-based and served from a different
# origin, so without this every request from it fails preflight.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(analysis_router)


@app.get("/")
async def root():
    return {
        "name": settings.APP_NAME,
        "status": "running",
    }
