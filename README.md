# AI Runtime — Cursor Local LLM Reference Implementation

> **Context Runtime v1** middleware for Cursor + local llama.cpp.  
> Product vision: [docs/VISION.md](./docs/VISION.md) · Module tiers: [docs/MODULE_MAP.md](./docs/MODULE_MAP.md)

WSL 환경에서 `llama.cpp server`를 Docker로 실행하고, OpenAI-compatible API(`http://localhost:8080/v1`)로 Cursor에 연결하는 **Runtime Middleware**입니다.

## Quick Start

## 폴더 구조

```text
~/models/
├── qwen/
├── exaone/
└── gptoss/

~/ai-runtime/cursor-local-llm/
├── docker-compose.yml
├── .env
├── .env.example
├── README.md
├── configs/
│   └── model-map.env
└── scripts/
    ├── start.sh
    ├── stop.sh
    ├── logs.sh
    ├── test-api.sh
    ├── benchmark.sh
    ├── download-model.sh
    └── switch-model.sh
```

## 환경 변수

기본값은 `.env`에 정의되어 있고, 런타임은 `MODEL_FILE`만 보고 모델을 마운트합니다.

예시:

```env
MODEL_FILE=~/models/qwen/Qwen3.6-27B-Q4_K_M.gguf
MODEL_URL=https://huggingface.co/sm54/Qwen3.6-27B-Q4_K_M-GGUF/resolve/main/Qwen3.6-27B-Q4_K_M.gguf?download=true
PORT=8080
CONTEXT_SIZE=8192
GPU_LAYERS=-1
```

## 모델 추가 방법

1. 모델 디렉토리 생성 (필요 시):
   - `mkdir -p ~/models/<model-name>`
2. `.env`의 `MODEL_FILE`, `MODEL_URL` 설정
3. 다운로드:
   - `cd ~/ai-runtime/cursor-local-llm`
   - `./scripts/download-model.sh`

`download-model.sh`는 `aria2c` 우선, 없으면 `wget`, 없으면 `curl`을 사용합니다.

## 모델 교체 방법

빠른 교체:

```bash
cd ~/ai-runtime/cursor-local-llm
./scripts/switch-model.sh qwen
./scripts/switch-model.sh exaone
./scripts/switch-model.sh gptoss
```

이 스크립트는 `.env`의 `MODEL_FILE`만 변경합니다.  
다른 URL이 필요하면 `.env`의 `MODEL_URL`도 수동으로 변경하세요.

## Docker 실행

```bash
cd ~/ai-runtime/cursor-local-llm
./scripts/start.sh
./scripts/stop.sh
./scripts/logs.sh
```

## API 테스트

```bash
cd ~/ai-runtime/cursor-local-llm
./scripts/test-api.sh
```

## Benchmark 사용법

```bash
cd ~/ai-runtime/cursor-local-llm
./scripts/benchmark.sh
```

출력 항목:
- `/v1/models` 응답
- `chat/completions` 간단 요청
- TTFT(ms)
- tokens/sec

## Cursor 설정

- OpenAI Base URL: `http://localhost:8080/v1`
- API Key: 아무 값 (예: `dummy-key`)

## 문제 해결

```bash
nvidia-smi
cd ~/ai-runtime/cursor-local-llm && docker compose ps
cd ~/ai-runtime/cursor-local-llm && docker compose logs -f
```
