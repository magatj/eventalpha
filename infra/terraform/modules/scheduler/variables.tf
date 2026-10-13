variable "name" {
  type = string
}

variable "region" {
  type = string
}

variable "account_id" {
  type = string
}

variable "cluster_arn" {
  type = string
}

variable "task_definition_arn" {
  type = string
}

variable "task_family" {
  type = string
}

variable "execution_role_arn" {
  type = string
}

variable "task_role_arn" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
}

variable "security_group_id" {
  type = string
}

variable "assign_public_ip" {
  type = bool
}

variable "enabled" {
  type    = bool
  default = true
}

# job type -> schedule expression (America/New_York). Mirrors app/scheduler/main.py.
# Holiday handling: run_job skips trading-day-only jobs on NYSE holidays.
variable "schedules" {
  type = map(string)
  default = {
    ingest_news       = "rate(60 minutes)"
    refresh_prices    = "cron(45 16 ? * MON-FRI *)"
    refresh_baseline  = "cron(5 17 ? * MON-FRI *)"
    sec_monitor       = "cron(10 7-19/2 ? * MON-FRI *)"
    catalyst_analysis = "cron(30 17 ? * MON-FRI *)"
    discovery         = "cron(0 18 ? * MON-FRI *)"
    forecast_review   = "cron(0 9 ? * SAT *)"
    cache_cleanup     = "cron(15 3 * * ? *)"
  }
}

variable "tags" {
  type    = map(string)
  default = {}
}
