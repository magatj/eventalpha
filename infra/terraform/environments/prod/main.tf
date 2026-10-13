# EventAlpha AWS environment. NOTHING here is applied automatically:
#   terraform init -backend-config=backend.hcl && terraform plan
# requires explicit, manual approval before any `terraform apply`.

data "aws_caller_identity" "current" {}

locals {
  name = "eventalpha-${var.environment}"
  tags = merge(var.tags, { Project = "eventalpha", Environment = var.environment, ManagedBy = "terraform" })
}

module "network" {
  source             = "../../modules/network"
  name               = local.name
  cidr_block         = var.vpc_cidr
  az_count           = 2
  enable_nat_gateway = var.enable_nat_gateway
  tags               = local.tags
}

module "ecr" {
  source       = "../../modules/ecr"
  name         = local.name
  repositories = ["backend"]
  force_delete = var.environment != "prod"
  tags         = local.tags
}

module "secrets" {
  source       = "../../modules/secrets"
  name         = local.name
  secret_names = ["massive-api-key", "openai-api-key"]
  tags         = local.tags
}

module "rds" {
  source                = "../../modules/rds"
  name                  = local.name
  subnet_ids            = module.network.private_subnet_ids
  security_group_id     = module.network.db_security_group_id
  instance_class        = var.db_instance_class
  allocated_storage_gb  = var.db_allocated_storage_gb
  multi_az              = var.db_multi_az
  backup_retention_days = var.db_backup_retention_days
  deletion_protection   = var.environment == "prod"
  final_snapshot        = var.environment == "prod"
  tags                  = local.tags
}

module "ecs" {
  source                = "../../modules/ecs"
  name                  = local.name
  region                = var.region
  vpc_id                = module.network.vpc_id
  public_subnet_ids     = module.network.public_subnet_ids
  task_subnet_ids       = module.network.task_subnet_ids
  assign_public_ip      = module.network.tasks_need_public_ip
  alb_security_group_id = module.network.alb_security_group_id
  app_security_group_id = module.network.app_security_group_id
  image_repository_url  = module.ecr.repository_urls["backend"]
  image_tag             = var.image_tag
  api_cpu               = var.api_cpu
  api_memory            = var.api_memory
  worker_cpu            = var.worker_cpu
  worker_memory         = var.worker_memory
  api_desired_count     = var.api_desired_count
  worker_desired_count  = var.worker_desired_count
  use_fargate_spot      = var.use_fargate_spot
  log_retention_days    = var.log_retention_days
  certificate_arn       = var.alb_certificate_arn
  db_host               = module.rds.address
  db_port               = module.rds.port
  db_name               = module.rds.db_name
  db_secret_arn         = module.rds.master_user_secret_arn
  app_secret_arns = {
    MASSIVE_API_KEY = module.secrets.secret_arns["massive-api-key"]
    OPENAI_API_KEY  = module.secrets.secret_arns["openai-api-key"]
  }
  sec_user_agent        = var.sec_user_agent
  openai_model          = var.openai_model
  ai_monthly_budget_usd = var.ai_monthly_budget_usd
  cors_origins          = var.cors_origins
  tags                  = local.tags
}

module "frontend" {
  source              = "../../modules/s3-cloudfront"
  name                = local.name
  bucket_name         = "${local.name}-web-${data.aws_caller_identity.current.account_id}"
  api_origin_domain   = module.ecs.alb_dns_name
  api_origin_https    = var.alb_certificate_arn != null
  aliases             = var.domain_aliases
  acm_certificate_arn = var.cloudfront_certificate_arn
  force_destroy       = var.environment != "prod"
  tags                = local.tags
}

module "scheduler" {
  source              = "../../modules/scheduler"
  name                = local.name
  region              = var.region
  account_id          = data.aws_caller_identity.current.account_id
  cluster_arn         = module.ecs.cluster_arn
  task_definition_arn = module.ecs.job_task_definition_arn
  task_family         = module.ecs.job_task_family
  execution_role_arn  = module.ecs.execution_role_arn
  task_role_arn       = module.ecs.task_role_arn
  subnet_ids          = module.network.task_subnet_ids
  security_group_id   = module.network.app_security_group_id
  assign_public_ip    = module.network.tasks_need_public_ip
  enabled             = var.schedules_enabled
  tags                = local.tags
}

module "monitoring" {
  source                  = "../../modules/monitoring"
  name                    = local.name
  alb_arn_suffix          = module.ecs.alb_arn_suffix
  target_group_arn_suffix = module.ecs.target_group_arn_suffix
  db_instance_id          = module.rds.instance_id
  log_group_name          = module.ecs.log_group_name
  alarm_email             = var.alarm_email
  monthly_budget_usd      = var.aws_monthly_budget_usd
  tags                    = local.tags
}
