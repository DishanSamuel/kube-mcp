# kube-log-mcp — Zero-Trust Kubernetes Observability Gateway

A Slack-driven ChatOps interface for Kubernetes troubleshooting. An AI agent (Anthropic Claude) uses a compiled Go MCP gateway to query deployment status, pod logs, and Prometheus metrics — then posts synthesized analysis directly in Slack threads.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Kubernetes Pod                                                 │
│                                                                 │
│  ┌──────────────┐    stdio (JSON-RPC)    ┌──────────────────┐  │
│  │ Slack Agent   │◄─────────────────────►│  Go MCP Gateway   │  │
│  │ (Python)      │                       │  (static binary)  │  │
│  │               │                       │                   │  │
│  │ slack-bolt    │                       │  K8s client-go ──►│──┼──► K8s API
│  │ anthropic SDK │                       │  Prom client   ──►│──┼──► Prometheus
│  └──────┬───────┘                       └──────────────────┘  │
│         │                                                       │
└─────────┼───────────────────────────────────────────────────────┘
          │ WebSocket (Socket Mode)
          ▼
     Slack API ◄──► Users in #incidents
```

**Key design decisions:**
- **Socket Mode** — no public ingress, no webhook URL. The pod connects outbound only.
- **stdio JSON-RPC** — the Go binary never touches the network directly; all tool calls are piped through stdin/stdout by the Python agent.
- **Zero-trust NetworkPolicy** — ingress fully blocked, egress scoped to DNS, HTTPS (443), and Prometheus (9090).

## Prerequisites

- Go 1.22+ (for building the gateway)
- Python 3.11+ (for the Slack agent)
- A [Slack App](https://api.slack.com/apps) with Socket Mode enabled
- An [Anthropic API key](https://console.anthropic.com/)
- (For cluster deployment) A Kubernetes cluster with Prometheus

### Slack App Setup

1. Create a new Slack App at https://api.slack.com/apps
2. Enable **Socket Mode** under Settings → Socket Mode. Create an App-Level Token with `connections:write` scope — this is your `SLACK_APP_TOKEN` (starts with `xapp-`).
3. Add the following **Bot Token Scopes** under OAuth & Permissions:
   - `app_mentions:read`
   - `chat:write`
   - `reactions:write`
   - `channels:history` / `groups:history` / `im:history`
4. Enable **Event Subscriptions** and subscribe to:
   - `app_mention`
   - `message.channels` (if using incident channel auto-response)
5. Install the app to your workspace. Copy the **Bot User OAuth Token** — this is your `SLACK_BOT_TOKEN` (starts with `xoxb-`).

## Local Development

### 1. Build the Go Gateway

```bash
go build -o bin/gateway ./cmd/gateway
```

### 2. Install Python Dependencies

```bash
cd agent
pip install -r requirements.txt
```

### 3. Configure Environment

```bash
cp .env.example .env
# Edit .env with your actual tokens
```

### 4. Run the Agent

```bash
# From the repo root:
python -m agent
```

> **Note:** The Go gateway uses `rest.InClusterConfig()` by default. When running locally outside a cluster, it will log a warning that the Kubernetes client failed to initialize. Tool calls that hit the K8s API will return errors, but the agent will still run and relay those errors to Slack. To test with a real cluster, you could modify `internal/kubernetes/client.go` to fall back to `~/.kube/config`.

### 5. Test the MCP Gateway Standalone

Use the [MCP Inspector](https://github.com/modelcontextprotocol/inspector) to test the gateway independently:

```bash
npx @modelcontextprotocol/inspector ./bin/gateway
```

## Kubernetes Deployment

### 1. Build & Push Images

```bash
# Go gateway image (used by initContainer)
docker build -t your-registry/kube-log-mcp:latest .
docker push your-registry/kube-log-mcp:latest

# Python agent image
docker build -t your-registry/kube-log-mcp-agent:latest ./agent
docker push your-registry/kube-log-mcp-agent:latest
```

### 2. Create Secrets

```bash
cp deploy/secrets.template.yaml deploy/secrets.yaml
# Edit deploy/secrets.yaml with your base64-encoded tokens
kubectl apply -f deploy/secrets.yaml
```

### 3. Deploy

```bash
kubectl apply -f deploy/rbac.yaml
kubectl apply -f deploy/networkpolicy.yaml
kubectl apply -f deploy/deployment.yaml
```

### 4. Update Image References

In `deploy/deployment.yaml`, replace `your-registry/kube-log-mcp:latest` and `your-registry/kube-log-mcp-agent:latest` with your actual image tags.

## Project Structure

```
.
├── .env.example                 # Environment variable template (copy to .env)
├── .gitignore
├── cmd/gateway/main.go          # Go MCP gateway entrypoint
├── internal/
│   ├── kubernetes/client.go     # K8s client (deployments, pod logs, events)
│   ├── mcp/server.go            # MCP tool registration and handlers
│   └── prometheus/client.go     # Prometheus metric queries
├── agent/
│   ├── main.py                  # Slack bot (slack-bolt + Socket Mode)
│   ├── llm.py                   # Anthropic Claude tool-use loop
│   ├── mcp_client.py            # MCP JSON-RPC client over stdio
│   ├── requirements.txt         # Python dependencies
│   └── Dockerfile               # Agent container image
├── deploy/
│   ├── deployment.yaml          # K8s Deployment (init + agent containers)
│   ├── rbac.yaml                # ServiceAccount, Role, RoleBinding
│   ├── networkpolicy.yaml       # Zero-trust network policy
│   └── secrets.template.yaml    # Secret template with token docs
├── Dockerfile                   # Go gateway container (multi-stage, scratch)
└── README.md
```

## MCP Tools

| Tool | Description |
|---|---|
| `get_deployment_status` | Replica counts, readiness, and recent events for a deployment |
| `search_pod_logs` | Tailed, capped log lines from a pod matched by prefix |
| `get_process_metrics` | CPU or memory metrics via safe, templated PromQL queries |

## Usage in Slack

Mention the bot in any channel it's been invited to:

```
@kube-bot Why is payment-service throwing 500s in prod?
```

The bot will:
1. React with 👀 to acknowledge
2. Post "🔍 Investigating..." in the thread
3. Query deployment status, pod logs, and/or metrics through the MCP gateway
4. Feed the data to Claude for analysis
5. Reply with a structured diagnosis including root cause, evidence, and remediation
# kube-mcp
