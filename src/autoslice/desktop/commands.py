"""字幕工作台统一命令注册与作用域分发。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Command:
    name: str
    handler: Callable[[], None]
    text_scope: bool = False


class CommandDispatcher:
    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}

    def register(self, name: str, handler: Callable[[], None], *, text_scope: bool = False) -> None:
        self._commands[name] = Command(name, handler, text_scope)

    def dispatch(self, name: str, *, editing: bool = False) -> bool:
        command = self._commands.get(name)
        if command is None or (editing and not command.text_scope):
            return False
        command.handler()
        return True

    def names(self) -> tuple[str, ...]:
        return tuple(self._commands)
