import json
import os
from pathlib import Path
import sys
import uuid


def default_state():
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "SATLOOT" / "StarPet"


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex[:10] + ".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def read_json(path, default=None):
    try:
        path = Path(path)
        if path.stat().st_size > 1024 * 1024:
            return default
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


class Preferences:
    def __init__(self, state):
        self.path = Path(state) / "preferences.json"
        self.values = {"size": 110, "humanSize": 160, "reducedMotion": False, "alwaysOnTop": True,
                       "codexEnabled": True, "workbuddyEnabled": True, "customImage": "",
                       "position": None, "hooksInstalled": False, "completionNotifications": True,
                       "character": "whale"}
        loaded = read_json(self.path, {})
        if isinstance(loaded, dict):
            for key in self.values:
                if key not in loaded:
                    continue
                if key == "position":
                    position = loaded[key]
                    if isinstance(position, list) and len(position) == 2 and all(type(v) is int for v in position):
                        self.values[key] = position
                elif type(loaded[key]) is type(self.values[key]):
                    self.values[key] = loaded[key]
        self.values["size"] = min(150, max(80, int(self.values["size"])))
        self.values['humanSize'] = min(200, max(100, self.values['humanSize']))

    def save(self, **changes):
        candidate = {**self.values, **changes}
        # Retired trial settings cannot reactivate the removed renderer, even
        # when an older installation or caller supplies its previous choices.
        for key in ('human3D', 'humanModel', 'cat3D'):
            candidate.pop(key, None)
        if type(candidate['humanSize']) is not int:
            raise ValueError('人物大小必须是整数。')
        candidate['humanSize'] = min(200, max(100, candidate['humanSize']))
        atomic_json(self.path, candidate)
        # UI consumers retain this mapping; only publish committed values.
        self.values.update(candidate)


def command_prefix():
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve())]
    python = Path(sys.executable)
    hidden = python.with_name("pythonw.exe")
    return [str(hidden if hidden.exists() else python), str(Path(__file__).resolve().parents[1] / "main.py")]
