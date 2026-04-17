import asyncio
import logging
from src.core.database import async_session
from src.services.reconciliation_service import ReconciliationService

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("reconciliation_job")

async def run_reconciliation_job():
    """
    Continuous reconciliation runner.
    In production, this would be triggered by a cron job or a task scheduler (e.g. Celery, ARQ).
    """
    logger.info("Starting mandatory reconciliation cycle...")
    async with async_session() as session:
        service = ReconciliationService(session)
        try:
            exceptions_found = await service.run_full_reconciliation()
            logger.info(f"Reconciliation cycle completed. Total exceptions recorded: {exceptions_found}")
        except Exception as e:
            logger.error(f"Reconciliation job failed: {str(e)}", exc_info=True)

if __name__ == "__main__":
    asyncio.run(run_reconciliation_job())
