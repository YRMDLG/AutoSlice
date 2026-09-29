"""字幕工作台的主选字幕和多选集合。"""

from __future__ import annotations


class CueSelection:
    def __init__(self) -> None:
        self.active: int | None = None
        self.anchor: int | None = None
        self.selected: set[int] = set()

    def clear(self) -> None:
        self.selected.clear()

    def choose(self, cue_id: int, order: list[int], *, ctrl: bool = False,
               shift: bool = False) -> None:
        if cue_id not in order:
            return
        if shift and self.anchor in order:
            first, last = sorted((order.index(self.anchor), order.index(cue_id)))
            selected = set(order[first:last + 1])
            self.selected = self.selected | selected if ctrl else selected
        elif ctrl:
            if cue_id in self.selected:
                self.selected.remove(cue_id)
            else:
                self.selected.add(cue_id)
            self.anchor = cue_id
        else:
            self.selected = {cue_id}
            self.anchor = cue_id
        self.active = cue_id

    def replace(self, cue_ids: set[int], order: list[int]) -> None:
        self.selected = set(order) & cue_ids
        if self.active not in self.selected and self.selected:
            self.active = next(item for item in order if item in self.selected)
        if self.selected:
            self.anchor = self.active

    def retain(self, order: list[int]) -> None:
        allowed = set(order)
        self.selected &= allowed
        if self.active not in allowed:
            self.active = next((item for item in order if item in self.selected), None)
        if self.anchor not in allowed:
            self.anchor = self.active
