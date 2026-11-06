"""Мост между окном (JS) и ядром: pywebview вызывает эти методы из страницы (window.pywebview.api.*)."""
import os
import threading
import webbrowser

from . import migrate, scan, updater
from .engine import Engine


class Api:
    def __init__(self):
        self._engine = Engine()
        self._window = None
        self._busy = threading.Lock()

    def attach(self, window):
        self._window = window

    # ── состояние ──

    def init(self):
        e = self._engine
        return {"folders": e.folder_list(), "settings": self.get_settings(), "checked_at": e.checked_at,
                "version": updater.VERSION}

    # ── обновление самого ModUp ──

    def update_check(self):
        try:
            self._update = updater.check()
            return dict(self._update, ok=True)
        except Exception as ex:   # нет сети/GitHub — молча, это не мешает работе
            return {"ok": False, "available": False, "error": str(ex)}

    def update_apply(self):
        info = getattr(self, "_update", None)
        if not info or not info.get("available"):
            return {"ok": False, "error": "Обновление не найдено — перезапусти ModUp"}

        def go():
            exe = updater.apply(info, progress=self._engine._set_progress)
            self._engine.progress = {"busy": False, "text": ""}
            updater.restart(exe)
            if self._window:
                threading.Timer(0.8, self._window.destroy).start()   # дать странице получить ответ
            return {"ok": True, "version": info["version"]}
        return self._locked(go)

    def folders(self):
        return self._engine.folder_list()

    def progress(self):
        return self._engine.progress

    def view(self, path):
        return self._engine.folder_view(path)

    # ── папки ──

    def discover(self):
        have = set(self._engine.cfg["folders"])
        out = []
        for f in scan.discover():
            out.append({"path": f.path, "name": f.name, "launcher": f.launcher, "game": f.game_version,
                        "loader": f.loader, "added": os.path.normpath(f.path) in have})
        return out

    def add_discovered(self, paths):
        wanted = {os.path.normcase(os.path.normpath(p)) for p in (paths or [])}
        found = [f for f in scan.discover() if os.path.normcase(os.path.normpath(f.path)) in wanted]
        return {"added": self._engine.add_folders(found), "folders": self._engine.folder_list()}

    def _ask_mods_dir(self):
        """Диалог выбора папки; выбрали инстанс или .minecraft — берём его папку mods."""
        if not self._window:
            return None
        import webview
        res = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if not res:
            return None
        path = res[0] if isinstance(res, (list, tuple)) else res
        if os.path.basename(path).lower() != "mods" and os.path.isdir(os.path.join(path, "mods")):
            path = os.path.join(path, "mods")
        if os.path.isdir(os.path.join(path, ".minecraft", "mods")):
            path = os.path.join(path, ".minecraft", "mods")
        return path

    def pick_folder(self):
        path = self._ask_mods_dir()
        if not path:
            return {"ok": False}
        self._engine.add_folders([path])
        return {"ok": True, "path": os.path.normpath(path), "folders": self._engine.folder_list()}

    def remove_folder(self, path):
        self._engine.remove_folder(path)
        return self._engine.folder_list()

    # ── проверка и обновление ──

    def check(self, paths=None):
        if not self._busy.acquire(blocking=False):
            return {"ok": False, "error": "Уже идёт проверка"}
        try:
            self._engine.check(only=paths or None)
            return {"ok": True, "folders": self._engine.folder_list(), "errors": self._engine.errors}
        except Exception as ex:
            self._engine.progress = {"busy": False, "text": ""}
            return {"ok": False, "error": str(ex)}
        finally:
            self._busy.release()

    def apply(self, path, keys):
        if not self._busy.acquire(blocking=False):
            return {"ok": False, "error": "Подожди — идёт другая операция"}
        try:
            return self._engine.apply(path, keys or [])
        except Exception as ex:
            self._engine.progress = {"busy": False, "text": ""}
            return {"ok": False, "error": str(ex)}
        finally:
            self._busy.release()

    def apply_group(self, group):
        """Обновить этот мод во всех папках, где он стоит и есть обновление."""
        return self._locked(lambda: self._engine.apply_many(self._engine.plan_group(group)))

    def apply_all(self):
        """Обновить всё во всех проверенных папках."""
        return self._locked(lambda: self._engine.apply_many(self._engine.plan_all()))

    def apply_plan(self, plan):
        """Повторить обновление по готовому плану {папка: [ключи]} — то, что в прошлый раз не скачалось."""
        return self._locked(lambda: self._engine.apply_many(plan or {}))

    def plan_counts(self, group=None):
        plan = self._engine.plan_group(group) if group else self._engine.plan_all()
        return {"folders": len(plan), "mods": sum(len(v) for v in plan.values())}

    def _locked(self, fn):
        if not self._busy.acquire(blocking=False):
            return {"ok": False, "error": "Подожди — идёт другая операция"}
        try:
            return fn()
        except Exception as ex:
            self._engine.progress = {"busy": False, "text": ""}
            return {"ok": False, "error": str(ex)}
        finally:
            self._busy.release()

    def export_info(self, path):
        return self._engine.export_info(path)

    def export_pack(self, path, name, version, keys, fmt):
        """Экспорт только модов (без настроек и прочего из инстанса)."""
        import re
        import webview
        ext = "mrpack" if fmt == "mrpack" else "zip"
        safe = re.sub(r'[\\/:*?"<>|]+', "_", (name or "сборка").strip()) or "сборка"
        label = "Modrinth-пак" if ext == "mrpack" else "Архив"
        res = self._window.create_file_dialog(webview.SAVE_DIALOG, save_filename=f"{safe}-{version or '1.0'}.{ext}",
                                              file_types=(f"{label} (*.{ext})",))
        if not res:
            return {"ok": False, "cancelled": True}
        dest = res[0] if isinstance(res, (list, tuple)) else res
        if not dest.lower().endswith("." + ext):
            dest += "." + ext
        return self._locked(lambda: self._engine.export_pack(path, dest, name, version, keys, False, fmt))

    # ── перенос на другую версию игры ──

    def migrate_targets(self):
        try:
            return {"ok": True, "versions": migrate.game_versions()[:40]}
        except Exception as ex:
            return {"ok": False, "error": str(ex), "versions": []}

    def migrate_plan(self, path, target):
        return self._locked(lambda: self._engine.migrate_plan(path, target))

    def pick_dest(self):
        path = self._ask_mods_dir()
        return self._engine.dest_info(path) if path else {"path": ""}

    def dest_info(self, path):
        return self._engine.dest_info(path)

    def migrate_apply(self, dest, keys, extras=None, retry=False):
        def go():
            res = self._engine.migrate_apply(dest, keys or [], extras or [], bool(retry))
            if res.get("done"):
                self._engine.check(only=[res["dest"]])
                res["folders"] = self._engine.folder_list()
            return res
        return self._locked(go)

    def rollback(self, path):
        if not self._busy.acquire(blocking=False):
            return {"ok": False, "error": "Подожди — идёт другая операция"}
        try:
            return self._engine.rollback(path)
        except Exception as ex:
            return {"ok": False, "error": str(ex)}
        finally:
            self._busy.release()

    # ── настройки модов ──

    def set_github(self, mod_id, url):
        return self._engine.set_github(mod_id, url)

    def ignore(self, mod_id, on=True):
        return self._engine.ignore(mod_id, on)

    def skip_version(self, group, version):
        return self._engine.skip_version(group, version)

    def set_game_version(self, path, version):
        return self._engine.set_game_version(path, version)

    def get_settings(self):
        c = self._engine.cfg
        return {"allow_pre": c["allow_pre"], "has_token": bool(c["github_token"]), "ignore": c["ignore"]}

    def set_settings(self, allow_pre=None, github_token=None):
        c = self._engine.cfg
        if allow_pre is not None:
            c.data["allow_pre"] = bool(allow_pre)
        if github_token is not None:
            c.data["github_token"] = github_token.strip()
        c.save()
        return self.get_settings()

    # ── внешнее ──

    def open_url(self, url):
        if isinstance(url, str) and url.startswith(("https://", "http://")):
            webbrowser.open(url)

    def open_folder(self, path):
        if path and os.path.isdir(path):
            os.startfile(path)

    def open_file(self, path):
        """Отчёт переноса — только .txt (страница не открывает этим произвольные файлы)."""
        if path and path.lower().endswith(".txt") and os.path.isfile(path):
            os.startfile(path)
