#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=/workspace/llama
SRC="$ROOT/llama.cpp"
BUILD="$SRC/build"
MODEL_DIR="$ROOT/models"
MODEL="$MODEL_DIR/Qwen3.8-27B-UD-Q8_K_XL.gguf"
MODEL_URL="https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/resolve/main/Qwen3.8-27B-UD-Q8_K_XL.gguf"
KEY_FILE="$ROOT/api_key.txt"

export CUDA_HOME=/usr/local/cuda-13.0
export CUDACXX="$CUDA_HOME/bin/nvcc"
export PATH="$CUDA_HOME/bin:$PATH"
export LD_LIBRARY_PATH="$CUDA_HOME/lib64:$CUDA_HOME/targets/x86_64-linux/lib:${LD_LIBRARY_PATH:-}"

mkdir -p "$ROOT" "$MODEL_DIR"
test -s "$KEY_FILE"

ARCHIVE="$ROOT/llama.cpp-master.tar.gz"
wget --tries=8 --timeout=30 --retry-connrefused \
  https://github.com/ggml-org/llama.cpp/archive/refs/heads/master.tar.gz \
  -O "$ARCHIVE.part"
test -s "$ARCHIVE.part"
mv -f "$ARCHIVE.part" "$ARCHIVE"
rm -rf "$SRC" "$ROOT/llama.cpp-master"
tar -xzf "$ARCHIVE" -C "$ROOT"
mv "$ROOT/llama.cpp-master" "$SRC"
rm -f "$ARCHIVE"

cmake -S "$SRC" -B "$BUILD" -G Ninja \
  -DGGML_CUDA=ON \
  -DGGML_NATIVE=OFF \
  -DCUDAToolkit_ROOT="$CUDA_HOME" \
  -DCMAKE_CUDA_COMPILER="$CUDACXX" \
  -DCMAKE_CUDA_ARCHITECTURES=80 \
  -DCMAKE_BUILD_TYPE=Release
cmake --build "$BUILD" --config Release -j "${MAX_JOBS:-12}"

if [ ! -s "$MODEL" ]; then
  wget --tries=8 --timeout=30 --retry-connrefused --continue "$MODEL_URL" -O "$MODEL.part"
  test -s "$MODEL.part"
  mv -f "$MODEL.part" "$MODEL"
fi

if [ ! -x "$ROOT/cloudflared" ]; then
  wget --tries=5 --timeout=30 \
    https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
    -O "$ROOT/cloudflared"
  chmod +x "$ROOT/cloudflared"
fi

cat > "$ROOT/start-qwen.sh" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
ulimit -n 65535 2>/dev/null || true
ROOT=/workspace/llama
export CUDA_HOME=/usr/local/cuda-13.0
export PATH="$CUDA_HOME/bin:$PATH"
export LD_LIBRARY_PATH="$CUDA_HOME/lib64:$CUDA_HOME/targets/x86_64-linux/lib:${LD_LIBRARY_PATH:-}"
export CUDA_DEVICE_ORDER=PCI_BUS_ID
export MALLOC_ARENA_MAX=2
exec "$ROOT/llama.cpp/build/bin/llama-server" \
  -m "$ROOT/models/Qwen3.8-27B-UD-Q8_K_XL.gguf" \
  --host 127.0.0.1 --port 18080 \
  -ngl auto \
  -c 262144 --parallel 1 \
  --fit on --fit-target 2048 \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  -b 512 -ub 256 \
  --spec-type draft-mtp --spec-draft-n-max 2 \
  --flash-attn auto --load-mode auto --jinja \
  --api-key "$(tr -d '\r\n' < "$ROOT/api_key.txt")"
EOF
chmod +x "$ROOT/start-qwen.sh"

"$BUILD/bin/llama-server" --version
du -h "$MODEL"
echo "LLAMA SETUP DONE"
