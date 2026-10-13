# EventBridge Scheduler replaces the local APScheduler service: each schedule starts a
# one-shot Fargate task running `python -m app.worker.run_job <job_type>`.
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.name}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = var.tags
}

# Least privilege: run only the job task family on this cluster, and pass only its roles.
data "aws_iam_policy_document" "run_task" {
  statement {
    actions   = ["ecs:RunTask"]
    resources = ["arn:aws:ecs:${var.region}:${var.account_id}:task-definition/${var.task_family}:*"]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [var.cluster_arn]
    }
  }
  statement {
    actions   = ["iam:PassRole"]
    resources = [var.execution_role_arn, var.task_role_arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "run_task" {
  name   = "run-eventalpha-jobs"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.run_task.json
}

resource "aws_scheduler_schedule_group" "this" {
  name = var.name
  tags = var.tags
}

resource "aws_scheduler_schedule" "job" {
  for_each   = var.schedules
  name       = each.key
  group_name = aws_scheduler_schedule_group.this.name
  state      = var.enabled ? "ENABLED" : "DISABLED"

  schedule_expression          = each.value
  schedule_expression_timezone = "America/New_York"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = var.cluster_arn
    role_arn = aws_iam_role.scheduler.arn

    ecs_parameters {
      task_definition_arn = var.task_definition_arn
      launch_type         = "FARGATE"
      task_count          = 1
      network_configuration {
        subnets          = var.subnet_ids
        security_groups  = [var.security_group_id]
        assign_public_ip = var.assign_public_ip
      }
    }

    input = jsonencode({
      containerOverrides = [{
        name    = "job"
        command = ["python", "-m", "app.worker.run_job", each.key]
      }]
    })

    retry_policy {
      maximum_retry_attempts       = 1
      maximum_event_age_in_seconds = 3600
    }
  }
}
