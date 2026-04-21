# Infrastructure and Resilience Guide (Free Tier)

This guide outlines how to deploy the **Lend** platform with high availability and resilience using entirely free services, focused on a US-based deployment.

## 1. Cloud Infrastructure (The Server)
**Provider**: [Google Cloud Platform (GCP) "Always Free" Tier](https://cloud.google.com/free)
- **Region**: US regions (`us-east1`, `us-west1`, or `us-central1`).
- **Resource**: **e2-micro** instance (2 vCPUs, 1 GB RAM).
- **Setup**: Install [k3s](https://k3s.io/) or run as a standalone Docker host for the application.

## 2. Database (The State)
**Provider**: [Neon.tech](https://neon.tech/) or [Supabase](https://supabase.com/)
- **Service**: Managed PostgreSQL.
- **Cost**: Free tier includes 500MB - 1GB storage and automated backups.
- **Resilience**: Managed providers handle underlying hardware failures automatically.

## 3. Containerization
**Dockerfile**: Located at the root of the project.
- **Optimized**: Multi-stage build to reduce image size and security surface.
- **Runtime**: Runs FastAPI via Uvicorn.

## 4. Orchestration (Kubernetes)
**Manifests**: Located in `/k8s`.
- `deployment.yaml`: Configured with 2 replicas for redundancy. If one container crashes, Kubernetes automatically restarts it or routes traffic to the other.
- `service.yaml`: Internal load balancing between replicas.
- `ingress.yaml`: External access with TLS (SSL) support via Let's Encrypt.

## 5. CI/CD (Automation)
**Provider**: GitHub Actions.
- **Workflow**: `.github/workflows/deploy.yml`.
- **Function**: Automatically builds the Docker image on every push to `main` and deploys it to your Kubernetes cluster.
- **Secrets**: You will need to add the following to your GitHub Repository Secrets:
    - `DOCKERHUB_USERNAME`
    - `DOCKERHUB_TOKEN`
    - `KUBECONFIG` (The base64 encoded kubeconfig file from your Oracle Cloud instance)
    - `DATABASE_URL` (From Neon/Supabase)

## 6. Resilience Mechanisms
- **Automated Restarts**: Kubernetes `livenessProbe` and `readinessProbe` ensure the application is healthy.
- **Rolling Updates**: Zero-downtime deployments via the CI/CD pipeline.
- **Local Redundancy**: Multi-replica deployment ensures service continuity during individual container failures.

## 7. Cost Summary
| Component | Provider | Cost |
| :--- | :--- | :--- |
| Compute/K8s | Google Cloud (e2-micro) | $0.00 |
| Database | Neon / Supabase | $0.00 |
| CI/CD | GitHub Actions | $0.00 |
| Registry | Docker Hub | $0.00 |
| SSL/TLS | Let's Encrypt | $0.00 |
| **Total** | | **$0.00** |
