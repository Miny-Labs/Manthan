# Vultr deploy

Manthan runs on a single Vultr Cloud Compute VM. Two supported deploy paths:

## A · One-click bootstrap (cloud-init)

When provisioning a fresh Vultr VM in the console, paste the contents of
`cloud-init.yaml` into the *Startup Script* field. The VM boots, installs
Docker, clones this repo, writes a placeholder `.env` to `/opt/manthan/.env`,
and brings the stack up with `docker compose up -d`.

After the VM is reachable:

```bash
ssh root@<your-vultr-ip>
nano /opt/manthan/.env       # paste VULTR_API_KEY + model selections
docker compose -f /opt/manthan/docker-compose.yml restart
```

Recommended Vultr plan: **2 GB RAM / 1 vCPU regular performance** (Frankfurt
or Amsterdam for EU judges; Toronto / NYC for NA). The hackathon's $200 Vultr
credit covers ~3 months on the smallest plan.

## B · Manual bootstrap (existing VM)

SSH into the VM, then:

```bash
curl -fsSL https://raw.githubusercontent.com/Miny-Labs/Manthan/main/infra/vultr/setup.sh | bash
```

This pulls the same `setup.sh` you'll find next to this README. It is the
exact body that `cloud-init.yaml` runs in the background; running it
manually lets you watch the install in real time.

## C · Coolify (the hackathon resource path)

The Milan AI Week hackathon resources include a tutorial on Coolify, an
open-source Heroku alternative that runs on a Vultr VM as a single
container and gives you a UI for deploys. After installing Coolify on a
Vultr VM:

1. Coolify dashboard → New Project → paste `https://github.com/Miny-Labs/Manthan`
2. Coolify auto-detects `docker-compose.yml`
3. Paste env vars in the Coolify UI (at minimum `VULTR_API_KEY`)
4. Click Deploy. Coolify builds the image, runs the stack, attaches
   Let's Encrypt TLS to your domain.

Zero shell commands on camera — every step is a click.

## Env vars

At minimum, `.env` must contain:

```
VULTR_API_KEY=<from Vultr console → Serverless Inference>
```

All other settings have sensible defaults. See `.env.example` for the full list.

## Networking

The stack listens on `:8000` (FastAPI + embedded React build). Put a Vultr
Cloud Firewall rule in front allowing 80/443 from the world and 22 from
your IP. If you don't terminate TLS at Coolify, run nginx or Caddy on
the VM with `proxy_buffering off` so SSE streaming through to the agent
loop works.
