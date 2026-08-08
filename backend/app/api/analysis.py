import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AnalysisNotFound, InvalidAnalysisId
from app.database import get_db
from app.models.analysis import Analysis as AnalysisRow
from app.schemas.analysis import AnalysisResponse, AnalyzeRequest
from app.services.analysis_service import AnalysisService

router = APIRouter(
    prefix="/analyze",
    tags=["Analysis"]
)


# Unauthenticated by design — this is the zero-setup playground path.
# No try/except here on purpose: AnalysisFailed and friends are AppException
# subclasses, converted centrally by app_exception_handler, matching how the
# shared backend's routes are written.
@router.post("", response_model=AnalysisResponse)
async def analyze(payload: AnalyzeRequest, db: AsyncSession = Depends(get_db)):
    return await AnalysisService.analyse(
        db=db,
        log=payload.log,
        dependencies=payload.dependencies,
        file_context=payload.file_context,
    )


@router.get("/{analysis_id}", response_model=AnalysisResponse)
async def get_analysis(analysis_id: str, db: AsyncSession = Depends(get_db)):
    try:
        parsed_id = uuid.UUID(analysis_id)
    except ValueError as exc:
        raise InvalidAnalysisId() from exc

    result = await db.execute(select(AnalysisRow).where(AnalysisRow.id == parsed_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise AnalysisNotFound()

    # Not a cache hit in the repeat-failure sense — this is a direct lookup
    # by id, so from_cache stays False; it's simply reading a stored record.
    return await AnalysisService._row_to_response(db, row, from_cache=False)
