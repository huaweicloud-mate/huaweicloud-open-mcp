# Make your first Huawei Cloud call through the MCP gateway

In this tutorial you will connect the `huaweicloud-open-mcp` gateway to your AI client and watch it call Huawei Cloud end to end — first against a mock endpoint that needs no account, then against real Huawei Cloud with your own API key. By the end you will have asked your agent to list your Elastic Cloud Servers and received live JSON back.

The gateway exposes 7 tools in its default mode: `list_products`, `get_product`, `list_apis`, `get_api`, `get_api_examples`, `execute_api`, and `manage_policy`. Your agent uses them in that order to narrow from "all of Huawei Cloud" down to one concrete API call. You never call the cloud directly — you talk to your agent in plain language.

## What you need

- [uv](https://docs.astral.sh/uv/) and Python 3.10+
- git
- An MCP-capable AI client: Claude Code, opencode, or Cursor
- Network access to `apiexplorer.cn-north-4.myhuaweicloud.com` (serves the mock data used in Steps 1–4)
- For Step 5 only: a Huawei Cloud account where you can create an IAM user and an Access Key

Throughout the tutorial, substitute `/path/to/huaweicloud-open-mcp` with the actual directory where you clone the repository in Step 1.

## Step 1 — Install the server

Clone the repository (or copy it) to a directory you control, then sync the Python environment:

```bash
cd /path/to/huaweicloud-open-mcp
uv sync
```

**Verify:** run

```bash
uv run huaweicloud-open-mcp --help
```

You should see usage text listing the flags `--mock`, `--policy`, `--region`, and others. If you see that, the server is installed and runnable.

## Step 2 — Register the server in your AI client (mock mode)

Every client ultimately launches the same command:

```bash
uv --directory /path/to/huaweicloud-open-mcp run huaweicloud-open-mcp \
  --mock \
  --policy /path/to/huaweicloud-open-mcp/configs/safety-policy.example.json
```

Two flags matter here:

- `--mock` — `execute_api` returns simulated data from the API Explorer mock endpoint. No Huawei Cloud account is needed.
- `--policy` — the gateway refuses every `execute_api` call unless a policy file allows it, so a policy is not optional. The shipped example policy allows most ECS operations (read *and* write), read-only VPC operations, IAM listings, and OBS `ListBuckets`, and denies everything else. **Use an absolute path**: clients spawn the server with their own working directory, and a relative path that only worked in your terminal will fail.

Follow the section for your client.

### Claude Code

```bash
claude mcp add huaweicloud -- \
  uv --directory /path/to/huaweicloud-open-mcp run huaweicloud-open-mcp \
  --mock \
  --policy /path/to/huaweicloud-open-mcp/configs/safety-policy.example.json
```

**Verify:** `claude mcp list` shows `huaweicloud` with a connected status.

### opencode

Add the server to `opencode.json` (project) or `~/.config/opencode/opencode.json` (global):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "huaweicloud": {
      "type": "local",
      "command": [
        "uv", "--directory", "/path/to/huaweicloud-open-mcp", "run",
        "huaweicloud-open-mcp",
        "--mock",
        "--policy", "/path/to/huaweicloud-open-mcp/configs/safety-policy.example.json"
      ],
      "enabled": true
    }
  }
}
```

**Verify:** start `opencode` — the seven gateway tools appear in the agent's tool list, prefixed with your server name.

### Cursor

Add the server to `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (project):

```json
{
  "mcpServers": {
    "huaweicloud": {
      "command": "uv",
      "args": [
        "--directory", "/path/to/huaweicloud-open-mcp", "run",
        "huaweicloud-open-mcp",
        "--mock",
        "--policy", "/path/to/huaweicloud-open-mcp/configs/safety-policy.example.json"
      ]
    }
  }
}
```

**Verify:** open Cursor's MCP settings — the `huaweicloud` server is listed with its tools.

## Step 3 — Make your first call

In your client, ask:

> List the available Huawei Cloud products.

Your agent calls `list_products` and reports back the full catalog of Huawei Cloud products — server names like `ECS` and `OBS`, their Chinese display names, categories, and how many APIs each exposes.

**Verify:** the reply contains real product entries (for example `ECS`, Elastic Cloud Server) rather than an error.

## Step 4 — Narrow down and execute an API (mock data)

This is the core pattern of the gateway: each step narrows the scope until one concrete call remains. Run it as a short conversation:

