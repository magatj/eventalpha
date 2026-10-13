output "schedule_names" {
  value = [for s in aws_scheduler_schedule.job : s.name]
}

output "scheduler_role_arn" {
  value = aws_iam_role.scheduler.arn
}
