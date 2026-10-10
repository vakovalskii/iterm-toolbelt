# Proxies for Claude Code and Codex

Anthropic and OpenAI refuse API traffic from some regions (403, `unsupported_country_region_territory`),
and some networks cut the connection to their hosts altogether. A small HTTP proxy on a VPS in a supported
country fixes the network part. This page is how we run ours: what to install, how to point the agents at it,
and how to keep it from becoming an open proxy for the whole internet.

A proxy only changes where your traffic leaves from. Your account, billing and the provider's terms still
apply; check that using the API from where you are is allowed for your account.

## 1. The server

Any small VPS in a country the providers support: 1 vCPU and 1 GB RAM are plenty, the traffic of a coding
agent is a few GB a month. Ubuntu 22.04/24.04, Docker installed.

We use [gost](https://github.com/ginuerzh/gost) v2: one static binary, HTTP `CONNECT` proxy with password auth.
The agent's TLS goes through the proxy end to end, the proxy never sees the content.

```bash
# credentials file, one "user password" per line; keep it out of argv (argv is visible in ps and docker inspect)
mkdir -p /opt/gost && umask 077
echo "me $(openssl rand -base64 24 | tr -d '/+=')" > /opt/gost/secrets.txt

docker run -d --name gost-http --restart unless-stopped \
  -p 48921:48921 \
  -v /opt/gost/secrets.txt:/secrets.txt:ro \
  --log-opt max-size=10m --log-opt max-file=3 \
  ginuerzh/gost:2.11.5 -L "http://:48921?secrets=/secrets.txt"
```

- Pin the image version. `latest` can change under you.
- Cap the logs (`max-size`). A busy proxy logs every connection, and an unbounded log has filled a 50 GB disk for us.
- Pick a random high port. It does not stop a scanner, but it keeps the log quiet.

Check from your machine (the password goes through stdin, not the command line):

```bash
printf 'proxy = "http://me:PASSWORD@SERVER:48921"\nurl = "https://api.anthropic.com/v1/models"\n' \
  | curl -s -o /dev/null -w '%{http_code}\n' -K -
```

`401` means the API is reachable and only wants a key: the proxy works. `403` means the provider still
sees a blocked region. No answer means the path to the proxy is dead.

## 2. The agents

Both CLIs honour the standard proxy variables.

**Claude Code**, for one session:

```bash
HTTPS_PROXY=http://me:PASSWORD@SERVER:48921 claude
```

or for every session, in `~/.claude/settings.json`:

```json
{ "env": { "HTTPS_PROXY": "http://me:PASSWORD@SERVER:48921" } }
```

**Codex**, for one session:

```bash
HTTPS_PROXY=http://me:PASSWORD@SERVER:48921 codex
```

Codex signed in with a ChatGPT account talks to `chatgpt.com`, with an API key to `api.openai.com`; the
proxy has to reach whichever one you use.

**With the toolbelt** you do not type any of this. Add the proxy in ⚙ → Network, and every row in
Agent actions gets **⧉ export** (the `export HTTPS_PROXY=…` line on your clipboard), **▶ claude** and
**▶ codex** (a new agent in the current directory through that proxy). ⚙ → Agents sets a default proxy
for new sessions. The network block probes every proxy each minute against the endpoint each CLI really
uses, so you see which route works from where you sit before you start a session.

Keep two or three proxies in different places: when one is down or blocked, switch with one click.

## 3. Keeping it yours

An open proxy gets found within hours and used for abuse that ends up tied to your server's address.
In order of strength:

1. **Password, always.** Never run the proxy without auth. Use a long random password, keep it in the
   secrets file, not in the command line, and rotate it when it leaks (a screenshot, a chat, a log).
2. **Firewall allowlist.** If your own addresses are stable, allow the proxy port only from them:
   ```bash
   ufw default deny incoming
   ufw allow 22/tcp
   ufw allow from YOUR.IP.ADDR.ESS to any port 48921 proto tcp
   ufw enable
   ```
   Docker publishes ports around `ufw` by writing its own iptables rules. Either bind the port to a
   specific address (`-p 10.8.0.1:48921:48921`) or put the allowlist into the `DOCKER-USER` chain.
3. **Only inside a tunnel.** The strongest setup: run WireGuard or AmneziaWG on the same server and publish
   the proxy only on the tunnel address (`-p 10.8.0.1:48921:48921`). From the internet there is nothing to
   connect to. The HTTP proxy password, which plain HTTP proxies send in clear text, never crosses the
   open network. A DPI box sees only the tunnel. AmneziaWG also survives networks that block plain WireGuard.

Also:

- `fail2ban` for SSH, key-only login (`PasswordAuthentication no`, `PermitRootLogin prohibit-password`).
- Server monitoring for disk, memory and the proxy port. A full disk silently kills the proxy.
- If a network starts cutting the direct path to the proxy (timeouts on a port that answers from
  elsewhere), route that one address through a tunnel on your router instead of moving the proxy.

## 4. What the toolbelt does with your proxy

- The proxy list lives only in `~/.config/agentbelt/config.json`, mode 600, never in the repo.
- Probes pass the proxy URL to `curl` through stdin, never through argv.
- Pages never get passwords: the settings page shows masked URLs and keeps the stored one when you save
  an untouched row, ⧉ export puts the line on the clipboard from the service side.
- Commands shown in Agent actions are masked (`user:***@host`).
