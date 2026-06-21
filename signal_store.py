import json
from pathlib import Path

from models import Signal


class SignalStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("[]", encoding="utf-8")

    def seen(self, signal: Signal) -> bool:
        return signal.key in self._load()

    def mark_seen(self, signal: Signal) -> None:
        keys = self._load()
        if signal.key not in keys:
            keys.append(signal.key)
            self.path.write_text(json.dumps(keys, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load(self) -> list[str]:
        return json.loads(self.path.read_text(encoding="utf-8"))
