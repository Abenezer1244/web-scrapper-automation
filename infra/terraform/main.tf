terraform {
  required_version = ">= 1.6"
  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.0"
    }
  }
  backend "remote" {
    organization = "bridgeleads"
    workspaces {
      name = "bridgeleads-infra"
    }
  }
}

provider "cloudflare" {
  api_token = var.cloudflare_api_token
}

# ─── Variables ────────────────────────────────────────────────────────────────

variable "cloudflare_api_token" {
  description = "Cloudflare API token with DNS + R2 + WAF permissions"
  type        = string
  sensitive   = true
}

variable "cloudflare_zone_id" {
  description = "Cloudflare zone ID for bridgeleads.io"
  type        = string
}

variable "cloudflare_account_id" {
  description = "Cloudflare account ID"
  type        = string
}

variable "railway_api_ip" {
  description = "Railway API service IP or hostname"
  type        = string
}

variable "vercel_ip" {
  description = "Vercel frontend deployment IP or hostname"
  type        = string
}

# ─── DNS Records ──────────────────────────────────────────────────────────────

resource "cloudflare_record" "api" {
  zone_id = var.cloudflare_zone_id
  name    = "api"
  type    = "A"
  value   = var.railway_api_ip
  proxied = true
  ttl     = 1  # Auto when proxied
}

resource "cloudflare_record" "app" {
  zone_id = var.cloudflare_zone_id
  name    = "app"
  type    = "CNAME"
  value   = "cname.vercel-dns.com"
  proxied = false  # Vercel needs direct DNS
  ttl     = 300
}

resource "cloudflare_record" "root_redirect" {
  zone_id = var.cloudflare_zone_id
  name    = "@"
  type    = "CNAME"
  value   = "cname.vercel-dns.com"
  proxied = false
  ttl     = 300
}

# ─── Outputs ──────────────────────────────────────────────────────────────────

output "api_dns" {
  value = cloudflare_record.api.hostname
}
