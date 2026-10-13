## Summary

<!-- What changed and why -->

## Checklist

- [ ] `.\scripts\eventalpha.ps1 test` passes (backend lint/tests, frontend lint/typecheck/tests)
- [ ] No API keys, `.env` files or Terraform state committed
- [ ] New financial values are labeled as observed data, external estimates, or model forecasts
- [ ] Database changes include an Alembic migration; forecast tables stay append-only
- [ ] No `terraform apply` or AWS deployment was run without explicit approval
