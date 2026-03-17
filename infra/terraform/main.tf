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

# ─── R2 Bucket ────────────────────────────────────────────────────────────────

resource "cloudflare_r2_bucket" "exports" {
  account_id = var.cloudflare_account_id
  name       = "bridgeleads-exports"
  location   = "WNAM"  # Western North America
}

# ─── WAF Rules ────────────────────────────────────────────────────────────────

resource "cloudflare_ruleset" "waf" {
  zone_id     = var.cloudflare_zone_id
  name        = "BridgeLeads WAF"
  description = "Custom WAF rules for BridgeLeads API"
  kind        = "zone"
  phase       = "http_request_firewall_custom"

  # Block known bad bots
  rules {
    action      = "block"
    description = "Block bad bots"
    expression  = "(cf.client.bot) and not (cf.verified_bot_category in {\"Search Engine Crawler\" \"Monitoring & Analytics\"})"
    enabled     = true
  }

  # Challenge auth endpoint floods
  rules {
    action      = "managed_challenge"
    description = "Challenge auth floods"
    expression  = "(http.request.uri.path matches \"^/auth/(login|register)\") and (cf.threat_score gt 10)"
    enabled     = true
  }

  # Block SQLi attempts
  rules {
    action      = "block"
    description = "Block SQL injection"
    expression  = "(cf.waf.score.sqli gt 40)"
    enabled     = true
  }

  # Block XSS attempts
  rules {
    action      = "block"
    description = "Block XSS"
    expression  = "(cf.waf.score.xss gt 40)"
    enabled     = true
  }
}

# ─── Rate limiting (Cloudflare layer, complements app-level) ──────────────────

resource "cloudflare_ruleset" "rate_limit" {
  zone_id     = var.cloudflare_zone_id
  name        = "BridgeLeads Rate Limiting"
  description = "Edge rate limits before traffic hits Railway"
  kind        = "zone"
  phase       = "http_ratelimit"

  rules {
    action      = "block"
    description = "Auth endpoint rate limit (20/min per IP)"
    expression  = "(http.request.uri.path matches \"^/auth/(login|register)\")"
    enabled     = true
    ratelimit {
      characteristics     = ["cf.unique_visitor_id"]
      period              = 60
      requests_per_period = 20
      mitigation_timeout  = 300
    }
  }

  rules {
    action      = "block"
    description = "General API rate limit (300/min per IP)"
    expression  = "(http.host eq \"api.bridgeleads.io\")"
    enabled     = true
    ratelimit {
      characteristics     = ["cf.unique_visitor_id"]
      period              = 60
      requests_per_period = 300
      mitigation_timeout  = 60
    }
  }
}

# ─── Outputs ──────────────────────────────────────────────────────────────────

output "r2_bucket_name" {
  value = cloudflare_r2_bucket.exports.name
}

output "api_dns" {
  value = cloudflare_record.api.hostname
}
