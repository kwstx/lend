import uvicorn
from fastapi import FastAPI, Depends, HTTPException, Header
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.database import get_session, set_tenant_context
from src.reconciliation import run_reconciliation_job
from src.services.advance_service import AdvanceService
from src.services.repayment_processor import RepaymentProcessor
from src.models.models import Customer, EventLog, FundingQueue, FinancingOffer, ReconciliationException
from uuid import UUID
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from src.webhooks import router as webhooks_router
from sqlalchemy import select, desc
from pydantic import BaseModel
from fastapi.responses import HTMLResponse
from datetime import datetime

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
    scheduler.add_job(run_reconciliation_job, "interval", hours=6)
    
    # Process repayments every 5 minutes
    async def process_repayments_job():
        from src.core.database import SessionLocal
        async with SessionLocal() as session:
            processor = RepaymentProcessor(session)
            await processor.process_pending_events()

    scheduler.add_job(process_repayments_job, "interval", minutes=5)
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

# --- Admin Interface ---

@app.get("/admin/funding-queue", response_class=HTMLResponse)
async def admin_dashboard(session: AsyncSession = Depends(get_session)):
    """
    Simulated Admin Dashboard for operations to review funding requests.
    In a real app, this would be protected by admin-only auth.
    """
    stmt = (
        select(FundingQueue, Customer, FinancingOffer)
        .join(Customer, FundingQueue.customer_id == Customer.id)
        .join(FinancingOffer, FundingQueue.offer_id == FinancingOffer.id)
        .filter(FundingQueue.status == "staged_for_approval")
        .order_by(FundingQueue.created_at.desc())
    )
    result = await session.execute(stmt)
    items = result.all()

    rows = ""
    for queue, customer, offer in items:
        rows += f"""
        <tr style="border-bottom: 1px solid #eee;">
            <td style="padding: 12px;">{customer.name}</td>
            <td style="padding: 12px;">${offer.amount:,.2f}</td>
            <td style="padding: 12px;">{queue.created_at.strftime('%Y-%m-%d %H:%M')}</td>
            <td style="padding: 12px;">
                <button onclick="approve('{queue.id}')" style="background:#2ecc71; color:white; border:none; padding:6-12px; border-radius:4px; cursor:pointer;">Approve</button>
                <button onclick="reject('{queue.id}')" style="background:#e74c3c; color:white; border:none; padding:6-12px; border-radius:4px; cursor:pointer; margin-left:8px;">Reject</button>
            </td>
        </tr>
        """

    html_content = f"""
    <html>
        <head>
            <title>Lend | Operations Dashboard</title>
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background-color: #f8f9fa; color: #333; }}
                .container {{ max-width: 1000px; margin: 40px auto; background: white; padding: 30px; border-radius: 12px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); }}
                h1 {{ color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 10px; }}
                table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
                th {{ text-align: left; background: #f1f3f5; padding: 12px; font-weight: 600; }}
            </style>
            <script>
                async function approve(id) {{
                     const notes = prompt("Reviewer Notes (Optional)");
                     const res = await fetch(`/admin/funding/${{id}}/approve?notes=${{encodeURIComponent(notes || '')}}`, {{ method: 'POST' }});
                     if (res.ok) {{ alert('Capital deployed successfully!'); location.reload(); }}
                     else alert('Error approving request');
                }}
                async function reject(id) {{
                     const reason = prompt("Reason for rejection?");
                     if (!reason) return;
                     const res = await fetch(`/admin/funding/${{id}}/reject?reason=${{encodeURIComponent(reason)}}`, {{ method: 'POST' }});
                     if (res.ok) {{ alert('Request rejected.'); location.reload(); }}
                     else alert('Error rejecting request');
                }}
            </script>
        </head>
        <body>
            <div class="container">
                <h1>Funding Approval Queue (HITL)</h1>
                <p>Reviewexposure and finalize capital deployment decisions.</p>
                <table>
                    <thead>
                        <tr>
                            <th>Customer</th>
                            <th>Amount</th>
                            <th>Requested At</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows or '<tr><td colspan="4" style="text-align:center; padding:20px; color:#999;">No pending approvals</td></tr>'}
                    </tbody>
                </table>
            </div>
        </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@app.post("/admin/funding/{queue_id}/approve")
async def approve_funding_request(
    queue_id: UUID, 
    notes: str = "",
    session: AsyncSession = Depends(get_session)
):
    service = AdvanceService(session)
    # Using 'admin_ops' as hardcoded reviewer for now
    advance = await service.approve_funding(queue_id, reviewer_id="admin_ops", notes=notes)
    return advance

@app.post("/admin/funding/{queue_id}/reject")
async def reject_funding_request(
    queue_id: UUID, 
    reason: str,
    session: AsyncSession = Depends(get_session)
):
    service = AdvanceService(session)
    result = await service.reject_funding(queue_id, reviewer_id="admin_ops", reason=reason)
    return result

@app.post("/admin/repayments/process-now")
async def trigger_repayment_processing(
    session: AsyncSession = Depends(get_session)
):
    """Manual trigger for the controlled event processor."""
    processor = RepaymentProcessor(session)
    count = await processor.process_pending_events()
    return {"status": "success", "processed_events": count}

@app.get("/admin/exceptions", response_class=HTMLResponse)
async def exceptions_dashboard(session: AsyncSession = Depends(get_session)):
    """
    Simulated Admin Dashboard for viewing and resolving reconciliation exceptions.
    """
    stmt = (
        select(ReconciliationException, Customer)
        .join(Customer, ReconciliationException.customer_id == Customer.id)
        .where(ReconciliationException.resolution_status == "unresolved")
        .order_by(ReconciliationException.created_at.desc())
    )
    result = await session.execute(stmt)
    items = result.all()

    rows = ""
    for exc, customer in items:
        color = "#e74c3c" if exc.severity == "critical" else "#f39c12"
        rows += f"""
        <tr style="border-bottom: 1px solid #eee;">
            <td style="padding: 12px;"><span style="color: {color}; font-weight: bold;">{exc.severity.upper()}</span></td>
            <td style="padding: 12px;">{customer.name}</td>
            <td style="padding: 12px;">{exc.exception_type}</td>
            <td style="padding: 12px; font-size: 0.85em; max-width: 300px; overflow: hidden; text-overflow: ellipsis;">{exc.notes}</td>
            <td style="padding: 12px;">{exc.created_at.strftime('%Y-%m-%d %H:%M')}</td>
            <td style="padding: 12px;">
                <button onclick="resolve('{exc.id}')" style="background:#2ecc71; color:white; border:none; padding:6px 12px; border-radius:4px; cursor:pointer;">Resolve</button>
            </td>
        </tr>
        """

    html_content = f"""
    <html>
        <head>
            <title>Lend | Exceptions Dashboard</title>
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background-color: #f8f9fa; color: #333; }}
                .container {{ max-width: 1100px; margin: 40px auto; background: white; padding: 30px; border-radius: 12px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); }}
                h1 {{ color: #2c3e50; border-bottom: 2px solid #e74c3c; padding-bottom: 10px; }}
                table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
                th {{ text-align: left; background: #f1f3f5; padding: 12px; font-weight: 600; }}
                .nav {{ margin-bottom: 20px; }}
                .nav a {{ margin-right: 15px; color: #3498db; text-decoration: none; font-weight: bold; }}
            </style>
            <script>
                async function resolve(id) {{
                     const notes = prompt("Resolution Notes?");
                     if (!notes) return;
                     const res = await fetch(`/admin/exceptions/${{id}}/resolve?notes=${{encodeURIComponent(notes)}}`, {{ method: 'POST' }});
                     if (res.ok) {{ alert('Exception resolved.'); location.reload(); }}
                     else alert('Error resolving exception');
                }}
            </script>
        </head>
        <body>
            <div class="container">
                <div class="nav">
                    <a href="/admin/funding-queue">Funding Queue</a>
                    <a href="/admin/exceptions" style="color: #333;">Exceptions</a>
                </div>
                <h1>Reconciliation Exceptions</h1>
                <p>Verify and manually correct ledger mismatches detected by the automated system.</p>
                <table>
                    <thead>
                        <tr>
                            <th>Severity</th>
                            <th>Customer</th>
                            <th>Type</th>
                            <th>Description</th>
                            <th>Detected At</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows or '<tr><td colspan="6" style="text-align:center; padding:20px; color:#999;">No unresolved exceptions</td></tr>'}
                    </tbody>
                </table>
                <div style="margin-top: 30px; text-align: right;">
                    <button onclick="runRecon()" style="background:#3498db; color:white; border:none; padding:10px 20px; border-radius:4px; cursor:pointer;">Run Reconciliation Now</button>
                </div>
                <script>
                async function runRecon() {{
                    const res = await fetch('/admin/reconciliation/run', {{ method: 'POST' }});
                    if (res.ok) {{ 
                        const data = await res.json();
                        alert(`Reconciliation complete. Found ${{data.exceptions_found}} exceptions.`); 
                        location.reload(); 
                    }}
                    else alert('Error running reconciliation');
                }}
                </script>
            </div>
        </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@app.post("/admin/exceptions/{exc_id}/resolve")
async def resolve_exception(
    exc_id: UUID, 
    notes: str,
    session: AsyncSession = Depends(get_session)
):
    stmt = select(ReconciliationException).where(ReconciliationException.id == exc_id)
    result = await session.execute(stmt)
    exc = result.scalars().first()
    if not exc:
        raise HTTPException(status_code=404, detail="Exception not found")
    
    exc.resolution_status = "resolved"
    exc.resolved_at = datetime.utcnow()
    exc.resolved_by = "admin_ops"
    exc.notes = notes
    await session.commit()
    return {"status": "resolved"}

@app.post("/admin/reconciliation/run")
async def trigger_reconciliation(
    session: AsyncSession = Depends(get_session)
):
    """Manual trigger for the full reconciliation service."""
    from src.services.reconciliation_service import ReconciliationService
    service = ReconciliationService(session)
    count = await service.run_full_reconciliation()
    return {"status": "success", "exceptions_found": count}

if __name__ == "__main__":
    uvicorn.run("src.main:app", host="0.0.0.0", port=8000, reload=True)
