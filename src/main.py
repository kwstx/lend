import uvicorn
from fastapi import FastAPI, Depends, HTTPException, Header
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.database import get_session, set_tenant_context
from src.models.models import Customer, EventLog
from uuid import UUID
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from src.webhooks import router as webhooks_router
from src.reconciliation import reconcile_stripe_data
from src.services.advance_service import AdvanceService
from pydantic import BaseModel

app = FastAPI(title="Lend - Embedded Financial Service")

async def get_current_customer_session(
    session: AsyncSession = Depends(get_session),
    x_customer_id: UUID = Header(..., description="The Customer ID for multi-tenant isolation")
) -> AsyncSession:
    """
    Dependency that ensures the tenant context is set for every request.
    In a real app, this would be verified via JWT/Auth.
    """
    await set_tenant_context(session, str(x_customer_id))
    return session

@app.get("/")
async def root():
    return {"message": "Lend API is running", "version": "0.1.0"}

@app.get("/health")
async def health_check():
    return {"status": "healthy"}

@app.on_event("startup")
async def startup_event():
    scheduler = AsyncIOScheduler()
    # Run reconciliation every 6 hours
    scheduler.add_job(reconcile_stripe_data, "interval", hours=6)
    scheduler.start()

app.include_router(webhooks_router)

# Example endpoint showing tenant isolation
@app.get("/events")
async def get_events(
    session: AsyncSession = Depends(get_current_customer_session)
):
    """
    Returns events for the current customer only, enforced by RLS.
    The query itself doesn't need a WHERE customer_id = ... because RLS handles it.
    """
    result = await session.execute(select(EventLog))
    events = result.scalars().all()
    return events

class FinancingRequest(BaseModel):
    amount: float

class OfferAcceptance(BaseModel):
    offer_id: UUID

@app.post("/financing/request")
async def request_financing(
    request: FinancingRequest,
    session: AsyncSession = Depends(get_current_customer_session),
    x_customer_id: UUID = Header(..., description="The Customer ID")
):
    """
    Evaluates risk engine and generates an offer object.
    """
    service = AdvanceService(session)
    offer = await service.create_financing_offer(x_customer_id, request.amount)
    return offer

@app.post("/financing/accept")
async def accept_financing(
    acceptance: OfferAcceptance,
    session: AsyncSession = Depends(get_current_customer_session),
    x_customer_id: UUID = Header(..., description="The Customer ID")
):
    """
    Moves the request into a funding_queue, staged for approval.
    """
    service = AdvanceService(session)
    queue_entry = await service.accept_financing_offer(x_customer_id, acceptance.offer_id)
    return queue_entry

if __name__ == "__main__":
    uvicorn.run("src.main:app", host="0.0.0.0", port=8000, reload=True)
