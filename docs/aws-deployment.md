# AWS deployment guide (not deployed)

Terraform in `infra/terraform` describes the production architecture. **Nothing in this repository creates AWS resources automatically.** The GitLab deploy stage is disabled unless `ENABLE_AWS=true`, and even then it requires a manual click.

## Architecture

```
            Users ──HTTPS──► CloudFront ──/*────► S3 (private, OAC)        React build
                                  └─────/api/*──► ALB (CloudFront-only SG) ─► ECS Fargate "api" service
                                                                             ECS Fargate "worker" service
 EventBridge Scheduler ──RunTask──► ECS Fargate one-shot "job" task (python -m app.worker.run_job <job>)
                                                    │
                     RDS PostgreSQL 17 (private subnets, SSL forced, encrypted, RDS-managed password)
 Secrets Manager: RDS master secret (managed by RDS) + massive-api-key + openai-api-key (values set manually)
 CloudWatch: /ecs/eventalpha-<env> logs, alarms (5xx, unhealthy targets, RDS CPU/storage, job failures) → SNS email
 AWS Budgets: monthly cost alert
```

| Module | Purpose |
|---|---|
| `network` | VPC, 2 AZs, public and private subnets, security groups (DB reachable only from app tasks; ALB reachable only from the CloudFront prefix list), optional NAT |
| `ecr` | Immutable-tag repository with scan-on-push and a lifecycle policy |
| `rds` | PostgreSQL, `manage_master_user_password` (password never in state), SSL forced, encrypted gp3, autoscaling storage |
| `secrets` | Empty secret containers for API keys; values are set out-of-band |
| `ecs` | Cluster, ALB and target group, api/worker services, job task, least-privilege execution role (reads only these secrets), task role with no permissions |
| `s3-cloudfront` | Private bucket with OAC, SPA rewrite function, `/api/*` behavior to the ALB with caching disabled |
| `scheduler` | EventBridge Scheduler schedules (America/New_York) → `ecs:RunTask` limited to the job family on this cluster |
| `monitoring` | SNS topic, CloudWatch alarms, log metric filter for job failures, AWS Budget |

## Estimated monthly cost — `dev` defaults (us-east-1)

These are estimates from public on-demand pricing as I understand it. Prices change, so **verify with the [AWS Pricing Calculator](https://calculator.aws/) before deploying.** Taxes, free-tier credits and data transfer beyond light personal use are excluded.

| Item | Sizing | ≈ USD/month |
|---|---|---|
| ECS Fargate API | 0.25 vCPU / 0.5 GB, 24×7 (Spot by default in dev, roughly 70% cheaper) | 3–9 |
| ECS Fargate worker | 0.5 vCPU / 1 GB, 24×7 (Spot) | 5–18 |
| Scheduled job tasks | ~200 short runs/month | 1–3 |
| Application Load Balancer | 1 ALB, low LCU | 17–20 |
| RDS PostgreSQL | db.t4g.micro single-AZ + 20 GB gp3 + backups | 14–17 |
| Public IPv4 addresses | ~2–3 (tasks with public IPs, no NAT) | 7–11 |
| CloudFront + S3 | light personal traffic | 0–2 |
| Secrets Manager | 3 secrets | ~1.2 |
| CloudWatch logs/alarms | 14-day retention, ~6 alarms | 1–4 |
| ECR | < 1 GB | < 1 |
| **Total (dev)** | | **≈ $45–65** |
| Optional NAT gateway (`enable_nat_gateway = true`) | + hourly + per-GB | + 33–45 |
| `prod` profile (on-demand Fargate, longer retention/backups) | | ≈ $65–90 (+ NAT and/or Multi-AZ RDS ≈ +$15–45) |

Plus Massive (free tier: $0) and OpenAI usage (capped by `AI_MONTHLY_BUDGET_USD`).

**Lowest-cost alternatives:** stop the services when not in use (`api_desired_count = 0`, `worker_desired_count = 0`, `schedules_enabled = false`). You still pay for RDS and the ALB, about $35/month. Running locally costs $0.

## Deployment steps (only after you decide to deploy)

1. **One-time AWS prep (manual):**
   * An S3 bucket for Terraform state (versioning + encryption on).
   * A GitLab OIDC identity provider plus an IAM role trusted by your project (`AWS_ROLE_ARN`), scoped to the resources above.
2. `cd infra/terraform/environments/dev`. Copy `backend.hcl.example` → `backend.hcl` and `terraform.tfvars.example` → `terraform.tfvars`, and fill them in.
3. Run `terraform init -backend-config=backend.hcl`, then `terraform plan`. **Review the plan.**
4. First apply (explicit, by you): `terraform apply`. Push an image first, or set `api_desired_count = 0` until one exists.
5. Set secret values, which are never stored in Terraform:
   ```sh
   aws secretsmanager put-secret-value --secret-id <arn from output secret_arns_to_populate> --secret-string "$MASSIVE_API_KEY"
   ```
6. Build, push and deploy:
   ```sh
   docker build --target prod -t $REPO:$SHA backend && docker push $REPO:$SHA
   terraform apply -var image_tag=$SHA
   (cd frontend && npm ci && npm run build)
   ../../../../scripts/aws-post-deploy.sh dev   # runs Alembic migrations as a one-off task, syncs the frontend, invalidates CloudFront
   ```
7. Or use GitLab CI: set `ENABLE_AWS=true`, `AWS_ROLE_ARN`, `TF_BACKEND_HCL` (file contents) and `SEC_USER_AGENT`, then click **deploy-aws** on the default branch.

## Local vs. AWS differences

| Concern | Local | AWS |
|---|---|---|
| Scheduling | APScheduler service + advisory-lock leader + slot keys | EventBridge Scheduler → one-shot task (`app.worker.run_job`), slot-keyed jobs, NYSE holiday skip |
| DB credentials | `DATABASE_URL` in `.env` | `DB_HOST`/`DB_PORT`/`DB_NAME` env + `DB_USER`/`DB_PASSWORD` injected from the RDS-managed secret; the app assembles the URL with `sslmode=require` |
| API keys | `.env` (git-ignored) | Secrets Manager → ECS `secrets` |
| Frontend | Vite dev server with `/api` proxy | S3 + CloudFront with `/api/*` → ALB (same origin, no CORS needed) |
| Migrations | `migrate` compose service | one-off `job` task running `alembic upgrade head` |
