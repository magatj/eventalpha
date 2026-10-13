#!/bin/sh
# Post-deploy steps (run from infra/terraform/environments/<env> after terraform apply):
#   1. run Alembic migrations as a one-off Fargate task and wait for it
#   2. sync the frontend build to S3 and invalidate CloudFront
set -eu
ENV="${1:-dev}"
out() { terraform output -raw "$1"; }

CLUSTER=$(out ecs_cluster)
FAMILY=$(out job_task_family)
SUBNETS=$(terraform output -json task_subnet_ids | tr -d '[]" \n')
SG=$(out app_security_group_id)
PUBLIC_IP=$( [ "$(out tasks_assign_public_ip)" = "true" ] && echo ENABLED || echo DISABLED )

echo "Running database migrations on $CLUSTER ($ENV)…"
TASK_ARN=$(aws ecs run-task --cluster "$CLUSTER" --task-definition "$FAMILY" --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=$PUBLIC_IP}" \
  --overrides '{"containerOverrides":[{"name":"job","command":["alembic","upgrade","head"]}]}' \
  --query 'tasks[0].taskArn' --output text)
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK_ARN"
CODE=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ARN" --query 'tasks[0].containers[0].exitCode' --output text)
[ "$CODE" = "0" ] || { echo "migration task failed (exit $CODE)"; exit 1; }

echo "Publishing frontend…"
aws s3 sync ../../../../frontend/dist "s3://$(out frontend_bucket)" --delete
aws cloudfront create-invalidation --distribution-id "$(out cloudfront_distribution_id)" --paths '/*' >/dev/null
echo "Done: $(out app_url)"
