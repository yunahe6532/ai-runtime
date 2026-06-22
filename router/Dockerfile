FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    httpx==0.28.1 \
    opentelemetry-api==1.29.0 \
    opentelemetry-sdk==1.29.0 \
    opentelemetry-exporter-otlp-proto-http==1.29.0

COPY main.py capture.py chat_fast.py context_cache.py intent_router.py cursor_reasoning.py runtime_inspector.py vl_pass.py prompt_builder.py context_budget.py context_need.py artifact_analyzer.py artifact_excerpt.py explorer_trace.py qwen_request.py message_index.py failed_action.py coverage_checker.py recovery_scheduler.py dynamic_context_scheduler.py runtime_turn_log.py /app/
COPY runtime_core/ /app/runtime_core/
COPY runtime_kernel/ /app/runtime_kernel/
COPY agent_brain/ /app/agent_brain/
COPY observability/ /app/observability/
COPY reference/ /app/reference/
COPY integrations/ /app/integrations/
COPY legacy/ /app/legacy/
COPY adapters/ /app/adapters/

ENV ROUTER_HOST=0.0.0.0
ENV ROUTER_PORT=8080
ENV FAST_URL=http://llama-fast:8081
ENV LONG_URL=http://llama-long:8082
ENV TOKEN_THRESHOLD=20000
ENV LONG_IDLE_TTL_SEC=900
ENV ROUTER_EXCLUSIVE=1
ENV DEFAULT_BACKEND=fast
ENV BACKEND_READY_TIMEOUT_SEC=300
ENV DOCKER_SOCK=/var/run/docker.sock
ENV DYNAMIC_BUDGET=1
ENV PROMPT_ENFORCER=1

EXPOSE 8080
CMD ["python", "-u", "/app/main.py"]
