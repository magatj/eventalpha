terraform {
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.68"
    }
  }

  # Remote state in S3 with native lockfile locking and encryption.
  # Configure with: terraform init -backend-config=backend.hcl  (see backend.hcl.example)
  backend "s3" {}
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = "eventalpha" }
  }
}
