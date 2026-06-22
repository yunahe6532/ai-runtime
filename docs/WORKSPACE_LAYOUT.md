# 워크스페이스 / 디렉토리 정리

`/home/yunahe`는 **공용 홈**이고, 이 프로젝트 루트는 **`/home/yunahe/ai-runtime/cursor-local-llm`** 입니다.

---

## 원칙

| 위치 | 용도 | 이동 |
|------|------|------|
| `~/ai-runtime/cursor-local-llm/` | **프로젝트 코드·설정·tmp·docs** | 여기가 기준 |
| `~/models/` | GGUF 대용량 (공용 자산) | 프로젝트 밖 유지 (용량·공유) |
| `~/.cursor`, `~/.codex` | IDE/에이전트 설정 | 건드리지 않음 |
| `~/ai-runtime/` | 런타임/인프라 모음 | cursor-local-llm 외 다른 서비스 가능 |

**Cursor Workspace**는 가능하면 `cursor-local-llm`으로 열 것.  
`/home/yunahe` 루트를 workspace로 쓰면 memory_store·캡처가 홈 전체와 섞입니다.

---

## 프로젝트 내부 구조 (목표)

```text
cursor-local-llm/
├── configs/
│   ├── model-profiles.env    # 프로필 (qwen3_coder | qwen3_6)
│   └── models.manifest.json  # 모델 경로 참조
├── docs/
│   ├── ARCHITECTURE.md
│   └── WORKSPACE_LAYOUT.md
├── router/                   # Python router
├── scripts/                  # 벤치, 다운로드, switch-model
├── tmp/                      # 캡처, 벤치 결과, context-cache (gitignore)
│   ├── cursor-captures/
│   ├── context-cache/
│   └── benchmark-*.json
├── docker-compose.yml
├── .env
└── handoff.md
```

---

## 홈 루트 (`~/`) 정리 가이드

현재 루트에 **이 프로젝트 전용으로 보이는 loose 파일은 거의 없음**.  
다만 아래는 정리 후보입니다 (수동 확인 후 삭제/이동):

| 경로 | 판단 |
|------|------|
| `~/META-INF/` | Java 아티팩트 잔여 — 프로젝트 무관, 삭제 가능 |
| `~/file1` | 테스트 파일 — 삭제 가능 |
| `~/react-preview/` | 별도 실험 — 필요 시 `~/ai-runtime/` 아래로 |
| `~/instantclient_*` | Oracle 클라이언트 — DB 작업용, 유지 |
| `~/models/` | **유지** — 모든 LLM GGUF 공용 저장소 |

프로젝트 산출물은 **절대 홈 루트에 두지 말 것**:

- `results.json`, `current_state.json`, `*.flow.json` → `tmp/` 아래만
- 벤치 결과 → `tmp/benchmark-*.json`

---

## 모델 경로 (공용 vs 프로젝트)

모델 파일은 **이동하지 않고** `~/models/`에 둡니다 (17–22GB × N).

프로젝트에서는 **참조만** 관리:

- `configs/model-profiles.env`
- `configs/models.manifest.json`

```bash
# 프로젝트에서 모델 위치 확인
cat configs/models.manifest.json
```

---

## Cursor workspace 권장

```text
Workspace Path: /home/yunahe/ai-runtime/cursor-local-llm
```

이렇게 하면:

- `memory_store` project key가 프로젝트 단위로 고정
- 캡처/flow가 해당 repo 맥락만 포함
- 루트 홈 디렉토리 오염 방지
