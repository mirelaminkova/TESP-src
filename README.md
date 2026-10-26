# TESP on SURF Research Cloud

This repository holds the Ansible playbook for a **GA4GH Task Execution Service (TES)** component on [SURF Research Cloud](https://portal.live.surfresearchcloud.nl/) (SRC). It turns a single workspace into a TES endpoint: you submit containerised tasks over HTTPS, and they run as SLURM jobs on the workspace.

It is built on [TESP-API](https://github.com/CESNET/tesp-api) from CESNET, which uses [Pulsar](https://pulsar.readthedocs.io/) as its execution backend.

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
Pulsar (host, :8913)    not exposed through nginx
   ▼
SLURM + DRMAA (host)    each task runs in an Apptainer container
```

What the playbook sets up:

| Part | Where it is configured |
| --- | --- |
| SLURM controller + worker on the same node, DRMAA, chrony | `playbook.yml` (first play), `group_vars/all.yml` |
| Pulsar with the `queued_drmaa` manager | `group_vars/all.yml` (`pulsar_*`) |
| Docker, Apptainer, nginx reverse proxy with SRAM login | `playbook.yml`, `group_vars/all.yml` (`nginx_reverse_proxy_locations`) |
| TESP-API stack: tesp-api, MongoDB, pulsar-proxy | `templates/tesp-api/`, `group_vars/all.yml` (`tesp_*`) |
| Welcome page with your endpoint and credentials | `templates/welcome.html` |

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
> To keep job data on an SRC storage volume, attach one when creating the workspace and set the component parameter `src_pulsar_storage_path` to its mount path. Without it, job data lives in `/mnt/pulsar` on the workspace disk.

### 3. Get your endpoint

When the workspace is running, click **Access**. After an SRAM login you see the welcome page for your node, with:

- the **TES base URL**: `https://<workspace-fqdn>/tes`
- the **username** (`tes`) and the generated **password**
- ready-to-copy `curl` commands and a Nextflow config snippet

Only members of your CO can open this page. Anyone with the username and password can submit tasks, so don't share them.

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

- **TESP-API version** is pinned in `group_vars/all.yml` (`tesp_api_version`). Update it deliberately and re-test.
- **Container runtime:** `tesp_container_type: singularity` runs task images with Apptainer. The upstream `both` mode is broken (it leaves out the `docker://` image prefix), so don't use it.
- **MTU:** SRC needs a lower MTU on Docker networks (`tesp_network_mtu: 1442`).

## Credits and license

This component started as a fork of [ErasmusMC-Bioinformatics/src-component-pulsar](https://github.com/ErasmusMC-Bioinformatics/src-component-pulsar) by Helena Rasche, and keeps its history. TES support comes from [CESNET/tesp-api](https://github.com/CESNET/tesp-api).

Licensed under the [GNU LGPL v2.1](LICENSE).
