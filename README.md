# Lend Financial Service Platform

Lend is a high-integrity, multi-tenant embedded financial service platform designed to provide dynamic credit lines based on real-time cash flow analysis. The system integrates with business data sources (invoices, sales, receivables) to evaluate risk and deploy capital with automated repayment and reconciliation.

## Core Capabilities

- **Real-Time Cash Flow Intelligence**: Deterministic SQL-based computation of rolling revenue, stability, and concentration risk.
- **Dynamic Risk Engine**: Versioned underwriting policies with automated offer generation and global exposure governors.
- **Human-in-the-Loop (HITL) Approvals**: Integrated administrative dashboard for manual review and approval of capital deployments.
- **Automated Repayment & Collection**: Logic-driven repayment processor that settles outstanding balances as revenue events occur.
- **Continuous Reconciliation**: Automated scheduled jobs to verify internal ledger states against external financial records (Stripe, Plaid).
- **Global Control System**: Centralized kill switches for underwriting, fund deployment, and repayment processing.
- **Multi-Tenant Security**: Database-level isolation using PostgreSQL Row-Level Security (RLS).

## System Architecture

### 1. Immutable Event Ledger
The system follows an event-driven architecture where every significant state change is recorded in an immutable `events_log`. This ledger serves as the single source of truth for auditing and reconstructing the lifecycle of any advance.

### 2. Multi-Tenancy (RLS)
Security is enforced at the database layer. All queries are restricted by PostgreSQL Row-Level Security based on the `current_customer_id` context set during the FastAPI request lifecycle.

### 3. Financial Lifecycle
1. **Ingestion**: Normalization of incoming events from Stripe, Plaid, and other receivable sources.
2. **Snapshotting**: Generation of versioned `cash_flow_snapshots` used for underwriting decisions.
3. **Offering**: Evaluation of risk policies to generate specific financing offers.
4. **Staging**: Accepted offers enter the `funding_queue` for operational review.
5. **Deployment**: Approved funds are reserved and disbursed through a capital source abstraction layer.
6. **Repayment**: Automatic deduction from incoming revenue events until the obligation is satisfied.

## Tech Stack

- **Framework**: FastAPI (Python 3.10+)
- **Database**: PostgreSQL (with Row-Level Security)
- **ORM/Modeling**: SQLModel (SQLAlchemy + Pydantic)
- **Migrations**: Alembic
- **External Integrations**: Stripe, Plaid
- **Observability**: Sentry for error tracking, structured JSON logging
- **Scheduling**: APScheduler for background reconciliation and repayment jobs

## Project Structure

- `src/core/`: Foundation logic (Database, Security, RLS Setup, Rate Limiting).
- `src/models/`: SQLModel definitions for Customers, Events, Offers, and System Configurations.
- `src/services/`: Core business logic (AdvanceService, RepaymentProcessor, ReconciliationService).
- `src/intelligence.py`: Cash-flow metric computation and snapshotting.
- `src/risk_engine.py`: Underwriting policy enforcement and global risk limits.
- `src/webhooks.py`: Secure intake and normalization for external data providers.
- `src/main.py`: API entry point and background task initialization.
- `src/simulation_harness.py`: Environment for validating workflows without external side effects.

## Setup and Installation

### Prerequisites
- Python 3.10 or higher
- PostgreSQL instance

### Installation
1. Clone the repository and install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Configure environmental variables in a `.env` file:
   - `DATABASE_URL`
   - `STRIPE_SECRET_KEY`
   - `PLAID_CLIENT_ID` (and other provider keys)
   - `SECRET_KEY` (for JWT administration)

3. Initialize the database schema and Row-Level Security:
   ```bash
   alembic upgrade head
   # Apply RLS policies
   psql -d lend_db -f src/core/rls_setup.sql
   ```

### Running the Application
To start the FastAPI server with auto-reload:
```bash
python src/main.py
```

## Security and Operational Controls

### Kill Switches
The platform includes the following global controls (manageable via the Admin UI):
- **Simulation Mode**: Toggles between isolated sandbox data and production-grade pilot data.
- **Underwriting Frozen**: Suspends all new financing requests.
- **Deployment Paused**: Halts the disbursement of approved capital.
- **Repayments Paused**: Stops the automated processing of repayment events.

### Authentication
- **Tenants**: Authenticated via per-customer API Keys passed in the `X-Customer-ID` or `Authorization` header.
- **Administrators**: Authenticated via JWT with role-based access (Operations, Admin).

## Support and Monitoring
Structured logs are generated for every request and background job. Reconciliation exceptions are surfaced in the Exceptions Dashboard for immediate operational resolution.
