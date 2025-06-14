from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import AsyncIterator, List

from ollama import AsyncClient, ChatResponse, Message

from ..config import (
    MAX_TOOL_CALL_DEPTH,
    MODEL_NAME,
    NUM_CTX,
    OLLAMA_HOST,
    SYSTEM_PROMPT,
    TOOL_PLACEHOLDER_CONTENT,
    UPLOAD_DIR,
)
from ..db import (
    Conversation,
    User,
    db,
    add_document,
)
from ..utils.logging import get_logger
from .schema import Msg
from contextlib import suppress

from ..tools import execute_terminal, set_vm, create_memory_tool
from ..utils.memory import get_memory
from ..vm import VMRegistry

from .state import SessionState, get_state
from .messages import (
    format_output,
    remove_tool_placeholder,
    store_assistant_message,
)

_LOG = get_logger(__name__)


class ChatSession:
    """Manage a conversation with persistent history and tool execution."""

    def __init__(
        self,
        user: str = "default",
        session: str = "default",
        host: str = OLLAMA_HOST,
        model: str = MODEL_NAME,
        *,
        system_prompt: str = SYSTEM_PROMPT,
        tools: list[callable] | None = None,
        think: bool = True,
    ) -> None:
        db.init_db()
        self._client = AsyncClient(host=host)
        self._model = model
        self._user = db.get_or_create_user(user)
        self._conversation = db.get_or_create_conversation(
            self._user, session
        )
        self._vm = None
        self._base_system_prompt = system_prompt
        self._system_prompt = self._apply_memory(system_prompt)
        memory_tool = create_memory_tool(self._user.username, self._refresh_system_prompt)
        self._tools = (tools or [execute_terminal]) + [memory_tool]
        self._tool_funcs = {func.__name__: func for func in self._tools}
        self._think = think
        self._current_tool_name: str | None = None
        self._messages: List[Msg] = self._load_history()
        self._state_data: SessionState = get_state(self._conversation.id)
        self._lock = self._state_data.lock
        self._prompt_queue: asyncio.Queue[
            tuple[str, asyncio.Queue[str | None]]
        ] = asyncio.Queue()
        self._worker: asyncio.Task | None = None

    # ------------------------------------------------------------------
    def _apply_memory(self, prompt: str) -> str:
        memory = get_memory(self._user.username)
        return f"{prompt}\n<memory>\n{memory}\n</memory>"

    def _refresh_system_prompt(self) -> None:
        self._system_prompt = self._apply_memory(self._base_system_prompt)

    # ------------------------------------------------------------------
    # Properties exposing session state
    @property
    def _state(self) -> str:
        return self._state_data.status

    @_state.setter
    def _state(self, value: str) -> None:
        self._state_data.status = value

    @property
    def _tool_task(self) -> asyncio.Task | None:
        return self._state_data.tool_task

    @_tool_task.setter
    def _tool_task(self, task: asyncio.Task | None) -> None:
        self._state_data.tool_task = task

    @property
    def _placeholder_saved(self) -> bool:
        return self._state_data.placeholder_saved

    @_placeholder_saved.setter
    def _placeholder_saved(self, value: bool) -> None:
        self._state_data.placeholder_saved = value

    @property
    def think(self) -> bool:
        """Default value for the ``think`` parameter in :meth:`ask`."""

        return self._think

    @think.setter
    def think(self, value: bool) -> None:
        self._think = value

    async def __aenter__(self) -> "ChatSession":
        self._vm = VMRegistry.acquire(self._user.username)
        set_vm(self._vm)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        set_vm(None)
        if self._vm:
            VMRegistry.release(self._user.username)
        db.close()

    # ------------------------------------------------------------------
    def upload_document(self, file_path: str) -> str:
        """Save a document for later access inside the VM."""

        src = Path(file_path)
        if not src.exists():
            raise FileNotFoundError(file_path)

        dest = Path(UPLOAD_DIR) / self._user.username
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / src.name
        shutil.copy(src, target)
        add_document(self._user.username, str(target), src.name)
        return f"/data/{src.name}"

    # ------------------------------------------------------------------
    def _load_history(self) -> List[Msg]:
        messages: List[Msg] = []
        for msg in db.list_messages(self._conversation):
            if msg.role == "system":
                continue
            if msg.role == "assistant":
                try:
                    data = json.loads(msg.content)
                except json.JSONDecodeError:
                    messages.append({"role": "assistant", "content": msg.content})
                else:
                    if isinstance(data, list):
                        messages.append(
                            {
                                "role": "assistant",
                                "tool_calls": [Message.ToolCall(**c) for c in data],
                            }
                        )
                    elif isinstance(data, dict):
                        msg_data: Msg = {"role": "assistant"}
                        if content := data.get("content"):
                            msg_data["content"] = content
                        if calls := data.get("tool_calls"):
                            msg_data["tool_calls"] = [
                                Message.ToolCall(**c) for c in calls
                            ]
                        messages.append(msg_data)
                    else:
                        messages.append({"role": "assistant", "content": str(data)})
            elif msg.role == "user":
                messages.append({"role": "user", "content": msg.content})
            else:
                messages.append({"role": "tool", "content": msg.content})
        return messages

    # ------------------------------------------------------------------
    async def ask(
        self, messages: List[Msg], *, think: bool | None = None
    ) -> ChatResponse:
        """Send a chat request, automatically prepending the system prompt."""

        if not messages or messages[0].get("role") != "system":
            payload = [{"role": "system", "content": self._system_prompt}, *messages]
        else:
            payload = messages

        if think is None:
            think = self._think

        return await self._client.chat(
            self._model,
            messages=payload,
            think=think,
            tools=self._tools,
            keep_alive=-1,
            options={"num_ctx": NUM_CTX, "temperature": 0.01},
        )

    async def _run_tool_async(self, func, **kwargs) -> str:
        if asyncio.iscoroutinefunction(func):
            return await func(**kwargs)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: func(**kwargs))

    # ------------------------------------------------------------------
    def _add_tool_message(
        self,
        conversation: Conversation,
        messages: list[Msg],
        name: str,
        content: str,
    ) -> None:
        messages.append({"role": "tool", "name": name, "content": content})
        db.create_message(conversation, "tool", content)

    def _add_assistant_message(
        self,
        conversation: Conversation,
        messages: list[Msg],
        message: Message,
    ) -> None:
        store_assistant_message(conversation, message)
        messages.append(message.model_dump())

    async def _await_tool_and_followup(
        self,
        exec_task: asyncio.Task,
        follow_task: asyncio.Task,
        messages: list[Msg],
        conversation: Conversation,
        tool_name: str,
    ) -> AsyncIterator[ChatResponse]:
        done, _ = await asyncio.wait(
            {exec_task, follow_task}, return_when=asyncio.FIRST_COMPLETED
        )

        name = "junior" if tool_name == "send_to_junior" else tool_name

        if exec_task in done:
            follow_task.cancel()
            with suppress(asyncio.CancelledError):
                await follow_task
            remove_tool_placeholder(messages, TOOL_PLACEHOLDER_CONTENT)
            self._placeholder_saved = False
            result = await exec_task
            self._current_tool_name = None
            self._add_tool_message(conversation, messages, name, result)
            async with self._lock:
                self._state = "generating"
                self._tool_task = None
            nxt = await self.ask(messages)
            self._add_assistant_message(conversation, messages, nxt.message)
            yield nxt
        else:
            followup = await follow_task
            self._save_tool_placeholder()
            self._add_assistant_message(conversation, messages, followup.message)
            yield followup
            result = await exec_task
            remove_tool_placeholder(messages, TOOL_PLACEHOLDER_CONTENT)
            self._placeholder_saved = False
            self._current_tool_name = None
            self._add_tool_message(conversation, messages, name, result)
            async with self._lock:
                self._state = "generating"
                self._tool_task = None
            nxt = await self.ask(messages)
            self._add_assistant_message(conversation, messages, nxt.message)
            yield nxt

    async def _process_tool_call(
        self,
        call: Message.ToolCall,
        messages: list[Msg],
        conversation: Conversation,
    ) -> AsyncIterator[ChatResponse]:
        func = self._tool_funcs.get(call.function.name)
        if not func:
            _LOG.warning("Unsupported tool call: %s", call.function.name)
            result = f"Unsupported tool: {call.function.name}"
            name = "junior" if call.function.name == "send_to_junior" else call.function.name
            self._add_tool_message(conversation, messages, name, result)
            return

        exec_task = asyncio.create_task(
            self._run_tool_async(func, **call.function.arguments)
        )
        self._current_tool_name = call.function.name

        placeholder = {
            "role": "tool",
            "name": "junior" if call.function.name == "send_to_junior" else call.function.name,
            "content": TOOL_PLACEHOLDER_CONTENT,
        }
        messages.append(placeholder)
        self._placeholder_saved = False

        follow_task = asyncio.create_task(self.ask(messages))
        async with self._lock:
            self._state = "awaiting_tool"
            self._tool_task = exec_task

        async for resp in self._await_tool_and_followup(
            exec_task, follow_task, messages, conversation, call.function.name
        ):
            yield resp

    async def _handle_tool_calls_stream(
        self,
        messages: List[Msg],
        response: ChatResponse,
        conversation: Conversation,
        depth: int = 0,
    ) -> AsyncIterator[ChatResponse]:
        if response.message.content:
            # Yield assistant content even when a tool call is present so context is not lost.
            yield response

        if not response.message.tool_calls:
            async with self._lock:
                self._state = "idle"
            return

        while depth < MAX_TOOL_CALL_DEPTH and response.message.tool_calls:
            for call in response.message.tool_calls:
                async for nxt in self._process_tool_call(call, messages, conversation):
                    response = nxt
                    yield nxt
                depth += 1
                if depth >= MAX_TOOL_CALL_DEPTH:
                    break

        async with self._lock:
            self._state = "idle"

    async def _generate_stream(self, prompt: str) -> AsyncIterator[str]:
        async with self._lock:
            if self._state == "awaiting_tool" and self._tool_task:
                async for part in self._chat_during_tool(prompt):
                    yield part
                return
            self._state = "generating"

        db.create_message(self._conversation, "user", prompt)
        self._messages.append({"role": "user", "content": prompt})

        response = await self.ask(self._messages)
        self._messages.append(response.message.model_dump())
        store_assistant_message(self._conversation, response.message)

        async for resp in self._handle_tool_calls_stream(
            self._messages, response, self._conversation
        ):
            text = format_output(resp.message)
            if text:
                yield text

    async def _process_prompt_queue(self) -> None:
        try:
            while not self._prompt_queue.empty():
                prompt, result_q = await self._prompt_queue.get()
                try:
                    async for part in self._generate_stream(prompt):
                        await result_q.put(part)
                except Exception as exc:  # pragma: no cover - unforeseen errors
                    _LOG.exception("Error processing prompt: %s", exc)
                    await result_q.put(f"Error: {exc}")
                finally:
                    await result_q.put(None)
        finally:
            self._worker = None

    async def chat_stream(self, prompt: str) -> AsyncIterator[str]:
        result_q: asyncio.Queue[str | None] = asyncio.Queue()
        await self._prompt_queue.put((prompt, result_q))
        if not self._worker or self._worker.done():
            self._worker = asyncio.create_task(self._process_prompt_queue())

        while True:
            part = await result_q.get()
            if part is None:
                break
            yield part

    async def continue_stream(self) -> AsyncIterator[str]:
        async with self._lock:
            if self._state != "idle":
                return
            self._state = "generating"

        response = await self.ask(self._messages)
        self._messages.append(response.message.model_dump())
        store_assistant_message(self._conversation, response.message)

        async for resp in self._handle_tool_calls_stream(
            self._messages, response, self._conversation
        ):
            text = format_output(resp.message)
            if text:
                yield text

    async def _chat_during_tool(self, prompt: str) -> AsyncIterator[str]:
        db.create_message(self._conversation, "user", prompt)
        self._messages.append({"role": "user", "content": prompt})

        user_task = asyncio.create_task(self.ask(self._messages))
        exec_task = self._tool_task

        async for resp in self._await_tool_and_followup(
            exec_task,
            user_task,
            self._messages,
            self._conversation,
            self._current_tool_name or "tool",
        ):
            text = format_output(resp.message)
            if text:
                yield text
            async for part in self._handle_tool_calls_stream(
                self._messages, resp, self._conversation
            ):
                part_text = format_output(part.message)
                if part_text:
                    yield part_text

    # ------------------------------------------------------------------
    def _save_tool_placeholder(self) -> None:
        if not self._placeholder_saved:
            db.create_message(
                self._conversation,
                "tool",
                TOOL_PLACEHOLDER_CONTENT,
            )
            self._placeholder_saved = True


from ..utils.debug import debug_all
debug_all(globals())

