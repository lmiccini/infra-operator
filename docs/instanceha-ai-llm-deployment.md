# InstanceHA AI - LLM Deployment Guide

This guide covers how to enable the AI-powered natural language interface for
InstanceHA by deploying an LLM backend and configuring the InstanceHA pod to
use it.

## Overview

InstanceHA includes a chat interface accessible via `oc rsh <pod> instanceha-chat`.
By default it supports structured commands (`status`, `services`, `alerts`, etc.).
When an LLM backend is configured, any message that doesn't match a built-in
command is routed to an AI agent that understands natural language, calls the
appropriate tools, and returns a conversational answer.

The AI agent can:
- Query and explain cluster state in plain language
- Diagnose evacuation failures and suggest remediation
- Check cluster capacity and correlate events
- Propose recovery actions (with mandatory operator approval for destructive ops)

Two LLM backends are supported:
- **Remote** (recommended): Any OpenAI-compatible API (Ollama, vLLM, TGI, LiteLLM)
- **Local**: On-pod inference via llama-cpp-python with GGUF model files (air-gap compatible)

---

## Quick Start with Ollama

Ollama is the simplest way to deploy an LLM in your cluster. It runs on CPU
(no GPU required), supports many models, and exposes an OpenAI-compatible API.

### Prerequisites

If pulling from Docker Hub on OpenShift, you may need to add it as an insecure
registry:

```bash
oc patch image.config.openshift.io/cluster --type=merge \
  -p '{"spec":{"registrySources":{"insecureRegistries":["docker.io"]}}}'

# Wait for MachineConfigPool rollout to complete
oc get mcp -w
```

### 1. Deploy Ollama

Create `ollama.yaml`:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ollama
  namespace: openstack
spec:
  replicas: 1
  selector:
    matchLabels:
      app: ollama
  template:
    metadata:
      labels:
        app: ollama
    spec:
      containers:
      - name: ollama
        image: docker.io/ollama/ollama:latest
        env:
        # HOME and OLLAMA_MODELS must be set to writable paths
        # because OpenShift runs containers as non-root
        - name: HOME
          value: /tmp/ollama-home
        - name: OLLAMA_MODELS
          value: /models
        ports:
        - containerPort: 11434
        resources:
          requests:
            memory: "4Gi"
            cpu: "2"
          limits:
            memory: "8Gi"
            cpu: "4"
        volumeMounts:
        - name: models
          mountPath: /models
        - name: home
          mountPath: /tmp/ollama-home
      volumes:
      - name: models
        persistentVolumeClaim:
          claimName: ollama-models
      - name: home
        emptyDir: {}
---
apiVersion: v1
kind: Service
metadata:
  name: ollama
  namespace: openstack
spec:
  selector:
    app: ollama
  ports:
  - port: 11434
    targetPort: 11434
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ollama-models
  namespace: openstack
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 20Gi
```

**Note on OpenShift permissions**: Ollama expects to write to `$HOME/.ollama`
for key generation and to store model files. OpenShift's default security
context runs containers as a random non-root UID, so the default paths are not
writable. The `HOME` and `OLLAMA_MODELS` env vars redirect these to writable
locations (`emptyDir` for ephemeral data, PVC for models).

Apply and pull a model:

```bash
oc apply -f ollama.yaml
oc wait --for=condition=ready pod -l app=ollama --timeout=120s

# Pull a model (downloads to the PVC, persists across restarts)
oc exec deployment/ollama -- ollama pull llama3.1:8b
```

### 2. Configure InstanceHA

```bash
oc set env deployment/instanceha \
  AI_ENABLED=True \
  AI_ENDPOINT=http://ollama.openstack.svc:11434 \
  AI_MODEL=llama3.1:8b
