# TESP on SURF Research Cloud

This repository holds the Ansible playbook for a **GA4GH Task Execution Service (TES)** component on [SURF Research Cloud](https://portal.live.surfresearchcloud.nl/) (SRC). It turns a single workspace into a TES endpoint: you submit containerised tasks over HTTPS, and they run as SLURM jobs on the workspace.

It is built on [TESP-API](https://github.com/CESNET/tesp-api) from CESNET, which uses [Pulsar](https://pulsar.readthedocs.io/) as its execution backend. The component deploys the [mirelaminkova/tesp-api](https://github.com/mirelaminkova/tesp-api) fork, which carries TES and S3 fixes.

## How it works

```
TES client (curl, Nextflow, ...)
   │  HTTPS + HTTP Basic auth
   ▼
nginx (host)            /tes/  →  127.0.0.1:8080
   ▼
tesp-api (Docker)       ──►  MongoDB (Docker, task state)
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
| TESP-API stack: tesp-api, MongoDB, pulsar-proxy | `templates/tesp-api/`, `group_vars/all.yml` (`tesp_*`) |
| Welcome page with your endpoint and credentials | `templates/welcome.html` |
| Host firewall rule for Pulsar's port | `templates/pulsar-firewall.*.j2`, `playbook.yml`, `group_vars/all.yml` (`pulsar_port`) |

Secrets are generated at deploy time: a random Pulsar token (only used between `pulsar-proxy` and Pulsar) and a random password for the TES endpoint.

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
> To keep job data on an SRC storage volume, attach one when creating the workspace and set the component parameter `src_pulsar_storage_path` to its mount path. Without it, job data lives in `/mnt/pulsar` on the workspace disk. To use S3 storage for task inputs and outputs, see [Using S3](#using-s3).

### 3. Get your endpoint

When the workspace is running, click **Access**. After an SRAM login you see the welcome page for your node, with:

- the **TES base URL**: `https://<workspace-fqdn>/tes`
- the **username** (`tes`) and the generated **password**
- ready-to-copy `curl` commands and a Nextflow config snippet

Only members of your CO can open this page. It shows the TES credentials, but not the Pulsar token or other deploy-time secrets. Anyone with the username and password can submit tasks, so don't share them.

### 4. Run a task

Check the service:

```bash
curl -u 'tes:<password>' https://<workspace-fqdn>/tes/v1/service-info
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

- **Who sets what:** the catalog item maintainer adds the parameters to the catalog item. Make the endpoint, region and addressing style overwritable if workspace owners should choose them when they create a workspace.
- **Secrets:** give the access key and secret key from SRC's secret store (see the SRC documentation on secrets in component parameters), never as plain fixed values, which anyone who can see the catalog item can read. Every workspace created with the same secret shares that key pair, so use a key that only grants access to the buckets meant for TESP.
- **Both or neither:** without a key pair, TESP accesses S3 anonymously, so only public buckets can be read and `s3://` outputs fail when the task uploads them.
- **Validation:** the playbook stops before installing anything if a parameter is invalid, for example an endpoint without `https://`, an unknown addressing style, or only one of the two keys.

Example for SURF Object Store, with the S3 endpoint and key pair of your Object Store project:

```
src_s3_endpoint_url      = <Object Store S3 endpoint, e.g. https://objectstore.surf.nl>
src_s3_addressing_style  = path
src_s3_access_key_id     = <access key>   (secret)
src_s3_secret_access_key = <secret key>   (secret)
```

The endpoint must be reachable from two places: from the workspace (tesp-api, and Pulsar staging inputs and outputs), and from wherever Nextflow runs, because Nextflow itself reads and writes the work directory. Configure Nextflow with the same endpoint and its own credentials, for example:

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

## Resource limits

A task's `resources.cpu_cores` and `resources.ram_gb` become SLURM options for its job: tesp-api fills in `pulsar.native_specification.*` in `settings.toml` and Pulsar hands the result to SLURM through DRMAA. A task with `"cpu_cores": 2, "ram_gb": 1.5` runs as a job with `--cpus-per-task=2 --mem=1536` (MiB, rounded up). A task that leaves a resource out gets no option for it. The option formats are `tesp_native_spec_cpu_cores` and `tesp_native_spec_ram_gb` in `group_vars/all.yml`; set one to `""` to stop passing that resource to SLURM.

SLURM sees the workspace's CPUs (`CPUs`) and its memory minus `tesp_slurm_reserved_memory_mb` (2 GiB, at most half the node) for the OS, Docker and Pulsar (`RealMemory`). CPUs and memory are consumable resources (`SelectType=select/cons_tres`, `SelectTypeParameters=CR_CPU_Memory`), and a job without `--mem` gets `DefMemPerCPU`, its per-CPU share of `RealMemory`. What that means for tasks:

- **Scheduling:** a job starts only when that many CPUs and that much memory are free; until then it waits in the queue (`squeue` shows it as pending). Tasks therefore don't oversubscribe the node, and tesp-api keeps waiting however long a task queues (see [Long tasks](#long-tasks)).
- **Too large for the node:** a task asking for more CPUs or memory than the node has is rejected by SLURM and fails at submission. Size the workspace for your largest task, or cap the requests in the client, for example with Nextflow's `process.resourceLimits = [cpus: 8, memory: 30.GB]`.
- **No hard limit at run time:** SLURM reserves the resources but doesn't confine the job to them. The role's `ProctrackType=proctrack/pgid` and the default task plugin don't use cgroups, so a task can use more CPU or memory than it asked for, at the cost of other tasks.
- **Re-running the playbook:** a change to these SLURM settings restarts `slurmctld`, and changing `SelectType` on a running node loses SLURM's running and pending jobs. Re-run the playbook on an existing workspace only when no tasks are running.

The container itself gets no `--cpus`/`--memory` flags: `container.resource_limits = "auto"` (`tesp_container_resource_limits`) applies them only to Docker. Apptainer needs a working cgroups manager for those flags and fails the task without one.

## Long tasks

tesp-api waits for each task until its SLURM job finishes, however long it queues or runs. It asks Pulsar for the job's status 4 seconds after submission and then less and less often, at most once a minute (`pulsar.status.poll_interval` and `max_poll_interval` in `templates/tesp-api/settings.toml.j2`). If Pulsar can't be reached or answers with a server error, tesp-api retries for 10 minutes (`error_retry_seconds`) before it fails the task. Canceling a task takes effect at the next status request, so up to a minute later.

To cap how long a task may queue and run, set `tesp_max_wait_seconds` in `group_vars/all.yml` (default `0`, no limit; `pulsar.status.max_wait_seconds` in `settings.toml`). The clock starts when the job is submitted to Pulsar, and tesp-api checks it after each status request, so up to a minute late. Past it tesp-api cancels the Pulsar job, and with it the SLURM job, and the task ends in `SYSTEM_ERROR`. A task with S3 outputs must also finish within `tesp_s3_output_presign_expiry_seconds` (1 day), or its uploads fail (see [Using S3](#using-s3)).

**Known limitation:** tesp-api waits for tasks in its own process and doesn't pick them up again after a restart. Restarting it (for example by re-running the playbook, which restarts the tesp-api stack whenever its config changes, and so far on every run because the TES password is regenerated each time; see also [#9](https://github.com/mirelaminkova/TESP-src/issues/9)) loses every task that is queued or running: the task never gets its logs or final state (after a clean shutdown it shows `CANCELED`, after a crash it keeps its last state), and its Pulsar and SLURM job goes on. Redeploy only when no tasks are running.

## Troubleshooting

SSH to the workspace with your SRAM key and become root (`sudo su`), then:

```bash
cd /srv/tesp-api
docker compose -f docker-compose.deploy.yml ps
docker compose -f docker-compose.deploy.yml logs -f tesp-api
systemctl status pulsar
squeue
```

## Configuration notes

- **TESP-API version:** `group_vars/all.yml` pins `tesp_api_repo` to the [mirelaminkova/tesp-api](https://github.com/mirelaminkova/tesp-api) fork and `tesp_api_version` to a commit on its `main` (currently `c3a9f23`). When you bump `tesp_api_version`:
  1. Check that the fork's CI is green at that commit.
  2. Diff the fork's `settings.toml` with `templates/tesp-api/settings.toml.j2` and add any new or renamed keys. The rendered file is bind-mounted over the checkout's `settings.toml`, so a missing key is missing in production. Also check that `tesp_api/config/properties.py` still loads `.secrets.toml` and that the S3 key names still match `templates/tesp-api/secrets.toml.j2`; otherwise the credentials are silently ignored and S3 access becomes anonymous. Also check that `tesp_api/service/resources.py` still fills in the `{cpu_cores}`, `{ram_gb}` and `{ram_mib}` placeholders used by `tesp_native_spec_*`, and that `load_status_polling` in `tesp_api/service/job_polling.py` still reads the `pulsar.status.*` keys the template sets.
  3. Diff the fork's `docker-compose.yaml` with `templates/tesp-api/docker-compose.yml.j2` for new build targets, env vars, volumes or services.
  4. Re-test on a workspace.
- **Container runtime:** `tesp_container_type: singularity` runs task images with Apptainer, using `singularity exec --no-eval`, which requires Apptainer 1.1 or newer. The `usegalaxy_eu.apptainer` role installs it from the Apptainer PPA, which provides 1.5.x for Ubuntu 22.04; it doesn't upgrade an apptainer that is already installed. The upstream `both` mode is broken (it leaves out the `docker://` image prefix), so don't use it.
- **S3:** set from the `src_s3_*` component parameters (see [Using S3](#using-s3)). `settings.toml` holds the endpoint, region and addressing style. The key pair goes in `/etc/tesp-api/secrets.toml` (`tesp_secrets_file`, root, 0600), which is bind-mounted read-only as `/app/.secrets.toml` for tesp-api's Dynaconf to merge in. The playbook validates the parameters before it installs anything. The presign lifetimes are `tesp_s3_presign_expiry_seconds` and `tesp_s3_output_presign_expiry_seconds` in `group_vars/all.yml`; the upload lifetime must cover a whole task run.
- **Generated config:** `settings.toml` (with the TES password), `secrets.toml` (the S3 key pair) and `pulsar-token-proxy.conf` (the Pulsar token) are written to `/etc/tesp-api` (`tesp_secrets_dir`, root, 0700; the files are 0600) and bind-mounted read-only into the containers: `settings.toml` over the checkout's own `/app/settings.toml`, `secrets.toml` as `/app/.secrets.toml`, and the proxy config as the `pulsar-proxy` nginx config. They're kept out of `/srv/tesp-api` because that directory is the image build context (the Dockerfile does `COPY . .`), so the image would otherwise contain them. When any of them changes, the playbook restarts the `tesp-api` and `pulsar-proxy` containers; containers read their config only at start. Changes to the compose file itself are applied when the stack is brought up.
- **Pulsar port:** Pulsar listens on `0.0.0.0:8913` because the `pulsar-proxy` container reaches it through `host.docker.internal` (the Docker bridge gateway). The `tesp-pulsar-firewall` systemd unit adds an iptables chain, `TESP-PULSAR`, that drops connections to 8913 unless they arrive on `lo`, `docker0` or a `br-*` Docker bridge. Pulsar requires that unit, so it won't start if the rule can't be applied. Check the rule with `iptables -S TESP-PULSAR`. If something rewrites the firewall later (`ufw reload`, `iptables-restore`), re-run `/usr/local/sbin/tesp-pulsar-firewall`.
- **MTU:** SRC needs a lower MTU on Docker networks (`tesp_network_mtu: 1442`).

## Checks

`tests/render-templates.yml` renders every template with test values and checks, among other things, that the welcome page doesn't leak the Pulsar token or dump variables, and that the S3 key pair ends up only in the secrets file. It needs only `ansible-core`, not root, Docker or the roles from `requirements.yml`:

```bash
ansible-playbook -i localhost, -c local tests/render-templates.yml
```

## Credits and license

This component started as a fork of [ErasmusMC-Bioinformatics/src-component-pulsar](https://github.com/ErasmusMC-Bioinformatics/src-component-pulsar) by Helena Rasche, and keeps its history. TES support comes from [CESNET/tesp-api](https://github.com/CESNET/tesp-api), via the fork named above.

Licensed under the [GNU LGPL v2.1](LICENSE).
