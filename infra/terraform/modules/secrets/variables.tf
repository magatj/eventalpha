variable "name" {
  type = string
}

variable "secret_names" {
  type    = list(string)
  default = ["massive-api-key", "openai-api-key"]
}

variable "recovery_window_days" {
  type    = number
  default = 7
}

variable "tags" {
  type    = map(string)
  default = {}
}
