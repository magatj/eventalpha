variable "environment" {
  type    = string
  default = "prod"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "enable_nat_gateway" {
  description = "false = tasks in public subnets with public IPs (cheapest); true = private subnets + NAT (~$32+/mo)."
  type        = bool
  default     = false
}

variable "image_tag" {
  description = "Backend image tag in ECR (set by CI to the commit SHA)."
  type        = string
  default     = "latest"
}

variable "api_cpu" {
  type    = number
  default = 256
}

variable "api_memory" {
  type    = number
  default = 512
}

variable "worker_cpu" {
  type    = number
  default = 512
}

variable "worker_memory" {
  type    = number
  default = 1024
}

variable "api_desired_count" {
  type    = number
  default = 1
}

variable "worker_desired_count" {
  description = "Queue worker for on-demand analyses. Scheduled jobs run as separate one-shot tasks."
  type        = number
  default     = 1
}

variable "use_fargate_spot" {
  type    = bool
  default = false
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "db_allocated_storage_gb" {
  type    = number
  default = 20
}

variable "db_multi_az" {
  type    = bool
  default = false
}

variable "db_backup_retention_days" {
  type    = number
  default = 14
}

variable "alb_certificate_arn" {
  type    = string
  default = null
}

variable "cloudfront_certificate_arn" {
  type    = string
  default = null
}

variable "domain_aliases" {
  type    = list(string)
  default = []
}

variable "sec_user_agent" {
  description = "SEC EDGAR requires 'AppName contact-email'."
  type        = string
}

variable "openai_model" {
  type    = string
  default = "gpt-5-mini"
}

variable "ai_monthly_budget_usd" {
  type    = number
  default = 10
}

variable "cors_origins" {
  type    = list(string)
  default = []
}

variable "schedules_enabled" {
  type    = bool
  default = true
}

variable "alarm_email" {
  type    = string
  default = null
}

variable "aws_monthly_budget_usd" {
  type    = number
  default = 150
}

variable "tags" {
  type    = map(string)
  default = {}
}
