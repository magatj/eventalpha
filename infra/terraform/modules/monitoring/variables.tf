variable "name" {
  type = string
}

variable "alb_arn_suffix" {
  type = string
}

variable "target_group_arn_suffix" {
  type = string
}

variable "db_instance_id" {
  type = string
}

variable "log_group_name" {
  type = string
}

variable "alarm_email" {
  type    = string
  default = null
}

variable "monthly_budget_usd" {
  description = "AWS Budgets cost alert threshold (null disables)."
  type        = number
  default     = 75
}

variable "tags" {
  type    = map(string)
  default = {}
}
