variable "name" {
  type = string
}

variable "repositories" {
  type    = list(string)
  default = ["backend"]
}

variable "keep_images" {
  type    = number
  default = 15
}

variable "force_delete" {
  type    = bool
  default = false
}

variable "tags" {
  type    = map(string)
  default = {}
}
