# Containers for application API keys. Terraform creates the secrets but never their values:
# set them out-of-band so keys stay out of source control and Terraform state, e.g.
#   aws secretsmanager put-secret-value --secret-id <arn> --secret-string "$KEY"
resource "aws_secretsmanager_secret" "this" {
  for_each                = toset(var.secret_names)
  name                    = "${var.name}/${each.key}"
  description             = "EventAlpha ${each.key} (value set manually, not by Terraform)"
  recovery_window_in_days = var.recovery_window_days
  tags                    = var.tags
}
