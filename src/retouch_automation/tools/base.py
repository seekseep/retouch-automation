"""ツールの共通インターフェース。

モデルを差し替えても他のツールが壊れないよう、各ツールはこの Protocol を満たす。
prepare() でモデルをロードし、全写真で使い回してから release() で解放する。
"""

from __future__ import annotations

from typing import Protocol


class Tool(Protocol):
    name: str

    def prepare(self) -> None: ...

    def release(self) -> None: ...


class NullLifecycle:
    """モデルを持たないツール向けの既定実装。"""

    def prepare(self) -> None:
        return None

    def release(self) -> None:
        return None
