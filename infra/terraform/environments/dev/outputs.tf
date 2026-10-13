output "app_url" {
  description = "Frontend + API (CloudFront)."
  value       = "https://${module.frontend.distribution_domain_name}"
}

output "alb_dns_name" {
  value = module.ecs.alb_dns_name
}

output "ecr_backend_repository" {
  value = module.ecr.repository_urls["backend"]
}

output "frontend_bucket" {
  value = module.frontend.bucket_name
}

output "cloudfront_distribution_id" {
  value = module.frontend.distribution_id
}

output "ecs_cluster" {
  value = module.ecs.cluster_name
}

output "job_task_family" {
  value = module.ecs.job_task_family
}

output "task_subnet_ids" {
  value = module.network.task_subnet_ids
}

output "app_security_group_id" {
  value = module.network.app_security_group_id
}

output "secret_arns_to_populate" {
  description = "Set these values manually (aws secretsmanager put-secret-value)."
  value       = module.secrets.secret_arns
}

output "rds_endpoint" {
  value = module.rds.address
}

output "tasks_assign_public_ip" {
  value = module.network.tasks_need_public_ip
}
