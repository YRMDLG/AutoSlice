"""CoverDocument 的轻量内存 Undo/Redo。"""

from __future__ import annotations

from dataclasses import dataclass

from .cover_model import CoverDocument


@dataclass
class CoverHistory:
    """按完整文档快照记录可逆编辑，拖动预览不会写入历史。"""

    limit: int = 80

    def __post_init__(self) -> None:
        self._undo_stack: list[CoverDocument] = []
        self._redo_stack: list[CoverDocument] = []

    def reset(self, document: CoverDocument | None) -> None:
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._current = document

    @property
    def current(self) -> CoverDocument | None:
        return getattr(self, "_current", None)

    def commit(self, document: CoverDocument) -> bool:
        before = self.current
        if before == document:
            return False
        if before is not None:
            self._undo_stack.append(before)
            if len(self._undo_stack) > self.limit:
                del self._undo_stack[:-self.limit]
        self._current = document
        self._redo_stack.clear()
        return True

    def undo(self) -> CoverDocument | None:
        if not self._undo_stack or self.current is None:
            return None
        self._redo_stack.append(self.current)
        self._current = self._undo_stack.pop()
        return self.current

    def redo(self) -> CoverDocument | None:
        if not self._redo_stack or self.current is None:
            return None
        self._undo_stack.append(self.current)
        self._current = self._redo_stack.pop()
        return self.current

    @property
    def can_undo(self) -> bool:
        return bool(self._undo_stack)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo_stack)

