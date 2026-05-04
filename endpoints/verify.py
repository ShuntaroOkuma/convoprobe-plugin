"""Day 1 verification endpoint (T03, T04).

Calls Reverse Invocation APIs against the configured target_app and
model provider to validate:
- T03: session.app.chat.invoke multi-turn continuity (conversation_id threading)
- T04: session.model.llm.invoke availability (ADR-007 option C)

Hidden endpoint. Remove before Marketplace submission.
"""
import json
import time
import traceback
from collections.abc import Mapping

from werkzeug import Request, Response

from dify_plugin.interfaces.endpoint import Endpoint


class VerifyEndpoint(Endpoint):
    def _invoke(self, r: Request, values: Mapping, settings: Mapping) -> Response:
        body = r.get_json(force=True, silent=True) or {}

        target_app = settings.get("target_app") or {}
        app_id = target_app.get("app_id") if isinstance(target_app, dict) else None
        if not app_id:
            return _json({"ok": False, "error": "target_app not configured"}, 400)

        queries = body.get("queries") or [
            "Hello, what is your name?",
            "Can you repeat what I just asked?",
            "Thanks, what's 2+2?",
        ]

        results = {
            "t03_reverse_invocation": _run_t03(self.session, app_id, queries),
            "t04_judge_llm": _run_t04(self.session, body.get("judge_model")),
        }
        return _json(results, 200)


def _run_t03(session, app_id: str, queries: list[str]) -> dict:
    outcome: dict = {
        "ok": False,
        "turns": [],
        "conversation_id": None,
        "error": None,
    }
    try:
        conversation_id: str | None = None
        for i, q in enumerate(queries, start=1):
            started = time.time()
            response = session.app.chat.invoke(
                app_id=app_id,
                query=q,
                inputs={},
                response_mode="blocking",
                conversation_id=conversation_id,
            )
            elapsed_ms = int((time.time() - started) * 1000)

            answer = ""
            if isinstance(response, dict):
                answer = (
                    response.get("answer")
                    or (response.get("data") or {}).get("answer")
                    or ""
                )
                new_conv_id = (
                    response.get("conversation_id")
                    or (response.get("data") or {}).get("conversation_id")
                )
                if new_conv_id:
                    conversation_id = new_conv_id

            outcome["turns"].append({
                "turn": i,
                "query": q,
                "answer_preview": (answer or "")[:200],
                "answer_len": len(answer or ""),
                "latency_ms": elapsed_ms,
                "conversation_id": conversation_id,
                "raw_keys": sorted(list(response.keys())) if isinstance(response, dict) else None,
            })
        outcome["conversation_id"] = conversation_id
        outcome["ok"] = True
    except Exception as e:
        outcome["error"] = f"{type(e).__name__}: {e}"
        outcome["traceback"] = traceback.format_exc()[-1500:]
    return outcome


def _run_t04(session, judge_model: dict | None) -> dict:
    outcome: dict = {
        "ok": False,
        "response_preview": None,
        "error": None,
    }
    model_config = judge_model or {
        "provider": "openai",
        "model": "gpt-4o-mini",
        "mode": "chat",
        "completion_params": {"temperature": 0.0, "max_tokens": 100},
    }
    try:
        from dify_plugin.entities.model.message import SystemPromptMessage, UserPromptMessage

        result = session.model.llm.invoke(
            model_config=model_config,
            prompt_messages=[
                SystemPromptMessage(content="You are a test judge. Respond only with PASS."),
                UserPromptMessage(content="This is a verification ping. Please answer with the word PASS only."),
            ],
            stream=False,
        )
        text = ""
        if hasattr(result, "message") and getattr(result.message, "content", None):
            content = result.message.content
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "".join(getattr(p, "data", "") or str(p) for p in content)
        outcome["response_preview"] = text[:300]
        outcome["ok"] = True
    except Exception as e:
        outcome["error"] = f"{type(e).__name__}: {e}"
        outcome["traceback"] = traceback.format_exc()[-1500:]
    return outcome


def _json(obj: dict, status: int) -> Response:
    return Response(
        response=json.dumps(obj, ensure_ascii=False, default=str),
        status=status,
        content_type="application/json",
    )