1. Ask: *"Show me the ECS APIs."* → the agent calls `list_apis(product="ECS")` and summarizes the API list, grouped by tag.
2. Ask: *"What parameters does ListServersDetails take?"* → the agent calls `get_api` and reads you the required parameters.
3. Ask: *"List my ECS servers in cn-north-4."* → the agent calls `execute_api` and returns a JSON list of servers.

**Verify:** the final reply is a structured JSON response containing server entries — names, statuses, IDs. In `--mock` mode these are simulated values shaped like the real response, not your actual resources.

Notice what just happened: you asked one natural-language question and the agent selected the product, found the API, read its documentation, filled the parameters, and executed it. That entire loop is the gateway's progressive workflow, and it works the same way for every one of the 200+ products.

## Step 5 — Go real with your own credentials

Everything so far used simulated data. Switching to real Huawei Cloud needs two changes: credentials and dropping `--mock`.

1. In the Huawei Cloud console, create a dedicated IAM sub-user and grant it only the permissions you plan to use (for example, read-only ECS access). Create an Access Key (AK/SK) for that user. A minimal-privilege user keeps the blast radius small — the gateway can only do what this user could do anyway.
2. Update your client registration: remove `--mock` and add the credentials as environment variables.

Claude Code:

```bash
claude mcp remove huaweicloud
claude mcp add huaweicloud \
  --env HUAWEICLOUD_SDK_AK=your-access-key-id \
  --env HUAWEICLOUD_SDK_SK=your-secret-access-key \
  -- \
  uv --directory /path/to/huaweicloud-open-mcp run huaweicloud-open-mcp \
  --policy /path/to/huaweicloud-open-mcp/configs/safety-policy.example.json
```

opencode (add an `environment` block next to `command`):

```json
"environment": {
  "HUAWEICLOUD_SDK_AK": "your-access-key-id",
  "HUAWEICLOUD_SDK_SK": "your-secret-access-key"
}
```

Cursor (add an `env` block next to `args`):

```json
"env": {
  "HUAWEICLOUD_SDK_AK": "your-access-key-id",
  "HUAWEICLOUD_SDK_SK": "your-secret-access-key"
}
```

The project ID is resolved automatically; you can also set `HUAWEICLOUD_SDK_PROJECT_ID` explicitly. Restart your client so it relaunches the server.

3. Ask again: *"List my ECS servers in cn-north-4."*

**Verify:** the JSON now contains your actual instances (or an empty list if the account has none in that region — that is also a success). Requests are signed locally with your SK; the secret itself never leaves your machine.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Client shows "failed to connect" or the server exits immediately | `--policy` path is relative or misspelled — the server fails fast on a bad policy file | Use an absolute path to `configs/safety-policy.example.json` |
| The seven tools never appear in the client | `uv` is not on the client's PATH | Find it with `which uv` and use the absolute path in place of `uv` in the command |
| `execute_api` returns `{"ok": false, "reason": ...}` mentioning policy | The API is not allowed by the policy file (the example file allows only specific ECS/VPC/IAM/OBS operations) | Edit the policy file — changes hot-reload without restarting the server — or, after confirming with the user, have the agent add a rule via `manage_policy` |
| 401 / SignatureDoesNotMatch | Wrong AK or SK | Recheck the environment variables in the client config |
| 403 from Huawei Cloud with a permission error | The IAM user lacks the permission | Grant the minimal IAM policy needed for that API |
| Mock calls hang or time out | No network route to the API Explorer mock endpoint | Check proxy/firewall access to `apiexplorer.cn-north-4.myhuaweicloud.com` |
| Empty server list in Step 5 | No resources in that region | Success — try another `region`, e.g. ask for servers in `cn-east-3` |

For deeper diagnosis, run the server with `--log-level DEBUG --log-file /tmp/hwc-mcp.log` in the registration command and inspect the log file.

## Where to go next

- **How the openapi mode works** — [mcp-openapi.md](mcp-openapi.md) (progressive workflow, signing, presigned OBS URLs, caching) and [architecture.md](architecture.md) (overall design).
- **Safety policy syntax** — the rules format (`product:apiPattern=allow|deny`) and the shipped example: `configs/safety-policy.example.json`.
- **The second mode** — `--mode discover` connects the agent to cloud-hosted Huawei Cloud MCP servers instead of calling OpenAPI directly; see [mcp-discovery.md](mcp-discovery.md) and the README.
