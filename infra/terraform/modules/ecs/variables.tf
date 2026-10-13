variable "name" {
  type = string
}

variable "region" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "public_subnet_ids" {
  type = list(string)
}

variable "task_subnet_ids" {
  type = list(string)
}

variable "assign_public_ip" {
  description = "Required when tasks run in public subnets without a NAT gateway (to reach external APIs)."
  type        = bool
}

variable "alb_security_group_id" {
  type = string
}

variable "app_security_group_id" {
  type = string
}

variable "image_repository_url" {
  type = string
}

variable "image_tag" {
  type = string
}

variable "cpu_architecture" {
  type    = string
  default = "X86_64"
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
  type    = number
  default = 1
}

variable "use_fargate_spot" {
  type    = bool
  default = false
}

variable "container_insights" {
  type    = bool
  default = false
}

variable "log_retention_days" {
  type    = number
  default = 14
}

variable "certificate_arn" {
  description = "ACM certificate for HTTPS on the ALB. Null means HTTP only; CloudFront still terminates TLS for users."
  type        = string
  default     = null
}

variable "db_host" {
  type = string
}

variable "db_port" {
  type = number
}

variable "db_name" {
  type = string
}

variable "db_secret_arn" {
  type = string
}

variable "app_secret_arns" {
  description = "Map of container env var name to Secrets Manager ARN."
  type        = map(string)
  default     = {}
}

variable "sec_user_agent" {
  type = string
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

variable "tags" {
  type    = map(string)
  default = {}
}
