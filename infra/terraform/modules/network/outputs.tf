output "vpc_id" {
  value = aws_vpc.this.id
}

output "public_subnet_ids" {
  value = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

# Where Fargate tasks run: private subnets with NAT, otherwise public subnets with a public IP.
output "task_subnet_ids" {
  value = var.enable_nat_gateway ? aws_subnet.private[*].id : aws_subnet.public[*].id
}

output "tasks_need_public_ip" {
  value = !var.enable_nat_gateway
}

output "alb_security_group_id" {
  value = aws_security_group.alb.id
}

output "app_security_group_id" {
  value = aws_security_group.app.id
}

output "db_security_group_id" {
  value = aws_security_group.db.id
}
