import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.analysis import Analysis as AnalysisRow
from app.schemas.analysis import AnalysisResponse, AnalyzeRequest
from app.services.analysis_service import AnalysisService

router = APIRouter(
    prefix="/analyze",
    tags=["Analysis"]
)


# Unauthenticated by design — this is the zero-setup playground path.
@router.post("", response_model=AnalysisResponse)
async def analyze(payload: AnalyzeRequest, db: AsyncSession = Depends(get_db)):
    try:
        return await AnalysisService.analyse(
            db=db,
            log=payload.log,
            dependencies=payload.dependencies,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{analysis_id}", response_model=AnalysisResponse)
async def get_analysis(analysis_id: str, db: AsyncSession = Depends(get_db)):
    try:
        parsed_id = uuid.UUID(analysis_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid analysis id.")

    result = await db.execute(select(AnalysisRow).where(AnalysisRow.id == parsed_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Analysis not found.")

    # Not a cache hit in the repeat-failure sense — this is a direct lookup
    # by id, so from_cache stays False; it's simply reading a stored record.
    return await AnalysisService._row_to_response(db, row, from_cache=False)
