import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/lend_db")

engine = create_async_engine(DATABASE_URL, echo=True)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def init_db():
    async with engine.begin() as conn:
        # This will create tables, but we will use Alembic for production
        # For now, let's include it for local dev
        # SQLModel.metadata.create_all(conn)
        pass

async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session

async def set_tenant_context(session: AsyncSession, customer_id: str):
    """Sets the customer_id in the PostgreSQL session for Row-Level Security"""
    await session.execute(f"SET app.current_customer_id = '{customer_id}'")
