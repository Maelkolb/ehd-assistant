"""An open-weights model behind an OpenAI-compatible endpoint, on Modal.

A stand-in for "a server with local inference": Alex talks to it exactly as it
would to vLLM, Ollama or llama.cpp on your own hardware.

    export ALEX_LOCAL_LLM_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(24))")
    modal deploy scripts/modal_local_llm.py        # prints the endpoint URL
    LOCAL_LLM_BASE_URL=https://…modal.run/v1 LOCAL_LLM_API_KEY=$ALEX_LOCAL_LLM_KEY \
      LOCAL_LLM_MODEL=qwen3.8-27b LOCAL_LLM_CONTEXT_TOKENS=64000 alex ask "…"
    modal app stop alex-local-llm                  # stop paying for the GPU
"""

import os
import subprocess

import modal

MODEL = "Qwen/Qwen3.8-27B-FP8"
SERVED_NAME = "qwen3.8-27b"
CONTEXT_TOKENS = 65536
PORT = 8000

image = (
    modal.Image.from_registry("nvidia/cuda:12.8.1-devel-ubuntu22.04", add_python="3.12")
    .entrypoint([])
    .uv_pip_install("vllm==0.30.0", "huggingface-hub[hf_xet]")
    .env({"HF_XET_HIGH_PERFORMANCE": "1"})
)
app = modal.App("alex-local-llm")
weights = modal.Volume.from_name("alex-local-llm-weights", create_if_missing=True)
engine_cache = modal.Volume.from_name("alex-local-llm-vllm-cache", create_if_missing=True)


@app.function(
    image=image,
    gpu="L40S",
    volumes={"/root/.cache/huggingface": weights, "/root/.cache/vllm": engine_cache},
    secrets=[modal.Secret.from_dict({"VLLM_API_KEY": os.environ.get("ALEX_LOCAL_LLM_KEY", "")})],
    scaledown_window=300,
    timeout=3600,
    max_containers=1,
)
@modal.concurrent(max_inputs=8)
@modal.web_server(port=PORT, startup_timeout=900)
def serve():
    subprocess.Popen(
        [
            "vllm",
            "serve",
            MODEL,
            "--served-model-name",
            SERVED_NAME,
            "--host",
            "0.0.0.0",
            "--port",
            str(PORT),
            "--max-model-len",
            str(CONTEXT_TOKENS),
            "--max-num-seqs",
            "16",
            "--kv-cache-dtype",
            "fp8",
            "--language-model-only",
            "--reasoning-parser",
            "qwen3",
            "--enable-auto-tool-choice",
            "--tool-call-parser",
            "qwen3_xml",
            "--enable-prefix-caching",
        ]
    )
