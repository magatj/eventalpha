output "secret_arns" {
  value = { for k, s in aws_secretsmanager_secret.this : k => s.arn }
}
