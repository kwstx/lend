import uvicorn
from fastapi import FastAPI, Depends, HTTPException, Header
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.database import get_session, set_tenant_context
from src.models.models import Customer, EventLog
from sqlmodel import select
from uuid import UUID

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

if __name__ == "__main__":
    uvicorn.run("src.main:app", host="0.0.0.0", port=8000, reload=True)