```

### 3. Test

```bash
oc rsh <instanceha-pod> instanceha-chat
> which hosts are currently down?
> check capacity for evacuating compute-1
> what happened in the last 30 minutes?
```

---

## Deploying with vLLM (GPU)

vLLM provides higher throughput and lower latency but requires a GPU node.
On OpenShift with RHOAI, you can use `quay.io/modh/vllm:rhoai-2.20-cuda`
instead of the Docker Hub image.

### Prerequisites

- A Kubernetes node with an NVIDIA GPU and the GPU operator installed
- A Hugging Face token (for gated models like Llama)

### 1. Create the HF token secret

```bash
oc create secret generic hf-token \
  --namespace openstack \
  --from-literal=token=hf_your_token_here
```

### 2. Deploy vLLM

Create `vllm.yaml`:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: vllm
  namespace: openstack
spec:
  replicas: 1
  selector:
    matchLabels:
      app: vllm
  template:
    metadata:
      labels:
        app: vllm
    spec:
      containers:
      - name: vllm
        image: docker.io/vllm/vllm-openai:latest
        args:
        - "--model"
        - "meta-llama/Llama-3.1-8B-Instruct"
        - "--max-model-len"
        - "4096"
        ports:
        - containerPort: 8000
        resources:
          limits:
            nvidia.com/gpu: 1
            memory: "16Gi"
        env:
        - name: HUGGING_FACE_HUB_TOKEN
          valueFrom:
            secretKeyRef:
              name: hf-token
              key: token
        volumeMounts:
        - name: model-cache
          mountPath: /root/.cache/huggingface
      tolerations:
      - key: nvidia.com/gpu
        operator: Exists
        effect: NoSchedule
      volumes:
      - name: model-cache
        persistentVolumeClaim:
          claimName: vllm-model-cache
---
apiVersion: v1
kind: Service
metadata:
  name: vllm
  namespace: openstack
spec:
  selector:
    app: vllm
  ports:
  - port: 8000
    targetPort: 8000
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: vllm-model-cache
  namespace: openstack
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 30Gi
```

```bash
oc apply -f vllm.yaml
oc wait --for=condition=ready pod -l app=vllm --timeout=600s
```

### 3. Configure InstanceHA

```bash
oc set env deployment/instanceha \
  AI_ENABLED=True \
  AI_ENDPOINT=http://vllm.openstack.svc:8000 \
  AI_MODEL=meta-llama/Llama-3.1-8B-Instruct
```

---

## Local Model (Air-Gap)

For air-gapped environments where the pod cannot reach an external LLM service,
use the local backend with a GGUF model file mounted via PVC.

### Prerequisites

- The `instanceha-ai` container image (includes `llama-cpp-python`)
- A GGUF model file (e.g., from Hugging Face, Q4_K_M quantization recommended)

### 1. Prepare the model PVC

```bash
# Create a PVC for the model
oc apply -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: instanceha-model
  namespace: openstack
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 10Gi
EOF

# Copy the model file into the PVC (using a temporary pod)
oc run model-loader --image=ubi9 --restart=Never \
  --overrides='{"spec":{"containers":[{"name":"loader","image":"ubi9","command":["sleep","3600"],"volumeMounts":[{"name":"model","mountPath":"/models"}]}],"volumes":[{"name":"model","persistentVolumeClaim":{"claimName":"instanceha-model"}}]}}'

oc wait --for=condition=ready pod/model-loader --timeout=60s
oc cp ./your-model.Q4_K_M.gguf model-loader:/models/model.gguf
oc delete pod model-loader
```

### 2. Mount the PVC in the InstanceHA deployment

Patch the deployment to add the model volume:

```bash
oc patch deployment instanceha --type=json -p='[
  {"op": "add", "path": "/spec/template/spec/volumes/-",
   "value": {"name": "ai-model", "persistentVolumeClaim": {"claimName": "instanceha-model"}}},
  {"op": "add", "path": "/spec/template/spec/containers/0/volumeMounts/-",
   "value": {"name": "ai-model", "mountPath": "/var/lib/instanceha/models", "readOnly": true}}
]'
```

### 3. Configure InstanceHA

```bash
oc set env deployment/instanceha \
  AI_ENABLED=True \
  AI_MODEL_PATH=/var/lib/instanceha/models/model.gguf
```

### Recommended models for local inference

