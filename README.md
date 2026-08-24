# AI Lab Stack

A single-node AI platform for learning the layer that actually matters:
retrieval, orchestration, evaluation, and observability.

Sized for an ASUS X99-E WS with a Xeon E5-1660 v3, 48 GB RAM, and two
GTX 970s — but nothing here is specific to that box beyond the defaults.

| Service | Port | Purpose |
|---|---|---|
| Open WebUI | 3000 | Chat front end, document upload, RAG |
| Langfuse | 3001 | Tracing, evaluation, prompt management |
| Grafana | 3002 | Dashboards (metrics profile) |
| Qdrant | 6333 | Vector database |
| Ollama | 11434 | Local inference (native or containerised) |
| Prometheus | 9090 | Metrics store (metrics profile) |
| GPU exporter | 9835 | nvidia-smi metrics (metrics profile) |

---

## Read this first: run Ollama natively on Windows

The compose file *can* run Ollama in a container, but on this hardware you
probably shouldn't. Reasons:

1. **Maxwell under WSL2 is unreliable.** CUDA passthrough to WSL2 works well
   on Ampere and later. On compute capability 5.2 it is inconsistent, and when
   it fails it fails silently — Ollama falls back to CPU and you spend an
   evening wondering why nothing is faster.
2. **Native Windows Ollama sees your GPUs directly.** No passthrough layer,
   no NVIDIA Container Toolkit, no debugging.
3. **Model storage.** Native Ollama can point straight at your 2 TB SATA
   drive without volume mapping.

So: Ollama on the host, everything else in Docker. That is the default
configuration here.

```powershell
# Install Ollama for Windows from https://ollama.com/download
# Then point it at the roomy drive and pin it to the idle GPU:
[Environment]::SetEnvironmentVariable("OLLAMA_MODELS", "D:\ollama\models", "User")
[Environment]::SetEnvironmentVariable("CUDA_VISIBLE_DEVICES", "1", "User")
[Environment]::SetEnvironmentVariable("OLLAMA_HOST", "0.0.0.0:11434", "User")
```

`OLLAMA_HOST=0.0.0.0` matters — otherwise Ollama binds to localhost only and
containers cannot reach it via `host.docker.internal`.

Restart Ollama, then confirm:

```powershell
ollama --version
curl http://localhost:11434/api/tags
```

---

## Setup

### 1. Prerequisites

- Docker Desktop with the WSL2 backend
- Ollama for Windows (see above)
- ~20 GB free disk for images and volumes, plus space for models

### 2. Configure

```powershell
Copy-Item .env.example .env
```

Generate four secrets and paste them into `.env`:

```powershell
function New-Secret { -join ((48..57)+(97..102) | Get-Random -Count 64 | % {[char]$_}) }
New-Secret   # WEBUI_SECRET_KEY
New-Secret   # LANGFUSE_NEXTAUTH_SECRET
New-Secret   # LANGFUSE_SALT
New-Secret   # LANGFUSE_ENCRYPTION_KEY  (must be exactly 64 hex chars)
```

Also set `POSTGRES_PASSWORD` and `GRAFANA_PASSWORD`.

### 3. Start

```powershell
docker compose up -d                       # core stack
docker compose --profile metrics up -d     # add Prometheus + Grafana + GPU
```

### 4. Pull models

```powershell
ollama pull nomic-embed-text     # ~275 MB, fits GPU 1 comfortably
ollama pull llama3.2:3b          # ~2 GB, fits GPU 1
ollama pull llama3.1:8b          # ~4.7 GB, CPU or partial offload
```

---

## First-run walkthrough

1. **Open WebUI** at <http://localhost:3000>. Create the first account — it
   becomes the admin. Confirm your models appear in the model picker.
2. **Langfuse** at <http://localhost:3001>. Sign up, create a project, copy
   the public and secret keys. Then set `LANGFUSE_DISABLE_SIGNUP=true` in
   `.env` and `docker compose up -d langfuse`.
3. **Qdrant** at <http://localhost:6333/dashboard>. Should be empty.
4. **Grafana** at <http://localhost:3002>. Prometheus is pre-provisioned as
   the default datasource. Import dashboard ID **14574** for the
   nvidia_gpu_exporter dashboard.

### Verify GPU metrics are flowing

```powershell
curl http://localhost:9835/metrics | Select-String "nvidia_smi_memory_used_bytes"
```

You should see two series, one per card. GPU 0 (display) will show ~1.7 GB
used; GPU 1 should be near zero until you load a model.

---

## What to actually build here

The stack is the easy part. The portfolio value is in what you do with it.

**1. A RAG pipeline with a real eval harness.** Ingest a document set you
know well. Build a golden question set of 30–50 items with expected answers.
Run it through Langfuse and score retrieval separately from generation —
most failures are retrieval failures, and you cannot see that without
separating them.

**2. A chunking experiment.** Same corpus, four chunking strategies (fixed
512, fixed 1024, semantic, recursive with overlap). Same eval set. Chart the
retrieval hit rate. This one experiment teaches more than any course.

**3. A cost model.** Instrument token counts through Langfuse, then build a
spreadsheet projecting cost per 1000 queries at hosted API rates versus
self-hosted amortised over hardware. This is the artifact that closes
consulting engagements.

**4. A GPU utilisation dashboard.** Two cards, one idle and one working,
makes a more interesting Grafana panel than a single card. Add memory
pressure, thermals, and power draw. This is your platform-layer proof.

---

## Operations

```powershell
docker compose ps                     # status
docker compose logs -f langfuse       # follow one service
docker compose restart open-webui     # restart one service
docker compose down                   # stop, keep volumes
docker compose down -v                # stop and DELETE ALL DATA
docker compose pull; docker compose up -d   # update images
```

Back up before any risky change:

```powershell
docker run --rm -v ai-lab_qdrant-data:/data -v ${PWD}:/backup alpine `
  tar czf /backup/qdrant-backup.tar.gz -C /data .
```

---

## Troubleshooting

**Open WebUI shows no models.** Ollama is bound to localhost. Set
`OLLAMA_HOST=0.0.0.0:11434` and restart it. Test from inside the container:
`docker compose exec open-webui curl http://host.docker.internal:11434/api/tags`

**Langfuse restart loop.** Almost always `LANGFUSE_ENCRYPTION_KEY` not being
exactly 64 hex characters. Check `docker compose logs langfuse`.

**GPU exporter returns nothing.** The container needs the WSL driver libraries.
Verify the host first with `wsl nvidia-smi`. If that fails, GPU passthrough to
WSL2 is not working and the metrics profile will not either — run the exporter
natively on Windows instead and point Prometheus at
`host.docker.internal:9835`.

**Port already in use.** Every port is overridable in `.env`.

**Everything is slow.** Expected. Two 3.5 GB Maxwell cards and DDR4-2133 are
not fast. The point of this lab is the platform layer, not throughput. Rent a
GPU when you need speed.

---

## Resource footprint

Idle, the core stack uses roughly 2–3 GB RAM and very little CPU. With the
metrics profile, add about 500 MB. On 48 GB you have ample room to run a
model on CPU alongside all of it.
