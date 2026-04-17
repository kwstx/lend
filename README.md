# Lend Backend Service

Monolithic FastAPI backend for an embedded financial service.

## Tech Stack
- **Framework**: FastAPI (Python 3.10+)
- **Database**: PostgreSQL
- **ORM**: SQLModel (SQLAlchemy + Pydantic)
- **Migrations**: Alembic
- **Multi-tenancy**: PostgreSQL Row-Level Security (RLS)

## Architecture: Event-Driven Financial Ledger
The system is designed as an event-driven ledger. Every state change (e.g., a new receivable, an advance requested, a repayment) is recorded in the `events_log` table.

### Multi-tenant Isolation
We use **Row-Level Security (RLS)** at the database level. 
- Every table (except `customers`) has a `customer_id` column.
- Policies are defined to restrict access based on the `app.current_customer_id` session variable.
- The FastAPI application sets this variable on every request via `src/core/database.set_tenant_context`.

## Setup

1. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Database Setup**:
   - Create a PostgreSQL database named `lend_db`.
   - Update `DATABASE_URL` in `.env`.
   - Run the RLS setup script: `src/core/rls_setup.sql`.

3. **Run the App**:
   ```bash
   python src/main.py
   ```

## API Usage
Endpoints require the `X-Customer-ID` header for tenant isolation.
- `GET /events`: Fetch events for the specified customer.