| Model | Size (Q4_K_M) | RAM Required | Quality |
|-------|---------------|-------------|---------|
| Llama 3.1 8B Instruct | ~4.7 GB | ~6 GB | Best |
| Mistral 7B Instruct | ~4.4 GB | ~6 GB | Good |
| Phi-3 Mini 3.8B | ~2.3 GB | ~4 GB | Acceptable (lighter) |

---

## Environment Variables Reference

| Variable | Description | Default |
|----------|-------------|---------|
| `AI_ENABLED` | Set to `True` to enable the LLM agent | `False` |
| `AI_ENDPOINT` | URL for an OpenAI-compatible API (remote backend) | - |
| `AI_MODEL` | Model name for the remote endpoint | `default` |
| `AI_API_KEY` | API key for the remote endpoint (if required) | - |
| `AI_MODEL_PATH` | Path to a local GGUF model file (local backend) | - |
| `AI_N_CTX` | Context window size (local backend only) | `4096` |
| `AI_N_THREADS` | Number of CPU threads for inference (local backend only) | `4` |

If both `AI_MODEL_PATH` and `AI_ENDPOINT` are set, the local backend takes
precedence.

---

## Backend Comparison

| | Ollama (remote) | vLLM (remote) | Local (GGUF) |
|---|---|---|---|
| Setup complexity | Low | Moderate | Moderate |
| GPU required | No | Yes | No |
| Latency (8B model, CPU) | 2-10s | N/A | 5-30s |
| Latency (8B model, GPU) | <1s | <1s | N/A |
| Tool calling support | Yes | Yes | Yes |
| Air-gap compatible | Needs initial pull | Needs initial download | Yes |
| Memory footprint | ~6 GB (separate pod) | ~16 GB GPU VRAM | ~6 GB (in InstanceHA pod) |

---

## Verifying the Integration

### Check if the LLM engine is loaded

Look at the InstanceHA pod logs after setting the environment variables:

```bash
oc logs deployment/instanceha | grep -i "llm\|engine\|ai"
```

You should see:
```
LLM engine ready: {'backend': 'remote', 'endpoint': 'http://ollama...', 'model': 'llama3.1:8b'}
```

### Check the remote endpoint is reachable

```bash
# From within the cluster
oc exec deployment/instanceha -- \
  python3 -c "import urllib.request; urllib.request.urlopen('http://ollama.openstack.svc:11434/v1/models', timeout=5); print('OK')"
```

### Test natural language queries

```bash
oc rsh <instanceha-pod> instanceha-chat
> what is the current state of the cluster?
> are there any alerts I should be aware of?
> explain the last fencing event
```

If the LLM is not configured, unrecognized commands return:
```
Unknown command: 'what'. Type 'help' for available commands.
```

When the LLM is configured, the same input produces a natural language answer
based on live cluster data.

---

## Troubleshooting

### "AI engine disabled" in logs

`AI_ENABLED` is not set to `True`. Check:
```bash
oc set env deployment/instanceha --list | grep AI_
```

### "Remote LLM inference failed: connection refused"

The Ollama/vLLM service is not reachable. Verify:
```bash
oc get svc -n openstack | grep -E "ollama|vllm"
oc get pods -l app=ollama   # or app=vllm
```

### "Model not loaded" (local backend)

The GGUF file path is wrong or the volume is not mounted:
```bash
oc exec deployment/instanceha -- ls -la /var/lib/instanceha/models/
```

### Slow responses

- On CPU, 8B models take 5-30 seconds per response. This is normal.
- For faster responses, use a GPU-backed vLLM deployment or a smaller model
  (`phi3:mini` on Ollama).
- Reduce `AI_N_CTX` to lower memory usage and slightly improve speed.

### Tool calling not working

Some models have limited tool/function calling support. Recommended models
with good tool calling:
- `llama3.1:8b` (Ollama) -- best tool calling support
- `mistral:7b-instruct` (Ollama) -- good tool calling
- `meta-llama/Llama-3.1-8B-Instruct` (vLLM) -- same model, full precision
