# TESP on SURF Research Cloud

This repository holds the Ansible playbook for a **GA4GH Task Execution Service (TES)** component on [SURF Research Cloud](https://portal.live.surfresearchcloud.nl/) (SRC). It turns a single workspace into a TES endpoint: you submit containerised tasks over HTTPS, and they run as SLURM jobs on the workspace.

It is built on [TESP-API](https://github.com/CESNET/tesp-api) from CESNET, which uses [Pulsar](https://pulsar.readthedocs.io/) as its execution backend. The component deploys the [mirelaminkova/tesp-api](https://github.com/mirelaminkova/tesp-api) fork, which carries TES and S3 fixes.

## How it works

```
TES client (curl, Nextflow, ...)
   │  HTTPS + HTTP Basic auth, or an LS Login access token (optional)
   ▼
nginx (host)            /tes/  →  127.0.0.1:8080
   ▼
gateway (Docker)        answers /v1/service-info itself; with tokens on, asks the
   │                    validator (Docker) and swaps a valid token for tesp-api's Basic credentials
   ▼
tesp-api (Docker)       ──►  MongoDB (Docker, task state); no host port
   ▼
pulsar-proxy (Docker)   adds Pulsar's private token to each request
   ▼
Pulsar (host, :8913)    not exposed through nginx; firewalled to loopback + Docker bridges
   ▼
SLURM + DRMAA (host)    each task runs in an Apptainer container
```

What the playbook sets up:

| Part | Where it is configured |
| --- | --- |
| SLURM controller + worker on the same node, DRMAA, chrony | `playbook.yml` (first play), `group_vars/all.yml` (`slurm_*`, `tesp_slurm_*`; see [Resource limits](#resource-limits)) |
| Pulsar with the `queued_drmaa` manager | `group_vars/all.yml` (`pulsar_*`) |
| Docker, Apptainer, nginx reverse proxy with SRAM login | `playbook.yml`, `group_vars/all.yml` (`nginx_reverse_proxy_locations`) |
| TESP-API stack: gateway, tesp-api, MongoDB, pulsar-proxy | `templates/tesp-api/`, `group_vars/all.yml` (`tesp_*`; see [Service info](#service-info)) |
| Welcome page with your endpoint and credentials | `templates/welcome.html` |
| Host firewall rule for Pulsar's port | `templates/pulsar-firewall.*.j2`, `playbook.yml`, `group_vars/all.yml` (`pulsar_port`) |
| Network fence for TES tasks, and Pulsar's uwsgi socket | `templates/task-fence.*.j2`, `templates/pulsar-uwsgi-socket.conf.j2`, `playbook.yml`, `group_vars/all.yml` (`tesp_task_fence_loopback_tcp_ports`, `tesp_pulsar_uwsgi_socket`, `docker_users`); see [Task network fence](#task-network-fence) |

Secrets are generated at deploy time: a random Pulsar token (only used between `pulsar-proxy` and Pulsar) and a random password for the TES endpoint. The Pulsar token is generated once and kept in `/etc/tesp-api/pulsar_token` (root, 0600), so re-running the playbook keeps it.

## Using the catalog item

### 1. Before you start

1. Make sure you have an SSH key in [your SRAM profile](https://sram.surf.nl/profile). You only need it for troubleshooting on the node.
2. Note your **username** from that page. It usually looks like `ABBBBBB###`: first initial, last name and possibly a number.

### 2. Create a workspace

1. Log in to [SURF Research Cloud](https://portal.live.surfresearchcloud.nl/).

   ![screenshot of SRC login portal with a bright yellow login button](images/surf1.png)

2. You need to be in a collaborative organisation (CO) with a wallet. The **Workspaces** tab in the lower half of the dashboard is the part you need.

   ![SRC dashboard, options like create new workspace, storage, or request a new wallet are available. three workspaces are listed in various states of running.](images/surf2.png)

3. Click the **plus button** on the right of the **Workspaces** tab to add a workspace, and choose the TESP catalog item.

   ![A small plus button is hovered over which says Add. Below, a workspace is shown running with a yellow Access button](images/surf3.png)

4. Pick a size. Choose it for the tasks you plan to run: everything runs on this one node.

   ![workspace creation screen, choose the cloud provider is locked to SURF HPC, flavour is locked to 22.04, and the size options are available from 1GB/8CPU to 60C/750GB Hi-mem](images/surf5.png)

5. Give it a name and check the **expiration date**. SRC deletes the workspace on that date, so choose just enough time for your work. Click **Submit**.

   ![final details page asking for name and description, with a yellow submit button at the bottom](images/surf6.png)

6. Wait for the workspace to be created. This usually takes 5–10 minutes.

   ![workspace list showing a workspace being created.](images/surf7.png)

> [!TIP]
> To keep job data on an SRC storage volume, attach one when creating the workspace and set the component parameter `src_pulsar_storage_path` to its mount path. Without it, job data lives in `/mnt/pulsar` on the workspace disk. To use S3 storage for task inputs and outputs, see [Using S3](#using-s3). Either way, the job store is readable only by root and the Pulsar service user (`0750`; see **Pulsar token** under Configuration notes): get task results through TES or S3.

### 3. Get your endpoint

When the workspace is running, click **Access**. After an SRAM login you see the welcome page for your node, with:

- the **TES base URL**: `https://<workspace-fqdn>/tes`
- the **username** (`tes`) and the generated **password**
- copy fields for the parameters of a [nextflow-tesp-src](https://github.com/mirelaminkova/nextflow-tesp-src) workspace (see [Using S3](#using-s3))
- ready-to-copy `curl` commands and a Nextflow config snippet for other setups

Only members of your CO can open this page. It shows the TES credentials, but not the Pulsar token or other deploy-time secrets. Anyone with the username and password can submit tasks, so don't share them. A workspace can also accept LS Login access tokens; see [Access with LS Login](#access-with-ls-login).

### 4. Run a task

Check the service (service info needs no credentials):

```bash
curl https://<workspace-fqdn>/tes/v1/service-info
```

Submit a task. Every executor must name a container image:

```bash
curl -u 'tes:<password>' -H 'Content-Type: application/json' \
  -d '{"name": "hello", "executors": [{"image": "alpine:3.20", "command": ["echo", "hello from TES"]}]}' \
  https://<workspace-fqdn>/tes/v1/tasks
```

Follow it with the returned id:

```bash
curl -u 'tes:<password>' 'https://<workspace-fqdn>/tes/v1/tasks/<id>?view=FULL'
```

The interactive API docs (Swagger UI) are at `https://<workspace-fqdn>/tes/swagger-ui.html`.

## Using S3

Tasks can name inputs and outputs as `s3://bucket/key`, so a Nextflow client can use an `s3://bucket/work` work directory. TESP reads and writes them on S3 storage chosen through component parameters, for example [SURF Object Store](https://www.surf.nl/en/services/surf-object-store) or MinIO. tesp-api signs every transfer itself with short-lived presigned URLs, so the secret key is not passed to Pulsar jobs or task containers; on the workspace it is readable only by root.

The catalog item can set these component parameters. All are optional:

| Parameter | Default | Secret | Meaning |
| --- | --- | --- | --- |
| `src_s3_endpoint_url` | empty (AWS S3) | no | S3 endpoint: `https://` (or `http://`), host, optional port and path |
| `src_s3_region` | `us-east-1` | no | Region to sign requests for. S3-compatible stores that don't use regions accept the default |
| `src_s3_access_key_id` | empty | **yes** | Access key |
| `src_s3_secret_access_key` | empty | **yes** | Secret key |
| `src_s3_addressing_style` | `path` | no | `path` (`https://host/bucket/key`, works with MinIO and most S3-compatible stores), `virtual` (`https://bucket.host/key`) or `auto` |
| `src_s3_bucket` | empty | no | Bucket shared with the [nextflow-tesp-src](https://github.com/mirelaminkova/nextflow-tesp-src) workspace, as `bucket`, `s3://bucket` or `s3://bucket/prefix`. The welcome page shows `s3://<bucket>/<prefix>` (or `s3://<bucket>/work` without a prefix) as a copy field for that workspace's `src_nf_workdir`, and uses it as `workDir` in its Nextflow example. tesp-api doesn't restrict tasks to it: the key pair decides which buckets tasks can reach |

- **Who sets what:** the catalog item maintainer adds the parameters to the catalog item. Make the endpoint, region and addressing style overwritable if workspace owners should choose them when they create a workspace.
- **Secrets:** give the access key and secret key from SRC's secret store (see the SRC documentation on secrets in component parameters), never as plain fixed values, which anyone who can see the catalog item can read. Every workspace created with the same secret shares that key pair, so use a key that only grants access to the buckets meant for TESP.
- **Both or neither:** without a key pair, TESP accesses S3 anonymously, so only public buckets can be read and `s3://` outputs fail when the task uploads them.
- **Validation:** the playbook stops before installing anything if a parameter is invalid, for example an endpoint without `https://`, an unknown addressing style, an invalid bucket name, or only one of the two keys.

Example for SURF Object Store, with the S3 endpoint and key pair of your Object Store project:

```
src_s3_endpoint_url      = <Object Store S3 endpoint, e.g. https://objectstore.surf.nl>
src_s3_addressing_style  = path
src_s3_access_key_id     = <access key>   (secret)
src_s3_secret_access_key = <secret key>   (secret)
src_s3_bucket            = <bucket name>
```

To run Nextflow against TESP, create a [nextflow-tesp-src](https://github.com/mirelaminkova/nextflow-tesp-src) workspace (its README, "Connecting to TESP"). The TESP welcome page has a copy button for each value it needs:

| nextflow-tesp-src parameter | Form label | Welcome page field |
| --- | --- | --- |
| `src_tes_endpoint` | Paste TESP URL | TES URL |
| `src_tes_username` | | TES username |
| `src_tes_password` (secret) | Write the TES password | TES password |
| `src_nf_workdir` | S3 bucket resource | Nextflow work directory: `s3://<bucket>/<prefix>` from `src_s3_bucket`, or `s3://<bucket>/work` without a prefix |
| `src_s3_endpoint_url`, `src_s3_region`, `src_s3_addressing_style` | | the same values as TESP's |

Give that workspace its own S3 key pair for the bucket (`src_s3_access_key_id`, `src_s3_secret_access_key`); the TESP welcome page never shows TESP's.

The endpoint must be reachable from two places: from the workspace (tesp-api, and Pulsar staging inputs and outputs), and from wherever Nextflow runs, because Nextflow itself reads and writes the work directory. nextflow-tesp-src writes the Nextflow configuration from its parameters. For a Nextflow setup of your own, use the same endpoint and its own credentials, for example:

```groovy
workDir = 's3://<bucket>/work'

aws {
    accessKey = '<access key>'
    secretKey = '<secret key>'
    region = 'us-east-1'           // same as src_s3_region
    client {
        endpoint = '<S3 endpoint>'
        s3PathStyleAccess = true   // matches src_s3_addressing_style = path
    }
}
```

Nextflow has no `auto` addressing style: without `s3PathStyleAccess = true` it uses virtual-host addressing, which MinIO and many S3-compatible stores don't serve. Use `path` on both sides unless the store needs `virtual`.

## Service info

`GET /tes/v1/service-info` is public and describes this workspace for clients and for the [ELIXIR Cloud Registry](https://github.com/elixir-cloud-aai/cloud-registry). tesp-api's own answer is hard-coded for another provider: it names Masaryk University and says `environment: dev`. So the `gateway` container (`tesp-gateway`) answers it instead, from `/etc/tesp-gateway/service-info.json`. The gateway passes the other requests to tesp-api unchanged, with one exception: a request to the TES API (`/tes/v1/...`) without Basic credentials gets `401` from the gateway, where tesp-api would answer `500`. Service info and the API docs (Swagger UI at `/tes/swagger-ui.html`, ReDoc at `/tes/redoc`, `/tes/openapi.json`) stay public.

These optional component parameters set it; blank means the default:

| Parameter | Default | Field |
| --- | --- | --- |
| `src_tes_service_name` | `TESP on SURF Research Cloud` | `name` |
| `src_tes_organization_name` | `SURF` | `organization.name` |
| `src_tes_organization_url` | `https://www.surf.nl/` | `organization.url` (https only) |
| `src_tes_environment` | `test` | `environment`: `test` or `prod`, what the registry accepts |
| `src_tes_contact_url` | this repository's issues | `contactUrl` (`https://` or `mailto:`) |

The other fields come from `tesp_service_info_*` in `group_vars/all.yml`: `id` is the workspace's domain name reversed, plus `.tes` (for example `nl.surf-hosted.src.<workspace>.tes`); the playbook stops if SRC hasn't set the domain name (`workspace_fqdn`). `type` is `org.ga4gh` / `tes` / `1.0.0`, the TES version tesp-api reports. `version` is `tesp-api-<commit>`. `createdAt` is the time of the first deploy, which later deploys keep. `updatedAt` is left out, so a redeploy that changes nothing leaves the file as it is. The playbook stops before installing anything if a value is invalid.

**Registering at the ELIXIR Cloud Registry.** The registry reads only public service info and accepts only `https` URLs. Each deployment sets the environments it accepts; its `GET /service-info` lists them in `registrationRules`. ELIXIR On-Cloud registers `test` and `prod` services, so set `src_tes_environment` to one of those, and set the organization to whoever runs the workspace. Then log in to the registry (LS Login) and import the service info, in its web front end or with the API:

```bash
curl -X POST "https://<registry>/ga4gh/registry/v1/services/import" \
  -H "Authorization: Bearer <your LS Login access token>" -H "Content-Type: application/json" \
  -d '{"serviceInfoUrl": "https://<workspace-fqdn>/tes/v1/service-info"}'
```

The import returns the values for a registration and stores nothing. Check them, then register them with `POST /services`. Since a workspace's name ends up in its domain name and `id`, register a workspace that stays.

## Access with LS Login

A workspace can accept [LS Login](https://lifescience-ri.eu/ls-login/) access tokens besides, or instead of, the Basic password. That way a portal such as Krini calls the TES with the user's own token, without any shared password; so can command-line users with a token of an allowed client. proTES can call it with its own service token. Tokens are off by default.

The TES API is then open to the internet for anyone with an accepted token: there is no SRAM in front of `/tes/`. With tokens on, **Basic is off unless you name the addresses that may use it** (`src_tes_basic_auth_sources`), for the co-located Nextflow component of the same CO. A workspace registered at the ELIXIR Cloud Registry runs token-only: every TES call except service info needs an LS Login token ([generic-issues#108](https://github.com/elixir-cloud-aai/generic-issues/issues/108)). The welcome page stays behind SRAM, because it shows the Basic password.

The gateway takes the client's address from `X-Real-IP`, which the host nginx sets to the address it was connected from. It trusts that header only from Docker's address ranges (`tesp_gateway_trusted_proxies`), where the host's connections to the gateway arrive. The Nextflow workspace's address is the one it reaches this workspace's public name from.

With tokens on, the gateway asks a small validator container (`files/tes-validator/`) about every request except service info and Swagger UI. The validator accepts a token only if all of these hold:

- the token's `iss` is exactly a configured issuer. The discovery document, keys (JWKS) and userinfo endpoint come from the configuration, never from the token;
- it is signed with an allowed algorithm (`RS256` by default; never `none` or `HS*`), by a key the issuer publishes;
- its header says `typ: at+jwt`, so it is an access token and not an ID token (RFC 9068);
- `exp` and `nbf` hold, with 60 s leeway by default (`tesp_oauth_leeway_seconds`, at most 300);
- its `client_id` is one of the allowed clients;
- `aud` is one of the configured audiences, if any are configured; an exempt client's token must also have one of `exempt_audiences`;
- LS Login's userinfo response for the token lists a required entitlement in `eduperson_entitlement`: an exact value, or one starting with a configured prefix (which ends with `:`, so `…:ECP_CLN:` matches the subgroups of `ECP_CLN` but not `…:ECP_CLN_OTHER`). Exempt clients skip this check: a service such as proTES, which checks the user itself.

Everything else gets `401`, except a valid token of a user without a required entitlement, which gets `403` (logging in again won't help). If userinfo has no `eduperson_entitlement` at all, the request also gets `403`, and the validator logs a warning once an hour per client: the client probably doesn't request the scope that releases it. If LS Login can't be reached (or rate-limits) and the token can't be checked, the answer is `503` with `Retry-After`, and the validator logs `unavailable: …`. For a valid token the gateway replaces the client's `Authorization` header with tesp-api's own Basic credentials, so tesp-api stays as it is. Basic credentials from an allowed address are passed to tesp-api unchanged, which checks them. The validator caches keys and userinfo answers (by a hash of the token, never the token; 5 minutes by default, `tesp_oauth_userinfo_cache_seconds`, and never past the token's expiry). Every call to LS Login has a timeout (10 s by default, `tesp_oauth_http_timeout_seconds`, at most 20). The validator asks userinfo with `Accept: */*`: LS Login's userinfo answered requests that accept only `application/json` about 6 s late, and `Accept: */*` in 0.3 s, with the same JSON. Discovery and JWKS still ask for `application/json`. When LS Login can't be reached, known keys stay in use, and a failed fetch is retried at most once a minute. The validator never logs tokens.

**Audit of service clients.** When an exempt client such as proTES submits a task, the user it acts for is named only in the task's `tags` ([proTES#254](https://github.com/elixir-cloud-aai/proTES/issues/254), [generic-issues#108](https://github.com/elixir-cloud-aai/generic-issues/issues/108)). The gateway sends a copy of every task submission to the validator. For exempt clients only, the validator logs the client and the tags listed in `tesp_oauth_audit_tag_keys`, and nothing else of the task:

```
audit: task from exempt client <proTES client id> for <tag>=<user>
```

**Known gap:** which tags proTES sets is not decided yet, so the list is empty by default and the end user behind proTES is **not** logged. The log then names only the tags such tasks carry, without their values. Set the list once the tags are agreed in proTES#254.

> [!WARNING]
> **No per-user task isolation.** tesp-api sees every request as its one Basic user. Everyone who is let in can list, read and cancel every task on the workspace, including other users'. That is fine for a demonstrator; don't put secrets in tasks.

Other limits to know about:

- **Basic bypasses the entitlement check.** Every CO member sees the Basic password on the welcome page. From the addresses in `src_tes_basic_auth_sources`, the entitlement check limits only token users. Leave the parameter empty where only token users should get in.
- **Audience.** Without `audiences`, any LS Login access token of an allowed client is accepted, including one that the client obtained for another service, which could replay it here. `src_tes_oauth_audiences` could close this, but only once every allowed client's tokens are known to carry one common `aud` value naming this TES; otherwise it locks out Krini users (see the parameter table). Until then, leave it empty. Exempt clients need `src_tes_oauth_exempt_audiences`: a value naming this TES, which only their tokens must carry. Without it, a proTES service token for another backend would work here too, without any user check. A client id is refused there, because every token of that client carries it. Without an agreed audience, configure no exempt client.
- **Rate limit.** The TES API (not service info or the docs) takes 20 requests per second per client address (`tesp_gateway_rate_per_second`). Faster requests are queued, up to 40 (`tesp_gateway_rate_burst`, at least 2); beyond that they get `429` with `Retry-After`. A queued request waits up to about burst / 2 / rate seconds (1 s by default); a large burst such as 400 makes that 10 s, which reaches Nextflow's HTTP timeout. Each address may also have 32 requests in progress at once (`tesp_gateway_conn_per_address`, below 64; a request counts until tesp-api answers). The validator checks at most 64 tokens at a time; the per-address cap stays below that, so one address can't take every check. If LS Login or tesp-api answers slowly, a client sending many requests at once can reach this cap and get `429`; a single Nextflow run, which sends one request at a time, stays below it. **A 429 fails a Nextflow task, or aborts the whole run**: nf-ga4gh doesn't retry it. So the addresses in `src_tes_basic_auth_sources` and `src_tes_rate_exempt_sources` are never limited. **Put the co-located Nextflow workspace's address in one of them**; without that (Basic open to everyone, the default with tokens off) it is limited like any client. proTES's address, which submits for all its users, belongs in `src_tes_rate_exempt_sources`. Keep both lists narrow: a wide CIDR (an institution's /16) is unlimited as a whole, and, like the Basic sources, an exempt address can be claimed through `X-Real-IP` by processes on the workspace (see "Who can claim an address"). An IPv6 client gets one bucket per address, not per /64. The validator container is limited to 128 processes and threads and 256 MiB, and refuses connections beyond about 100 at a time (one thread each).
- **Who can claim an address.** The gateway trusts `X-Real-IP` from anything that connects from Docker's ranges: the host nginx, but also other processes on the workspace. Someone who can run code on the workspace and has the Basic password can therefore use Basic from there. That is a CO member anyway. TES tasks can't: the [task network fence](#task-network-fence) keeps them off the gateway's port and the Docker bridges. The trusted ranges are Docker's default bridge pools, `172.16.0.0/12` and `192.168.0.0/16`. If Docker uses other address pools on a workspace, add them to `tesp_gateway_trusted_proxies`, or every request looks like it comes from the Docker bridge (one rate-limit bucket for everyone).
- **Entitlement prefixes** admit every subgroup under them, including ones created later. Prefer the exact values; the default is the two below.
- **Revocation.** Tokens aren't introspected, so a revoked token works until it expires. A removed entitlement keeps working for up to the userinfo cache time.

Component parameters (all optional). Lists are comma-separated **without spaces**: depending on how SRC passes parameters, a space can split a value.

| Parameter | Default | Meaning |
| --- | --- | --- |
| `src_tes_oauth_enabled` | `false` | accept LS Login access tokens |
| `src_tes_basic_auth_sources` | empty | IPs or CIDRs (at least /8 for IPv4, /16 for IPv6) that may use the Basic password, e.g. the Nextflow workspace's address. Empty: anywhere with tokens off, nowhere with tokens on |
| `src_tes_oauth_client_ids` | empty | client ids whose tokens count (Krini: generic-issues#48); required with tokens on |
| `src_tes_oauth_exempt_client_ids` | empty | service clients that skip the entitlement check (proTES: #47); must also be allowed, and need `src_tes_oauth_exempt_audiences` |
| `src_tes_oauth_audiences` | empty | values of `aud` that **every** token, Krini's too, must have one of. Leave it empty unless every client's tokens carry that value, or Krini users are locked out |
| `src_tes_oauth_exempt_audiences` | empty | values of `aud` naming this TES that only **exempt** clients' tokens (proTES) must carry; required with exempt clients, never a client id. This is the one proTES needs |
| `src_tes_oauth_audit_tag_keys` | empty | task tags that name the user of an exempt client's task, logged for audit (proTES#254) |
| `src_tes_oauth_entitlements` | EOC-Compute and ADMIN (below) | `eduperson_entitlement` values, any one of which is enough |
| `src_tes_oauth_entitlement_prefixes` | empty | prefixes ending with `:`, for subgroups |
| `src_tes_rate_exempt_sources` | empty | IPs or CIDRs (at least /8 for IPv4, /16 for IPv6) that are never rate-limited, e.g. proTES's or the Nextflow workspace's (the Basic sources aren't limited either) |
| `src_tes_cors_origins` | empty | browser origins (`https://host[:port]`, without `:443`, as browsers send them) that may call the API, e.g. `https://krini.rahtiapp.fi` |

Take the entitlement values from a real userinfo response of a member for that client. Don't guess them. From [generic-issues#46](https://github.com/elixir-cloud-aai/generic-issues/issues/46):

- the values come from userinfo, not from the token;
- LS Login releases a group only to clients whose Perun facility it is assigned to, so the parent `ECP_CLN` value arrives for some clients and not others;
- match the subgroups you need, for example `urn:geant:lifescience-ri.eu:group:elixir:ECP_CLN:EOC-Compute#aai.lifescience-ri.eu` for compute users and `…:ECP_CLN:ADMIN#aai.lifescience-ri.eu` for admins, or use a prefix;
- each value also arrives in an `…elixir-europe.org…#perun.elixir-czech.cz` form, so match the `lifescience-ri.eu` form, or list both forms explicitly.

Krini's client id is recorded in [generic-issues#48](https://github.com/elixir-cloud-aai/generic-issues/issues/48), and proTES's service client id in [#47](https://github.com/elixir-cloud-aai/generic-issues/issues/47); proTES belongs in `src_tes_oauth_exempt_client_ids` too. With tokens on, the playbook stops before installing anything unless there is at least one allowed client, and exempt audiences if there are exempt clients. Without `src_tes_oauth_entitlements`, these two exact values from #46 apply (check them against a real userinfo response of a member before relying on them): `urn:geant:lifescience-ri.eu:group:elixir:ECP_CLN:EOC-Compute#aai.lifescience-ri.eu` and `urn:geant:lifescience-ri.eu:group:elixir:ECP_CLN:ADMIN#aai.lifescience-ri.eu` (`tesp_oauth_default_entitlements`). The playbook checks a new gateway config with `nginx -t` before it replaces the old one, then waits until the validator answers its health check. Switching tokens off removes the validator, and any other container left in the stack's compose project (`remove_orphans`). The parameters fill one LS Login entry in `tesp_oauth_issuers` in `group_vars/all.yml`. For other issuers, audiences or algorithms, set that list directly; its comment describes each field. For a test issuer with its own CA, point `tesp_oauth_ca_bundle` at a CA bundle on the workspace that includes it, for example `/etc/ssl/certs/ca-certificates.crt` after adding the CA to the system. The bundle replaces the validator's own trust store, so it must also hold the public CAs if LS Login stays configured.

**Browser portals (CORS).** Krini's Cloud Components call the TES from the user's browser, so every call is cross-origin. List the portal's exact origins in `src_tes_cors_origins` (comma-separated, no spaces), today `https://krini.rahtiapp.fi`; the production address comes with [generic-issues#55](https://github.com/elixir-cloud-aai/generic-issues/issues/55). For those origins only, the gateway answers the browser's `OPTIONS` preflight itself (`204`, without auth) and adds `Access-Control-Allow-Origin` to every answer, errors included, so the portal can show them. It exposes `WWW-Authenticate` and `Retry-After` to the portal (`Access-Control-Expose-Headers`), and never sends a wildcard or `Access-Control-Allow-Credentials` (Krini sends a Bearer header, not cookies). Empty, the default: no CORS headers, as before.

Call the TES with a token:

```bash
curl -H "Authorization: Bearer $TOKEN" https://<workspace-fqdn>/tes/v1/tasks
```

`401` answers carry `WWW-Authenticate: Bearer realm="tes", error="invalid_token"` for a rejected token. The validator's log (`docker logs tesp-validator`) says why, without the token.

**Trying it on a workspace.** You need an LS Login access token for one of the allowed clients, for an account with the entitlement.

1. Create the workspace with `src_tes_oauth_enabled = true`, the client id(s) and the entitlement value(s). Or set them in `group_vars/all.yml` and re-run the playbook.
2. Check that service info is still public: `curl https://<workspace-fqdn>/tes/v1/service-info`.
3. Check that a request without credentials is refused: `curl -i https://<workspace-fqdn>/tes/v1/tasks` gives `401`.
4. Get an access token from the client, for example with its device-code flow, and put it in `$TOKEN` without echoing it.
5. `curl -H "Authorization: Bearer $TOKEN" https://<workspace-fqdn>/tes/v1/tasks` gives `200`. Submit a task as in [Run a task](#4-run-a-task), with `-H "Authorization: Bearer $TOKEN"` instead of `-u`.
6. With an account without the entitlement the same call gives `403`, and with a token of another client `401`; `docker logs tesp-validator` on the workspace says which check failed.
7. If `src_tes_basic_auth_sources` is set, check that the Basic password works from the Nextflow component, and that `curl -i -u 'tes:<password>' https://<workspace-fqdn>/tes/v1/tasks` from anywhere else gives `401`.

## Resource limits

A task's `resources.cpu_cores` and `resources.ram_gb` become SLURM options for its job: tesp-api fills in `pulsar.native_specification.*` in `settings.toml` and Pulsar hands the result to SLURM through DRMAA. A task with `"cpu_cores": 2, "ram_gb": 1.5` runs as a job with `--cpus-per-task=2 --mem=1536` (MiB, rounded up). A task that leaves a resource out gets no option for it. The option formats are `tesp_native_spec_cpu_cores` and `tesp_native_spec_ram_gb` in `group_vars/all.yml`; set one to `""` to stop passing that resource to SLURM.

SLURM sees the workspace's CPUs (`CPUs`) and its memory minus `tesp_slurm_reserved_memory_mb` (2 GiB, at most half the node) for the OS, Docker and Pulsar (`RealMemory`). CPUs and memory are consumable resources (`SelectType=select/cons_tres`, `SelectTypeParameters=CR_CPU_Memory`), and a job without `--mem` gets `DefMemPerCPU`, its per-CPU share of `RealMemory`. What that means for tasks:

- **Scheduling:** a job starts only when that many CPUs and that much memory are free; until then it waits in the queue (`squeue` shows it as pending). Tasks therefore don't oversubscribe the node, and tesp-api keeps waiting however long a task queues (see [Long tasks](#long-tasks)).
- **Too large for the node:** a task asking for more CPUs or memory than the node has is rejected by SLURM at submission and the task fails. For memory SLURM always did this; for CPUs it takes `EnforcePartLimits=ALL`, without which the job would stay pending forever (#21). Size the workspace for your largest task, or cap the requests in the client, for example with Nextflow's `process.resourceLimits = [cpus: 8, memory: 30.GB]`.
- **Memory at run time:** enforced by polling. `jobacct_gather/linux` samples the memory of each job's processes every 10 s (`JobAcctGatherFrequency=task=10`), counting shared pages once (`UsePss`), and `JobAcctGatherParams=OverMemoryKill` kills a job that uses more than its `--mem`. The task fails; its stderr says `exceeded memory limit`. What to know:
  - **Tasks without `ram_gb`** are held to `DefMemPerCPU` per CPU, that is (memory − 2 GiB) / CPUs: about 1.7 GiB on an 8-CPU, 16 GB workspace. Before #13 they could use any free memory. Clients that leave memory out (a Nextflow process without `memory`) may need to set it now.
  - **Not a hard limit:** a task that allocates faster than one sample interval can still push the node into the kernel's out-of-memory killer before SLURM sees it, and memory in `/dev/shm` or another tmpfs isn't counted. The other way round, resident pages of memory-mapped files do count: a tool that maps a large file (an aligner with a big index) can be killed where a cgroup limit would only have reclaimed the page cache. Give such tasks a `ram_gb` that covers the mapped file.
  - **Apptainer only:** it covers processes started inside the job, which is how the default `tesp_container_type: singularity` runs tasks. That includes pulling and converting an image that isn't cached yet: a task with a small `ram_gb` and a large uncached image can be killed while the image converts, with the same message. Raise its `ram_gb`, or run it once with more memory so the image is cached. A Docker container runs outside the job, and only Docker's `--memory` limits it.
  - **On a workspace:** `scontrol show config | grep JobAcctGather` shows the settings, a killed job leaves `exceeded memory limit` in `/var/log/slurm/slurmd.log`, and `tests/e2e/resource-limits.sh ram-within ram-overuse` (see [Checks](#checks)) tests it.
- **CPUs at run time:** pinned, not confined. `TaskPlugin=task/affinity` pins each job to the CPUs SLURM gave it, so a task's threads run only on its `cpu_cores` CPUs and `nproc` inside the task shows that number. A task without `cpu_cores` gets one CPU; before #39 it could use all of them. Clients that leave CPUs out may need to set them now. What to know:
  - **Not a limit:** pinning is the kernel's CPU affinity, without cgroups. A task can widen its own (`taskset -c 0-7`, `sched_setaffinity`) and use CPUs given to other tasks. Tools that count CPUs from `/proc/cpuinfo` or `os.cpu_count()`, not from their affinity, still start one thread per node CPU, but those threads share the task's CPUs.
  - **Only where the CPUs are numbered 0..N-1:** `task/affinity` binds by CPU number. Where the node may only run on other CPUs (a container pinned to some host CPUs, a cpuset), binding fails, every job fails to start and SLURM drains the node. The playbook therefore reads the CPUs PID 1 may use (`Cpus_allowed_list` in `/proc/1/status`) and sets `task/affinity` only when they are `0`..N-1, as on an SRC VM; otherwise it keeps `task/none`, CPUs aren't pinned, and the deploy prints a warning (`Warn that tasks won't be pinned to their CPUs`).
  - **On a workspace:** `scontrol show config | grep TaskPlugin` shows which one is set, and `tests/e2e/resource-limits.sh cpu-pinned` (see [Checks](#checks)) tests it.
- **Why not cgroups:** SLURM's `task/cgroup` and `proctrack/cgroup` would give hard memory and CPU limits (tracked in #39), but SLURM 21.08, the version in Ubuntu 22.04, only has a cgroup v1 plugin (cgroup v2 support came in 22.05), and Ubuntu 22.04 mounts cgroup v2 only by default. With either plugin slurmd doesn't start. `OverMemoryKill` can't be combined with `task/cgroup` either: slurmd 21.08 then refuses to start ("Jobs memory is being constrained by both TaskPlugin cgroup and JobAcctGather plugin"). The role's default `ProctrackType=proctrack/pgid` stays.
- **Re-running the playbook:** a change to these SLURM settings restarts `slurmd` and `slurmctld` (jobs already running keep the old settings until they end), and changing `SelectType` on a running node loses SLURM's running and pending jobs. Re-run the playbook on an existing workspace only when no tasks are running.

The container itself gets no `--cpus`/`--memory` flags: `container.resource_limits = "auto"` (`tesp_container_resource_limits`) applies them only to Docker. Apptainer needs a working cgroups manager for those flags and fails the task without one.

## Long tasks

tesp-api waits for each task until its SLURM job finishes, however long it queues or runs. It asks Pulsar for the job's status 4 seconds after submission and then less and less often, at most once a minute (`pulsar.status.poll_interval` and `max_poll_interval` in `templates/tesp-api/settings.toml.j2`). If Pulsar can't be reached or answers with a server error, tesp-api retries for 10 minutes (`error_retry_seconds`) before it fails the task. Canceling a task takes effect at the next status request, so up to a minute later.

To cap how long a task may queue and run, set `tesp_max_wait_seconds` in `group_vars/all.yml` (default `0`, no limit; `pulsar.status.max_wait_seconds` in `settings.toml`). The clock starts when the job is submitted to Pulsar, and tesp-api checks it after each status request, so up to a minute late. Past it tesp-api cancels the Pulsar job, and with it the SLURM job, and the task ends in `SYSTEM_ERROR`. A task with S3 outputs must also finish within `tesp_s3_output_presign_expiry_seconds` (1 day), or its uploads fail (see [Using S3](#using-s3)).

**Known limitation:** tesp-api waits for tasks in its own process and doesn't pick them up again after a restart. Restarting it (for example by re-running the playbook, which recreates the `tesp-api` container whenever its config changes, and today that is every run, because the TES password is regenerated each time; see also [#9](https://github.com/mirelaminkova/TESP-src/issues/9)) loses every task that is queued or running: the task never gets its logs or final state (after a clean shutdown it shows `CANCELED`, after a crash it keeps its last state), and its Pulsar and SLURM job goes on. Redeploy only when no tasks are running.

## Task network fence

TES tasks run as SLURM jobs of the Pulsar user (`pulsar`), and Apptainer gives them no network namespace of their own: they share the workspace's network. Without a fence, any user allowed to submit a task could reach MongoDB (no authentication) and tesp-api directly on the Docker bridge, the gateway and Pulsar on loopback, and other services on the workspace, and could serve on any port the workspace's security group opens (issue #45).

The `tesp-task-fence` systemd unit adds an iptables and ip6tables chain, `TESP-TASKS`, that the first rule of `OUTPUT` applies to every process of the `pulsar` user, and to nobody else (root, SSH and other users are not affected). For those processes:

- New connections to the workspace's own addresses (loopback, the public address, the Docker bridge gateways), to containers on Docker bridges (`docker0`, `br-*`), to link-local addresses (the cloud metadata service) and to broadcast and multicast addresses are refused. Exceptions: DNS (port 53) and slurmctld (`tesp_task_fence_loopback_tcp_ports`, 6817) on the workspace itself, at any of its addresses, which Pulsar itself needs for DRMAA. Tasks run as the same user, so they can reach those too.
- They can't answer connections that others open. The one exception is Pulsar on port 8913, over IPv4, to loopback and the Docker bridges, where `pulsar-proxy` is. A task can still open a listening port and receive datagrams on it, but it can't answer, so no TCP connection to it is ever set up.
- Everything else is untouched: tasks still pull images and download and upload inputs and outputs (S3, http(s)) themselves.

Pulsar runs as `pulsar` too, so it is fenced as well. That is why its uwsgi HTTP router hands requests to its worker over a unix socket in `/run/pulsar` (`tesp_pulsar_uwsgi_socket`; a drop-in for Pulsar's unit, `tesp_pulsar_unit`, creates the directory) instead of uwsgi's default TCP port on loopback. For the same reason, `pulsar` is no longer in the `docker` group, which is root-equivalent and would let a task step around the fence. It is added only with `tesp_container_type: docker`. On hosts deployed before, the playbook removes it, restarts slurmctld (also on a later run if an earlier one stopped part-way), and restarts Pulsar if its process still has the group. The slurmctld restart is needed because slurmctld caches each user's groups for new jobs for about 10 minutes, and `scontrol reconfigure` doesn't clear that cache. After it, new tasks start without the group at once; SLURM is unavailable for a few seconds while it restarts. SLURM jobs that were already running keep the group until they end.

Pulsar and slurmd require the unit, so they won't start if the rules can't be applied. That includes jobs that SLURM restarts at boot. At boot it runs after `ufw` and `netfilter-persistent`, if they are installed. Each run loads the chain with one `iptables-restore` and one `ip6tables-restore`, so re-applying never leaves it empty. It also puts the jump back in first place if something else has added rules ahead of it. The playbook runs the script on every deploy, so a re-run repairs rules that something else has dropped. Check the rules with `iptables -S TESP-TASKS`, `ip6tables -S TESP-TASKS`, `iptables -S OUTPUT` and `ip6tables -S OUTPUT`. If something rewrites the firewall later (`ufw enable` or `ufw reload`, a full `iptables-restore`), re-run `/usr/local/sbin/tesp-task-fence` or the playbook. Don't use `systemctl restart tesp-task-fence` for this: restarting the unit restarts Pulsar and slurmd, and stopping it stops them but leaves the rules in place. To take the fence off by hand, delete the jump (`iptables -D OUTPUT -m owner --uid-owner pulsar -j TESP-TASKS`, and the same with `ip6tables`).

What it doesn't cover:

- With `tesp_container_type: docker`, tasks run in Docker containers as root, not as `pulsar`, so the fence doesn't apply. Use `singularity` (the default).
- Tasks still share one user, `pulsar`, with each other and with Pulsar. A task can see and stop the files and processes of other tasks, and use what Pulsar can use: its unix socket, its config, and slurmctld (for example `scancel` on other tasks). A task could also stop Pulsar and take over port 8913 itself, and then receive tesp-api's requests to Pulsar. So against a hostile task, the fence protects everything else on the workspace, but not Pulsar and the other tasks. Running tasks as a user other than Pulsar's, and per-user isolation, are separate changes (#47).
- Only IP traffic is fenced. Unix sockets that the `pulsar` user can open (Pulsar's, munge's) are not.
- `srun` inside a task doesn't work: slurmd and the job's callbacks are on ports that the fence refuses.
- Of Docker's networks, only those with the default bridge names (`docker0`, `br-*`) are fenced. Containers on a network with a custom bridge name (`com.docker.network.bridge.name`) are reachable (#49).
- Other workspaces and services reachable over the network (for example, on a collaboration's private network) are not fenced.
- The workspace's security group (the catalog item's access rules) still decides which ports are open from outside; the fence only stops tasks from answering on them.
- The script needs both iptables and ip6tables. On a kernel booted with IPv6 disabled it fails, and Pulsar doesn't start.

## Troubleshooting

SSH to the workspace with your SRAM key and become root (`sudo su`), then:

```bash
cd /srv/tesp-api
docker compose -f docker-compose.deploy.yml ps
docker compose -f docker-compose.deploy.yml logs -f tesp-api
docker compose -f docker-compose.deploy.yml logs -f gateway
systemctl status pulsar
systemctl status tesp-task-fence
squeue
```

**New tasks end in SYSTEM_ERROR (Pulsar down) or stay QUEUED (slurmd down):** check `systemctl status tesp-task-fence` and `journalctl -u tesp-task-fence`. Pulsar and slurmd require the [task network fence](#task-network-fence), so if its rules can't be applied (at boot, for example), they don't start, on purpose. Fix the cause, then `systemctl start tesp-task-fence pulsar slurmd` (with a custom `pulsar_systemd_service_name`, that unit instead of `pulsar`).

## Configuration notes

- **TESP-API version:** `group_vars/all.yml` pins `tesp_api_repo` to the [mirelaminkova/tesp-api](https://github.com/mirelaminkova/tesp-api) fork and `tesp_api_version` to a commit on its `main` (currently `c3a9f23`). When you bump `tesp_api_version`:
  1. Check that the fork's CI is green at that commit.
  2. Diff the fork's `settings.toml` with `templates/tesp-api/settings.toml.j2` and add any new or renamed keys. The rendered file is bind-mounted over the checkout's `settings.toml`, so a missing key is missing in production. Also check that `tesp_api/config/properties.py` still loads `.secrets.toml` and that the S3 key names still match `templates/tesp-api/secrets.toml.j2`; otherwise the credentials are silently ignored and S3 access becomes anonymous. Also check that `tesp_api/service/resources.py` still fills in the `{cpu_cores}`, `{ram_gb}` and `{ram_mib}` placeholders used by `tesp_native_spec_*`, and that `load_status_polling` in `tesp_api/service/job_polling.py` still reads the `pulsar.status.*` keys the template sets.
  3. Diff the fork's `docker-compose.yaml` with `templates/tesp-api/docker-compose.yml.j2` for new build targets, env vars, volumes or services.
  4. Re-test on a workspace.
- **Container runtime:** `tesp_container_type: singularity` runs task images with Apptainer, using `singularity exec --no-eval`, which requires Apptainer 1.1 or newer. The `usegalaxy_eu.apptainer` role installs it from the Apptainer PPA, which provides 1.5.x for Ubuntu 22.04; it doesn't upgrade an apptainer that is already installed. The upstream `both` mode is broken (it leaves out the `docker://` image prefix), so don't use it.
- **S3:** set from the `src_s3_*` component parameters (see [Using S3](#using-s3)). `settings.toml` holds the endpoint, region and addressing style. The key pair goes in `/etc/tesp-api/secrets.toml` (`tesp_secrets_file`, root, 0600), which is bind-mounted read-only as `/app/.secrets.toml` for tesp-api's Dynaconf to merge in. The playbook validates the parameters before it installs anything. The presign lifetimes are `tesp_s3_presign_expiry_seconds` and `tesp_s3_output_presign_expiry_seconds` in `group_vars/all.yml`; the upload lifetime must cover a whole task run.
- **Generated config:** `settings.toml` (with the TES password, 0600), `secrets.toml` (the S3 key pair, 0600) and `pulsar-token-proxy.conf` (the Pulsar token, 0640) are written to `/etc/tesp-api` (`tesp_secrets_dir`, root, 0700) and bind-mounted read-only into the containers: `settings.toml` over the checkout's own `/app/settings.toml`, `secrets.toml` as `/app/.secrets.toml`, and the proxy config as the `pulsar-proxy` nginx config. They're kept out of `/srv/tesp-api` because that directory is the image build context (the Dockerfile does `COPY . .`), so the image would otherwise contain them. A container reads its config only at start, so the playbook puts the checksums of the files each container reads into that container's labels (`tasks/tesp-api-config.yml` for `tesp-api`): a changed `settings.toml` or `secrets.toml` changes `tesp-api`'s labels, a changed proxy config `pulsar-proxy`'s, and the playbook's `docker compose up` recreates that container. This also catches up on the next run if a run failed halfway. (`docker-compose.deploy.yml` itself is still in the build context, so any change to it, a label included, also rebuilds the `tesp-api` image.) `tesp-api` images built by earlier versions of this playbook still hold the `settings.toml` of those deploys (the TES password), and older ones also `pulsar-token-proxy.conf` (the Pulsar token), in the image and in the build cache. Once a deploy of this version has rebuilt the image, `docker image prune` and `docker builder prune` remove them.
- **Pulsar port:** Pulsar listens on `0.0.0.0:8913` because the `pulsar-proxy` container reaches it through `host.docker.internal` (the Docker bridge gateway). The `tesp-pulsar-firewall` systemd unit adds an iptables chain, `TESP-PULSAR`, that drops connections to 8913 unless they arrive on `lo`, `docker0` or a `br-*` Docker bridge. Pulsar requires that unit, so it won't start if the rule can't be applied. Check the rule with `iptables -S TESP-PULSAR`. If something rewrites the firewall later (`ufw reload`, `iptables-restore`), re-run `/usr/local/sbin/tesp-pulsar-firewall`.
- **Pulsar token:** generated by `tasks/pulsar-token.yml` on the first run (32 letters and digits) and kept in `tesp_pulsar_token_file` (`/etc/tesp-api/pulsar_token`, 0600). Later runs read it back, so Pulsar's `app.yml` and `pulsar-token-proxy.conf` stay the same.
  - A token put in the file by hand is kept even if it has `+`, `/` or `=`: the proxy template URL-encodes it. An empty or short file (under 24 characters) stops the playbook with a message to delete it.
  - To rotate the token, delete the file and re-run the playbook.
  - `--check` works on a deployed host, including one deployed before the token file existed: that one gets a stand-in token that is never written, and the real one is generated on the next normal run. (On a host where nothing is installed yet, the roles themselves don't support `--check`; the Pulsar restart is skipped there because systemd has no Pulsar unit yet.)
  - Pulsar is restarted when its config changes, by the playbook's `Restart Pulsar` handler (the role's own restart handler never runs; see `pulsar_restart_handler_name` in `group_vars/all.yml`). In case a run stopped before handlers ran, the playbook also restarts Pulsar whenever it isn't running, or a file in its config directory is newer than the running process (`templates/pulsar-config-stale.sh.j2`). That script reads Pulsar's state and start time from systemd over D-Bus with `busctl`, as microseconds since the epoch, so no local time is parsed; it needs the D-Bus system bus, which Ubuntu runs.
  - `pulsar-proxy` reads its config only when it starts, so the playbook puts the config's sha256 in a label on the service (`docker-compose.deploy.yml`), and Compose recreates the container whenever the config changes, also on the next run if a run stopped halfway.
  - **Where the token is:** only in that file, Pulsar's `app.yml` and `/etc/tesp-api/pulsar-token-proxy.conf` (root, 0640, in the root-only `/etc/tesp-api`; kept out of `/srv/tesp-api`, the tesp-api image's build context, so no image layer holds it). The role writes `app.yml` (and backups of earlier versions next to it) as 0444, but `pulsar_group: pulsar` makes the role create `pulsar_root` and every directory under it `0750` (`root:pulsar` for `config`, `pulsar:pulsar` for the job directories), so only root and the `pulsar` user can reach them. That includes the job data, on purpose: job scripts in the staging directory hold presigned S3 URLs. The role applies one mode to all its directories, so `config` can't be restricted alone. On an attached storage volume (`src_pulsar_storage_path`), the volume's root directory becomes `0750 root:pulsar` too.
  - **Logs:** uwsgi logs no requests and no write errors (`disable-logging`, `ignore-sigpipe`, `ignore-write-errors`, `disable-write-exception` in `pulsar_uwsgi_options`), because both would show the request URL, which carries the token. Pulsar's own log lines still go to `journalctl -u pulsar`. One place is left: on any upstream error (Pulsar down or restarting, a timeout, a connection closed early, a 502), `pulsar-proxy`'s nginx error log (`docker logs tesp-pulsar-proxy`) shows the upstream URL with the token. Only root and the `docker` group can read it, and they can read the token from the container anyway.
  - **After upgrading from a version before these changes, rotate the token once:** delete `/etc/tesp-api/pulsar_token`, re-run the playbook, and run `docker image prune`. Older journal entries and images built before then still hold earlier tokens.
  - **Ansible output:** tasks that set or read the token or a password are `no_log`; the templates that render one, and the Pulsar role, run with `diff: false`. So `-v` and `--diff` don't print them.
- **MTU:** SRC needs a lower MTU on Docker networks (`tesp_network_mtu: 1442`).
- **Roles and collections:** `requirements.yml` pins every role and collection, including `community.docker`'s dependency, so a new upstream release can't change what a new workspace installs (`tests/requirements-pinned.yml` checks this). SRC's catalog item runs Ansible 9.1.0 (its `remote_ansible_version`; the external plugin's default is 11.1.0), which is ansible-core ~=2.16.1, so the Galaxy collections are the newest releases whose `requires_ansible` covers 2.16 (versions and the cut-off in `requirements.yml`). Before bumping one, check its `requires_ansible` on Galaxy or in its `meta/runtime.yml`, and raise past 2.16 only together with SRC's Ansible version.

  CI's `src-ansible-core` job installs the pinned collections under ansible-core 2.16 and fails if one of them excludes that version (`tests/check-collections.sh`; needs network access). The unit tests run under 2.16 in the `unit` matrix.
- **Validator:** with tokens on, the compose service `validator` (container `tesp-validator`) is built from `files/tes-validator/`, copied to `tesp_validator_dir` (`/srv/tesp-validator`): a pinned Python base image and dependencies installed by hash (`requirements.txt`, generated from `requirements.in` with `pip-compile --generate-hashes`). It runs as `nobody` with a read-only file system and no capabilities, and has no port. Its configuration is `/etc/tesp-gateway/validator/config.json`, rendered from `tesp_oauth_*` and `tesp_basic_auth_enabled`. With tokens on, `gateway.conf` holds tesp-api's Basic credentials, so it is readable by root only.
- **Gateway:** the compose service `gateway` (container `tesp-gateway`) runs nginx. Its config `gateway.conf` and `service-info.json` live in `tesp_gateway_dir` (`/etc/tesp-gateway`), mounted as `/etc/nginx/conf.d`, outside the image build context. It is the only container with a host port (`127.0.0.1:8080`, `tesp_api_port`); tesp-api is reachable only inside the stack. A changed `gateway.conf` changes a label on the container, so the playbook's `docker compose up` recreates it; `service-info.json` is read on every request. The gateway resolves `tesp-api` through Docker's DNS on each request, so it finds a rebuilt tesp-api container without a restart. The gateway and `pulsar-proxy` share one nginx image, `tesp_nginx_image`, pinned by digest.

## Checks

CI (`.github/workflows/ci.yml`) runs on every pull request and every push to `main`. Its `lint` job runs the [pre-commit](https://pre-commit.com/) hooks in `.pre-commit-config.yaml`: yamllint, shellcheck, ansible-lint, ast-grep with the rules in `.ast-grep/`, and semgrep. Its `unit` jobs run `tests/run.sh`, which runs every test in `tests/` (the validator's unit tests, `tests/test_tes_validator.py`, pytest, 100% line and branch coverage required, and the playbook tests), on ansible-core 2.16.20 (what SRC's plugin installs), 2.18.1 and 2.21.5; a job named `unit` sums them up for branch protection. The `src-ansible-core` job checks the pinned collections against ansible-core 2.16. `.github/workflows/e2e.yml` brings up the tesp-api stack as the playbook renders it (`tests/e2e/render-stack.yml`, without Pulsar) and checks the gateway with `tests/e2e/check-gateway.sh`: public service info, `401` without credentials, Basic auth through to tesp-api, and no host port for tesp-api. The workflow then recreates tesp-api with a new address and checks that the gateway finds it. It runs twice: with Basic only, and with tokens on against a mock LS Login over https (`tests/e2e/idp/`, `tests/e2e/check-oauth.sh`). Its `task-fence` job renders the [task network fence](#task-network-fence) (`tests/e2e/render-task-fence.yml`) and applies it in a throwaway network namespace, where `tests/e2e/check-task-fence.sh` checks what a task user can and can't reach (`sudo tests/e2e/check-task-fence.sh <rendered script> nobody` runs it locally). Run the same checks locally before you push:

```bash
pip install pre-commit==4.7.0 ansible-core==2.21.5 -r tests/requirements.txt
```

```bash
pip install --require-hashes --only-binary=:all: --no-deps -r files/tes-validator/requirements.txt
```

```bash
pre-commit install
```

`pre-commit install` sets up both stages. The linters run on `git commit`, and so do the unit tests when the commit touches `playbook.yml`, `requirements.yml`, `group_vars/`, `templates/`, `tests/` or the workflows. ast-grep and semgrep run on `git push`. To run everything on demand:

```bash
pre-commit run --all-files && pre-commit run --all-files --hook-stage pre-push
```

ansible-lint runs offline against stand-ins for the roles and collections in `requirements.yml` (`mock_roles` and `mock_modules` in `.ansible-lint`); add new ones there.

`tests/render-templates.yml` renders every template with test values and checks, among other things, that the welcome page doesn't leak the Pulsar token or dump variables, and that the S3 key pair ends up only in the secrets file. It needs only `ansible-core`, not root, Docker or the roles from `requirements.yml`. `tests/run.sh` runs it; to run it alone:

```bash
ansible-playbook -i localhost, -c local tests/render-templates.yml
```

`tests/e2e/` holds end-to-end checks that need a deployed endpoint, so CI doesn't run them; run them on a test workspace after deploying. `tests/e2e/resource-limits.sh` checks that a task that fits the node completes, that tasks asking for more CPUs or memory than any node has fail at once instead of staying queued, and that a task using more memory than its `ram_gb` is killed, and that a task is pinned to its CPUs (see [Resource limits](#resource-limits)). Its header lists the options and cases. On the workspace itself, with the password from the welcome page:

```bash
read -rsp 'TES password: ' TES_PASSWORD && echo && export TES_PASSWORD
```

```bash
TES_URL=http://127.0.0.1:8080 tests/e2e/resource-limits.sh
```

## Credits and license

This component started as a fork of [ErasmusMC-Bioinformatics/src-component-pulsar](https://github.com/ErasmusMC-Bioinformatics/src-component-pulsar) by Helena Rasche, and keeps its history. TES support comes from [CESNET/tesp-api](https://github.com/CESNET/tesp-api), via the fork named above.

Licensed under the [GNU LGPL v2.1](LICENSE).
