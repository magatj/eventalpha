variable "name" {
  type = string
}

variable "bucket_name" {
  description = "Globally unique S3 bucket name for the frontend build."
  type        = string
}

variable "api_origin_domain" {
  description = "ALB DNS name that serves /api/*."
  type        = string
}

variable "api_origin_https" {
  description = "Use HTTPS from CloudFront to the ALB (requires an ALB certificate matching the origin domain)."
  type        = bool
  default     = false
}

variable "aliases" {
  type    = list(string)
  default = []
}

variable "acm_certificate_arn" {
  description = "us-east-1 ACM certificate for custom aliases (null = *.cloudfront.net)."
  type        = string
  default     = null
}

variable "price_class" {
  type    = string
  default = "PriceClass_100"
}

variable "force_destroy" {
  type    = bool
  default = false
}

variable "tags" {
  type    = map(string)
  default = {}
}
