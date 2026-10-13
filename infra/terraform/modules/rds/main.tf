# Private PostgreSQL. The master password is generated and stored by RDS in Secrets Manager
# (manage_master_user_password), so it never appears in Terraform state or variables.
resource "aws_db_subnet_group" "this" {
  name       = "${var.name}-db"
  subnet_ids = var.subnet_ids
  tags       = var.tags
}

resource "aws_db_parameter_group" "this" {
  name   = "${var.name}-pg${var.engine_major_version}"
  family = "postgres${var.engine_major_version}"
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }
  tags = var.tags
}

resource "aws_db_instance" "this" {
  identifier                   = "${var.name}-db"
  engine                       = "postgres"
  engine_version               = var.engine_major_version
  instance_class               = var.instance_class
  allocated_storage            = var.allocated_storage_gb
  max_allocated_storage        = var.max_allocated_storage_gb
  storage_type                 = "gp3"
  storage_encrypted            = true
  db_name                      = var.db_name
  username                     = var.username
  manage_master_user_password  = true
  db_subnet_group_name         = aws_db_subnet_group.this.name
  vpc_security_group_ids       = [var.security_group_id]
  parameter_group_name         = aws_db_parameter_group.this.name
  publicly_accessible          = false
  multi_az                     = var.multi_az
  backup_retention_period      = var.backup_retention_days
  deletion_protection          = var.deletion_protection
  skip_final_snapshot          = !var.final_snapshot
  final_snapshot_identifier    = var.final_snapshot ? "${var.name}-db-final" : null
  auto_minor_version_upgrade   = true
  performance_insights_enabled = var.performance_insights
  copy_tags_to_snapshot        = true
  tags                         = var.tags
}
