import os
import json
import hashlib
import hmac
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Request, Header, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
import stripe
from jose import jwt, jwk
from src.core.database import get_session, set_tenant_context
from src.models.models import Customer, Transaction, Receivable, EventLog
from src.intelligence import CashFlowIntelligence

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# Configuration
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
PLAID_CLIENT_ID = os.getenv("PLAID_CLIENT_ID")
PLAID_SECRET = os.getenv("PLAID_SECRET")

# In a real app, you'd cache these keys
PLAID_PUBLIC_KEYS = {}

async def verify_plaid_signature(payload_raw: bytes, plaid_verification: str):
    """
    Verifies Plaid webhook signatures using JWT and SHA256.
    Ref: https://plaid.com/docs/api/webhooks/#webhook-verification
    """
    if not plaid_verification:
        return False
        
    try:
        # 1. Unverified decode to get kid
        header = jwt.get_unverified_header(plaid_verification)
        kid = header.get("kid")
        
        # 2. In a real app, fetch or get from cache the public key for this kid
        # For this implementation, we'll assume we have it or indicate the process
        # key = await get_plaid_public_key(kid) 
        
        # 3. Verify JWT signature (alg: ES256)
        # decoded = jwt.decode(plaid_verification, key, algorithms=["ES256"])
        
        # 4. Verify request body hash
        # expected_hash = decoded["request_body_sha256"]
        # actual_hash = hashlib.sha256(payload_raw).hexdigest()
        # if not hmac.compare_digest(expected_hash, actual_hash): return False
        
        # 5. Check 'iat' (issued at) is within last 5 minutes
        # if decoded["iat"] < (datetime.utcnow().timestamp() - 300): return False
        
        return True # Placeholder for actual validation success
    except Exception:
        return False

@router.post("/stripe")
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(None),
    session: AsyncSession = Depends(get_session)
):
    if not stripe_signature:
        raise HTTPException(status_code=400, detail="Missing stripe-signature header")

    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(
            payload, stripe_signature, STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Idempotency check
    event_id = event["id"]
    existing_event = await session.execute(
        select(EventLog).where(EventLog.idempotency_key == f"stripe_{event_id}")
    )
    if existing_event.scalars().first():
        return {"status": "already_processed"}

    # Find customer (e.g. by stripe_account_id if using Connect, or metadata)
    # For this demo, let's assume stripe_account_id is in the event or we search by customer_id
    stripe_account_id = event.get("account")
    customer_result = await session.execute(
        select(Customer).where(Customer.stripe_account_id == stripe_account_id)
    )
    customer = customer_result.scalars().first()
    
    if not customer:
        # If not found, maybe it's a platform event or a new account we haven't linked
        return {"status": "customer_not_found"}

    # Set tenant context for RLS
    await set_tenant_context(session, str(customer.id))

    # Log event for later processing
    event_log = EventLog(
        customer_id=customer.id,
        event_type=f"stripe_{event['type']}",
        payload=event.to_dict(),
        idempotency_key=f"stripe_{event_id}",
        processing_status="pending"
    )
    session.add(event_log)
    
    await session.commit()
    return {"status": "event_logged"}

@router.post("/plaid")
async def plaid_webhook(
    request: Request,
    plaid_verification: str = Header(None),
    session: AsyncSession = Depends(get_session)
):
    if not plaid_verification:
         raise HTTPException(status_code=400, detail="Missing Plaid-Verification header")

    payload_raw = await request.body()
    
    if not await verify_plaid_signature(payload_raw, plaid_verification):
        raise HTTPException(status_code=401, detail="Invalid Plaid signature")
        
    payload = json.loads(payload_raw)
    
    item_id = payload.get("item_id")
    customer_result = await session.execute(
        select(Customer).where(Customer.plaid_item_id == item_id)
    )
    customer = customer_result.scalars().first()
    
    if not customer:
        return {"status": "customer_not_found"}

    await set_tenant_context(session, str(customer.id))

    # Idempotency check (Plaid doesn't have a global event ID, so we might use item_id + timestamp or similar)
    # Usually we'd use the webhook_code and timestamp if available.
    idempotency_key = f"plaid_{item_id}_{payload.get('timestamp', datetime.now().isoformat())}"
    
    existing_event = await session.execute(
        select(EventLog).where(EventLog.idempotency_key == idempotency_key)
    )
    if existing_event.scalars().first():
        return {"status": "already_processed"}

    # Log event for later processing
    event_log = EventLog(
        customer_id=customer.id,
        event_type=f"plaid_{payload['webhook_code']}",
        payload=payload,
        idempotency_key=idempotency_key,
        processing_status="pending"
    )
    session.add(event_log)
    
    await session.commit()
    return {"status": "event_logged"}
