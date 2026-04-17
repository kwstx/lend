import logging
import json
import sys
import os
from datetime import datetime
from typing import Any, Dict, Optional
from uuid import UUID, uuid4

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from pythonjsonlogger import jsonlogger

from src.models.models import EventLog

# Initialize Sentry
SENTRY_DSN = os.getenv("SENTRY_DSN")
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[
            FastApiIntegration(),
            SqlalchemyIntegration(),
        ],
        traces_sample_rate=1.0,
        profiles_sample_rate=1.0,
        environment=os.getenv("ENV", "development"),
    )

def setup_logging():
    """Configures structured JSON logging for the application."""
    logger = logging.getLogger()
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logger.setLevel(log_level)

    # Console handler with JSON formatting
    handler = logging.StreamHandler(sys.stdout)
    formatter = jsonlogger.JsonFormatter(
        '%(asctime)s %(levelname)s %(name)s %(message)s',
        json_ensure_ascii=False
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    # Disable propagation for some noisy libraries if needed
    logging.getLogger("uvicorn.access").propagate = False

class AuditLogger:
    """
    Utility for recording every action into the events_log table 
    and emitting structured logs.
    """
    
    @staticmethod
    async def log_action(
        session,
        customer_id: UUID,
        event_type: str,
        payload: Dict[str, Any],
        advance_id: Optional[UUID] = None,
        idempotency_key: Optional[str] = None
    ):
        """
        Records an event in the database ledger (EventLog) 
        and sends it to structured logs and Sentry Breadcrumbs.
        """
        if not idempotency_key:
            idempotency_key = f"audit_{uuid4()}"

        # 1. Create DB Record (The source of truth)
        log_entry = EventLog(
            customer_id=customer_id,
            advance_id=advance_id,
            event_type=event_type,
            payload=payload,
            idempotency_key=idempotency_key,
            processing_status="processed", # Audit logs are processed immediately
            created_at=datetime.utcnow()
        )
        session.add(log_entry)
        
        # 2. Structured logging
        logger = logging.getLogger("audit")
        logger.info(f"Audit Action: {event_type}", extra={
            "customer_id": str(customer_id),
            "advance_id": str(advance_id) if advance_id else None,
            "event_type": event_type,
            "payload": payload,
            "idempotency_key": idempotency_key
        })

        # 3. Sentry breadcrumb
        sentry_sdk.add_breadcrumb(
            category="audit",
            message=event_type,
            level="info",
            data={
                "customer_id": str(customer_id),
                "advance_id": str(advance_id) if advance_id else None,
                **payload
            }
        )
        
        # Note: We don't commit here. We assume this is part of a larger transaction.
        return log_entry
