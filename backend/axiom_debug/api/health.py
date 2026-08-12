from fastapi import APIRouter
from sqlalchemy import text

from axiom_debug.database import engine

router = APIRouter()


async def database_health() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@router.get("/health")
async def health():
    return {
        "status": "healthy",
        "services": {
            "database": await database_health(),
        },
    }
