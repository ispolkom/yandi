#!/usr/bin/env bash
# Собирает самодостаточный llama-server (llama.cpp) для ВШИВАНИЯ в бинарник узла. Запускается один раз при сборке релиза (нужен интернет, git, cmake, g++).
# Результат: dist/llama-server. Дальше:  YANDI_EMBED_LLAMA_SERVER=$PWD/dist/llama-server cargo build --release   (в каталоге node/)
# Пользователю узла ничего из этого делать не нужно: движок внутри бинарника и запускается сам.
# Видеокарта NVIDIA (нужен CUDA):  GGML_CUDA=ON ./scripts/build-llama-server.sh      Зафиксировать версию:  LLAMA_CPP_REF=<тег или коммит> ./scripts/build-llama-server.sh
set -euo pipefail
cd "$(dirname "$0")/.."
WORK="${LLAMA_CPP_DIR:-build/llama.cpp}"
if [ ! -d "$WORK/.git" ]; then
  mkdir -p "$(dirname "$WORK")"
  git clone https://github.com/ggml-org/llama.cpp "$WORK"
fi
( cd "$WORK" && git fetch -q --tags origin && git checkout -q "${LLAMA_CPP_REF:-master}" )
CMAKE_ARGS=(-DBUILD_SHARED_LIBS=OFF -DCMAKE_BUILD_TYPE=Release)
[ "${GGML_CUDA:-OFF}" = "ON" ] && CMAKE_ARGS+=(-DGGML_CUDA=ON)
cmake -S "$WORK" -B "$WORK/build" "${CMAKE_ARGS[@]}"
cmake --build "$WORK/build" --config Release -j --target llama-server
mkdir -p dist
cp "$WORK/build/bin/llama-server" dist/llama-server
( cd "$WORK" && echo "llama.cpp $(git rev-parse --short HEAD)" ) > dist/llama-server.version
echo "ГОТОВО: dist/llama-server ($(cat dist/llama-server.version))"
echo "Теперь: cd node && YANDI_EMBED_LLAMA_SERVER=$PWD/dist/llama-server cargo build --release"
