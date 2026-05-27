"""LLM engine abstraction — local GGUF or remote OpenAI-compatible API."""

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolCall:
    name: str
    arguments: Dict[str, Any]
    call_id: str = ""


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: Optional[Dict[str, int]] = None

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


class LLMEngine(ABC):

    @abstractmethod
    def chat(self, messages: List[Dict], tools: Optional[List[Dict]] = None,
             temperature: float = 0.1, max_tokens: int = 2048) -> LLMResponse:
        pass

    @abstractmethod
    def is_available(self) -> bool:
        pass

    @abstractmethod
    def model_info(self) -> Dict[str, Any]:
        pass


class LocalEngine(LLMEngine):

    def __init__(self, model_path: str, n_ctx: int = 4096, n_threads: int = 4,
                 verbose: bool = False):
        self._model_path = model_path
        self._n_ctx = n_ctx
        self._n_threads = n_threads
        self._verbose = verbose
        self._llm = None
        self._model_name = ""

    def load(self) -> bool:
        try:
            from llama_cpp import Llama
            logging.info("Loading local model from %s ...", self._model_path)
            self._llm = Llama(
                model_path=self._model_path,
                n_ctx=self._n_ctx,
                n_threads=self._n_threads,
                verbose=self._verbose,
                chat_format="chatml-function-calling",
            )
            import os
            self._model_name = os.path.basename(self._model_path)
            logging.info("Model loaded: %s (ctx=%d, threads=%d)",
                         self._model_name, self._n_ctx, self._n_threads)
            return True
        except ImportError:
            logging.error("llama-cpp-python is not installed")
            return False
        except Exception as e:
            logging.error("Failed to load local model: %s", e)
            return False

    def chat(self, messages: List[Dict], tools: Optional[List[Dict]] = None,
             temperature: float = 0.1, max_tokens: int = 2048) -> LLMResponse:
        if not self._llm:
            return LLMResponse(content="Model not loaded", finish_reason="error")

        try:
            kwargs = {
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }

            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"

            result = self._llm.create_chat_completion(**kwargs)
            choice = result["choices"][0]
            message = choice["message"]

            tool_calls = _parse_tool_calls(message)

            return LLMResponse(
                content=message.get("content", "") or "",
                tool_calls=tool_calls,
                finish_reason=choice.get("finish_reason", "stop"),
                usage=result.get("usage"),
            )

        except Exception as e:
            logging.error("Local LLM inference failed: %s", e)
            return LLMResponse(content=f"Inference error: {e}", finish_reason="error")

    def is_available(self) -> bool:
        return self._llm is not None

    def model_info(self) -> Dict[str, Any]:
        return {
            "backend": "local",
            "model_path": self._model_path,
            "model_name": self._model_name,
            "loaded": self._llm is not None,
            "n_ctx": self._n_ctx,
            "n_threads": self._n_threads,
        }


class RemoteEngine(LLMEngine):

    def __init__(self, endpoint: str, api_key: str = "",
                 model: str = "default", timeout: int = 60):
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout

    def chat(self, messages: List[Dict], tools: Optional[List[Dict]] = None,
             temperature: float = 0.1, max_tokens: int = 2048) -> LLMResponse:
        try:
            import urllib.request

            body = {
                "model": self._model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if tools:
                body["tools"] = tools
                body["tool_choice"] = "auto"

            data = json.dumps(body).encode("utf-8")
            url = f"{self._endpoint}/v1/chat/completions"

            headers = {"Content-Type": "application/json"}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"

            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))

            choice = result["choices"][0]
            message = choice["message"]

            tool_calls = _parse_tool_calls(message)

            return LLMResponse(
                content=message.get("content", "") or "",
                tool_calls=tool_calls,
                finish_reason=choice.get("finish_reason", "stop"),
                usage=result.get("usage"),
            )

        except Exception as e:
            logging.error("Remote LLM inference failed: %s", e)
            return LLMResponse(content=f"Remote inference error: {e}", finish_reason="error")

    def is_available(self) -> bool:
        try:
            import urllib.request
            url = f"{self._endpoint}/v1/models"
            headers = {}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=5):
                return True
        except Exception:
            return False

    def model_info(self) -> Dict[str, Any]:
        return {
            "backend": "remote",
            "endpoint": self._endpoint,
            "model": self._model,
        }


def _parse_tool_calls(message: dict) -> List[ToolCall]:
    tool_calls = []
    if "tool_calls" in message and message["tool_calls"]:
        for tc in message["tool_calls"]:
            func = tc.get("function", {})
            args = func.get("arguments", "{}")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            tool_calls.append(ToolCall(
                name=func.get("name", ""),
                arguments=args,
                call_id=tc.get("id", ""),
            ))
    return tool_calls


def create_engine(config: Dict[str, str]) -> Optional[LLMEngine]:
    if config.get("enabled", "").lower() != "true":
        logging.info("AI engine disabled")
        return None

    model_path = config.get("model_path", "")
    endpoint = config.get("endpoint", "")

    if model_path:
        import os
        if not os.path.isfile(model_path):
            logging.error("AI model file not found: %s", model_path)
            return None

        engine = LocalEngine(
            model_path=model_path,
            n_ctx=int(config.get("n_ctx", "4096")),
            n_threads=int(config.get("n_threads", "4")),
        )
        if engine.load():
            return engine
        return None

    elif endpoint:
        return RemoteEngine(
            endpoint=endpoint,
            api_key=config.get("api_key", ""),
            model=config.get("model", "default"),
        )

    else:
        logging.warning("AI enabled but no model_path or endpoint configured")
        return None
