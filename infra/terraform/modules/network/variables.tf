variable "name" {
  type = string
}

variable "cidr_block" {
  type    = string
  default = "10.40.0.0/16"
}

variable "az_count" {
  type    = number
  default = 2
  validation {
    condition     = var.az_count >= 2 && var.az_count <= 3
    error_message = "RDS and ALB need at least two AZs."
  }
}

variable "enable_nat_gateway" {
  description = "Run tasks in private subnets behind a NAT gateway (more isolation, ~$32+/month)."
  type        = bool
  default     = false
}

variable "alb_ingress_cloudfront_only" {
  description = "Restrict ALB ingress to the CloudFront origin-facing prefix list."
  type        = bool
  default     = true
}

variable "tags" {
  type    = map(string)
  default = {}
}
