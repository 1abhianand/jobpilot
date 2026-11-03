"""The few Telegram Bot API calls the approval bot needs, over plain HTTPS long polling."""

from __future__ import annotations

from typing import Any

import httpx

Buttons = list[list[tuple[str, str]]]  # rows of (label, callback data)


class TelegramError(RuntimeError):
    pass


def keyboard(rows: Buttons | None) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": text, "callback_data": data} for text, data in row] for row in rows or []]}


class Telegram:
    def __init__(self, token: str, http: httpx.AsyncClient | None = None):
        self._url = f"https://api.telegram.org/bot{token}/"
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0))

    async def call(self, method: str, **params: Any) -> Any:
        payload = {key: value for key, value in params.items() if value is not None}
        response = await self._http.post(self._url + method, json=payload)
        try:
            data = response.json()
        except ValueError as exc:
            raise TelegramError(f"{method}: HTTP {response.status_code} from something other than Telegram; "
                                "a company network or firewall may be blocking api.telegram.org") from exc
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description', response.status_code)}")
        return data["result"]

    async def me(self) -> dict[str, Any]:
        return await self.call("getMe")

    async def send(self, chat_id: int, text: str, buttons: Buttons | None = None, *,
                   force_reply: bool = False, reply_to: int | None = None) -> int:
        markup = {"force_reply": True, "selective": True} if force_reply else (keyboard(buttons) if buttons else None)
        message = await self.call(
            "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup,
            link_preview_options={"is_disabled": True},
            reply_parameters={"message_id": reply_to, "allow_sending_without_reply": True} if reply_to else None,
        )
        return message["message_id"]

    async def edit(self, chat_id: int, message_id: int, text: str, buttons: Buttons | None = None) -> None:
        try:
            await self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                            parse_mode="HTML", reply_markup=keyboard(buttons),
                            link_preview_options={"is_disabled": True})
        except TelegramError as exc:
            # Editing to identical content, or a message too old to edit, is not worth failing over.
            if "message is not modified" not in str(exc) and "message to edit not found" not in str(exc):
                raise

    async def set_commands(self, commands: list[tuple[str, str]]) -> None:
        """The command menu Telegram shows when you type / (names without the slash)."""
        await self.call("setMyCommands", commands=[{"command": name, "description": text} for name, text in commands])

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        try:
            await self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except TelegramError:
            pass  # the button spinner times out on its own; never worth failing a decision over

    async def updates(self, offset: int | None, timeout: int = 30) -> list[dict[str, Any]]:
        return await self.call("getUpdates", offset=offset, timeout=timeout,
                               allowed_updates=["message", "callback_query"])
