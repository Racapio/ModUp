"""Проверка обновлений, проблемы папки, замена jar с бэкапом и откат."""
import base64
import datetime
import hashlib
import json
import os
import re
import shutil
import threading
import time
import urllib.request
import zipfile

from . import migrate, scan, sources, versions

# MODUP_HOME — для тестов: настройки не в %APPDATA%\ModUp.
APP_DIR = os.environ.get("MODUP_HOME") or os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "ModUp")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
GH_CACHE_PATH = os.path.join(APP_DIR, "github-cache.json")
BACKUP_DIR_NAME = "modup-backup"   # рядом с папкой mods (не внутри — иначе игра загрузила бы старые jar)

# Репозитории популярных модов, у которых в jar нет ссылки на GitHub (или там ссылка на Modrinth, а
# сборки под новую версию игры лежат только на GitHub — SkyHanni под 26.2).
KNOWN_REPOS = {
    "skyhanni": "hannibal002/SkyHanni",
    "skyblocker": "SkyblockerMod/Skyblocker",
    "firmament": "nea89o/Firmament",
    "opsec": "aurickk/OpSec",
    "hm-api": "AzureAaron/hm-api",
}

# Моды, которые не обновляем: файл-заглушка Essential, закрытые клиенты, наш мод.
DEFAULT_IGNORE = ["essential-dependencies", "nebula", "androidblood"]

# id мода → проект на Modrinth, когда они не совпадают. Нужно, если jar не с Modrinth (Essential с
# essential.gg, Sodium с CurseForge/GitHub): по отпечатку его не узнать, ищем по id.
MOD_ALIASES = {"essential-container": "essential", "essential-loader": "essential"}

# Что переносится вместе с модами (из папки игры старой версии в новую). (ключ, подпись, путь, папка?)
SETTINGS_ITEMS = [
    ("options", "Настройки игры (управление, графика, звук)", ["options.txt", "optionsof.txt", "optionsshaders.txt"], False),
    ("servers", "Список серверов", ["servers.dat"], False),
    ("config", "Настройки модов (config)", ["config"], True),
    ("resourcepacks", "Ресурс-паки", ["resourcepacks"], True),
    ("shaderpacks", "Шейдеры", ["shaderpacks"], True),
    ("screenshots", "Скриншоты", ["screenshots"], True),
]
# Кэши модов внутри config — не настройки (у SkyHanni/Skyblocker/NEU это десятки тысяч файлов).
MIGRATE_SKIP_DIRS = {"repo", "repos", "cache", "caches", "logs", "log", "temp", "tmp", "downloads",
                     "backup", "backups", "notenoughupdates-repo"}


def game_dir_of(mods_path):
    """Папка игры (где options.txt, config…) для папки mods; None — если это не папка mods."""
    p = os.path.normpath(mods_path or "")
    return os.path.dirname(p) if os.path.basename(p).lower() == "mods" else None


def resolve_mods_dir(dest):
    """Куда класть моды, если выбрали не саму папку mods, а инстанс/папку игры/пустую папку.
    Раньше jar ложились прямо в выбранную папку — игра их не видела (у друга 31.10: пустой инстанс)."""
    dest = os.path.normpath(dest or "")
    if not dest or dest == "." or os.path.basename(dest).lower() == "mods":
        return dest
    if os.path.isdir(os.path.join(dest, "mods")):
        return os.path.join(dest, "mods")
    for sub in (".minecraft", "minecraft"):
        if os.path.isdir(os.path.join(dest, sub)):
            return os.path.join(dest, sub, "mods")
    if any(os.path.isfile(os.path.join(dest, n)) for n in ("instance.cfg", "mmc-pack.json")):
        return os.path.join(dest, ".minecraft", "mods")   # инстанс Prism/MultiMC, ещё ни разу не запускался
    return os.path.join(dest, "mods")


def _now_iso():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


class Config:
    def __init__(self):
        self.data = {"folders": [], "github": {}, "ignore": list(DEFAULT_IGNORE), "skip": {},
                     "allow_pre": False, "github_token": "", "game_version": {}}
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                self.data.update(json.load(f))
        except (OSError, ValueError):
            pass

    def save(self):
        os.makedirs(APP_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)

    def __getitem__(self, k):
        return self.data[k]


def _key(folder_path, filename):
    return hashlib.sha1((os.path.normcase(folder_path) + "|" + filename).encode()).hexdigest()[:16]


class Engine:
    def __init__(self):
        self.cfg = Config()
        self.lock = threading.Lock()
        self.folders = {}        # path → scan.Folder
        self.rows = {}           # path → [row]
        self.problems = {}       # path → [str]
        self.checked_at = ""
        self.errors = []
        self.progress = {"busy": False, "text": ""}
        try:
            with open(GH_CACHE_PATH, encoding="utf-8") as fh:
                self.gh_cache = json.load(fh)
        except (OSError, ValueError):
            self.gh_cache = {}

    def _save_gh_cache(self):
        try:
            os.makedirs(APP_DIR, exist_ok=True)
            tmp = GH_CACHE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.gh_cache, fh)
            os.replace(tmp, GH_CACHE_PATH)
        except OSError:
            pass

    # ── папки ──

    def folder_list(self):
        out = []
        for p in self.cfg["folders"]:
            f = self.folders.get(p) or scan.describe_folder(p, self._launcher_of(p))
            rows = self.rows.get(p, [])
            out.append({
                "path": p, "name": f.name, "launcher": f.launcher,
                "game": self.cfg["game_version"].get(p) or f.game_version, "loader": f.loader or "fabric",
                "mods": len(f.mods) if f.mods else None,
                "updates": sum(1 for r in rows if r["update"]),
                "problems": len(self.problems.get(p, [])),
                "exists": os.path.isdir(p),
            })
        return out

    def _launcher_of(self, path):
        return self.cfg.data.get("launchers", {}).get(path, "")

    def add_folders(self, folders):
        added = 0
        launchers = self.cfg.data.setdefault("launchers", {})
        for f in folders:
            p = os.path.normpath(f.path if isinstance(f, scan.Folder) else f)
            if p not in self.cfg["folders"]:
                self.cfg["folders"].append(p)
                added += 1
            if isinstance(f, scan.Folder):
                launchers[p] = f.launcher
        self.cfg.save()
        return added

    def remove_folder(self, path):
        if path in self.cfg["folders"]:
            self.cfg["folders"].remove(path)
            self.cfg.save()
        self.folders.pop(path, None)
        self.rows.pop(path, None)

    # ── проверка ──

    def _set_progress(self, text):
        self.progress = {"busy": True, "text": text}

    def check(self, only=None):
        """Сканирует папки и ищет обновления. only — список путей (иначе все)."""
        only_n = {os.path.normcase(os.path.normpath(x)) for x in (only or [])}
        paths = [p for p in self.cfg["folders"]
                 if (not only or os.path.normcase(os.path.normpath(p)) in only_n) and os.path.isdir(p)]
        self.errors = []
        self._set_progress("Читаю моды…")
        loaded = sources.run_parallel(lambda p: scan.load_mods(scan.describe_folder(p, self._launcher_of(p))), paths, workers=4)
        for f in loaded:
            override = self.cfg["game_version"].get(f.path)
            if override:
                f.game_version = override
            self.folders[f.path] = f

        all_mods = [m for f in loaded for m in f.mods]
        self._set_progress(f"Сверяю {len(all_mods)} файлов с Modrinth…")
        try:
            ident = sources.modrinth_identify(sorted({m.sha1 for m in all_mods}))
        except sources.SourceError as e:
            ident = {}
            self.errors.append(f"Modrinth: {e}")
        try:
            projects = sources.modrinth_projects({v["project_id"] for v in ident.values()})
        except sources.SourceError as e:
            projects = {}
            self.errors.append(f"Modrinth (описания): {e}")

        # Версии проектов под каждую пару (загрузчик, версия игры) — один запрос на проект.
        need = sorted({(v["project_id"], f.loader or "fabric", f.game_version) for f in loaded for m in f.mods
                       if (v := ident.get(m.sha1))})
        self._set_progress(f"Ищу новые версии ({len(need)} проектов на Modrinth)…")

        def fetch(k):
            try:
                return k, sources.modrinth_versions(k[0], k[1], k[2])
            except sources.SourceError as e:
                return k, e
        mr_versions = dict(sources.run_parallel(fetch, need))

        # GitHub — для модов не с Modrinth, у которых известен репозиторий.
        gh_need = {}
        for f in loaded:
            for m in f.mods:
                if m.sha1 in ident or self._ignored(m):
                    continue
                repo = self._repo_of(m)
                if repo:
                    gh_need[repo] = None
        self._set_progress(f"Проверяю GitHub ({len(gh_need)} репозиториев)…")

        def fetch_gh(repo):
            try:
                return repo, sources.github_releases(repo, self.cfg["github_token"], self.gh_cache)
            except sources.SourceError as e:
                return repo, e
        gh_releases = dict(sources.run_parallel(fetch_gh, list(gh_need), workers=4))
        self._save_gh_cache()

        # Последний запасной путь: в моде ссылка на страницу Modrinth, но сам файл там не нашёлся
        # (собран отдельно) — ищем версии этого проекта под нашу игру.
        slug_need = sorted({(m.modrinth_slug, f.loader or "fabric", f.game_version) for f in loaded for m in f.mods
                            if m.modrinth_slug and m.sha1 not in ident and not self._ignored(m) and not self._repo_of(m)})
        slug_versions = dict(sources.run_parallel(fetch, slug_need)) if slug_need else {}
        self._slug_versions = slug_versions

        rows_all, problems_all = {}, {}
        for f in loaded:
            rows = []
            for m in f.mods:
                rows.append(self._row(f, m, ident, projects, mr_versions, gh_releases))
            rows_all[f.path] = rows
            problems_all[f.path] = self._problems(f, rows)
        # «Где стоит»: тот же мод в других папках.
        by_mod = {}
        for p, rows in rows_all.items():
            for r in rows:
                by_mod.setdefault(r["group"], []).append(self.folders[p].name)
        for rows in rows_all.values():
            for r in rows:
                r["where"] = sorted(set(by_mod.get(r["group"], [])))
        with self.lock:
            self.rows.update(rows_all)
            self.problems.update(problems_all)
            self.checked_at = _now_iso()
        self.progress = {"busy": False, "text": ""}
        return True

    def _ignored(self, m):
        return (m.mod_id or "").lower() in [x.lower() for x in self.cfg["ignore"]]

    def _row(self, f, m, ident, projects, mr_versions, gh_releases):
        row = {
            "key": _key(f.path, m.filename), "filename": m.filename, "name": m.name, "id": m.mod_id,
            "version": m.version or "", "source": "unknown", "update": None, "status": "", "reason": "",
            "icon": "", "page": "", "repo": "", "group": (m.mod_id or m.filename).lower(), "problem": "",
        }
        if m.icon_bytes:
            kind = "png" if m.icon_bytes[:4] == b"\x89PNG" else "jpeg"
            row["icon"] = f"data:image/{kind};base64," + base64.b64encode(m.icon_bytes).decode()
        if self._ignored(m):
            row.update(source="ignored", status="Пропускается")
            return row
        skip = self.cfg["skip"].get(row["group"], "")
        v = ident.get(m.sha1)
        if v:
            p = projects.get(v["project_id"], {})
            f_mr = next((x for x in v.get("files", []) if (x.get("hashes") or {}).get("sha1") == m.sha1), None)
            if f_mr:   # для экспорта в .mrpack: мод можно указать ссылкой, а не класть файл
                row["mr_file"] = {"url": f_mr["url"], "sha1": m.sha1,
                                  "sha512": (f_mr.get("hashes") or {}).get("sha512") or m.sha512,
                                  "size": f_mr.get("size") or m.size}
            row.update(source="modrinth", name=p.get("title") or m.name, icon=p.get("icon_url") or row["icon"],
                       page=f"https://modrinth.com/mod/{p.get('slug') or v['project_id']}",
                       version=v.get("version_number") or row["version"], group="mr:" + v["project_id"])
            cands = mr_versions.get((v["project_id"], f.loader or "fabric", f.game_version))
            if isinstance(cands, Exception):
                row.update(status="Ошибка проверки", reason=str(cands))
                return row
            upd = sources.pick_modrinth_update(v, cands or [], self.cfg["allow_pre"])
            if upd:
                upd["page"] = row["page"]
            self._apply_update(row, upd, skip)
            return row
        repo = self._repo_of(m)
        if not repo and m.modrinth_slug:
            return self._row_by_slug(f, m, row, skip)
        if repo:
            row.update(source="github", repo=repo, page=f"https://github.com/{repo}")
            rel = gh_releases.get(repo)
            if isinstance(rel, Exception):
                row.update(status="Ошибка проверки", reason=str(rel))
                return row
            upd, why = sources.pick_github_update(m.version, m.filename, rel or [], f.loader or "fabric",
                                                  f.game_version, self.cfg["allow_pre"])
            self._apply_update(row, upd, skip)
            if not upd and why:
                row["reason"] = why
            return row
        row.update(status="Укажи источник",
                   reason="Мода нет на Modrinth, и в нём не указан GitHub. Вставь ссылку один раз — запомню.")
        return row

    def _repo_of(self, m):
        mid = (m.mod_id or "").lower()
        return self.cfg["github"].get(m.mod_id) or m.github or KNOWN_REPOS.get(mid, "")

    def _row_by_slug(self, f, m, row, skip):
        row.update(source="modrinth", page=f"https://modrinth.com/mod/{m.modrinth_slug}")
        cands = getattr(self, "_slug_versions", {}).get((m.modrinth_slug, f.loader or "fabric", f.game_version))
        if isinstance(cands, Exception):
            row.update(status="Ошибка проверки", reason=str(cands))
            return row
        allow_pre = self.cfg["allow_pre"] or bool(__import__("re").search(r"(?i)(alpha|beta|pre|rc)", m.version or ""))
        best = None
        for v in cands or []:
            if v.get("version_type", "release") != "release" and not allow_pre:
                continue
            if best is None or v.get("date_published", "") > best.get("date_published", ""):
                best = v
        if not best:
            row.update(status="Актуальная", reason=f"на Modrinth нет сборки под {f.game_version}; укажи GitHub, если он есть")
            return row
        upd = None
        if versions.compare(best.get("version_number", ""), m.version or "0") > 0:
            fake_cur = {"id": "", "version_type": "release", "date_published": ""}
            upd = sources.pick_modrinth_update(fake_cur, [best], True)
            if upd:
                upd["page"] = row["page"]
        self._apply_update(row, upd, skip)
        return row

    def _apply_update(self, row, upd, skip):
        if upd and skip and versions.compare(upd["version"], skip) <= 0:
            row.update(status="Актуальная", reason=f"Версия {skip} пропущена тобой")
            return
        if upd:
            row.update(update=upd, status="Есть обновление")
        else:
            row["status"] = "Актуальная"

    def _problems(self, f, rows):
        out = []
        by_id = {}
        for m in f.mods:
            if m.mod_id:
                by_id.setdefault(m.mod_id, []).append(m)
        for mid, ms in by_id.items():
            if len(ms) > 1:
                names = ", ".join(sorted(x.version or x.filename for x in ms))
                out.append(f"{ms[0].name} стоит {len(ms)} раза ({names}) — оставь одну версию")
                for r in rows:
                    if r["id"] == mid:
                        r["problem"] = "Дубль"
        for m in f.mods:
            if "(1)" in m.filename or "(2)" in m.filename:
                out.append(f"{m.filename} — похоже, скачан повторно")
                for r in rows:
                    if r["filename"] == m.filename and not r["problem"]:
                        r["problem"] = "Повторно скачан"
            if f.game_version and m.mc_dep is not None and not versions.mc_dependency_ok(m.mc_dep, f.game_version):
                dep = m.mc_dep if isinstance(m.mc_dep, str) else " / ".join(map(str, m.mc_dep))
                out.append(f"{m.name} требует Minecraft {dep}, а здесь {f.game_version}")
                for r in rows:
                    if r["filename"] == m.filename:
                        r["problem"] = "Другая версия игры"
        return out

    # ── состояние для окна ──

    def folder_view(self, path):
        f = self.folders.get(path) or scan.describe_folder(path, self._launcher_of(path))
        return {
            "path": path, "name": f.name, "launcher": f.launcher,
            "game": self.cfg["game_version"].get(path) or f.game_version, "loader": f.loader or "fabric",
            "rows": self.rows.get(path), "problems": self.problems.get(path, []),
            "disabled": len(f.disabled), "checked_at": self.checked_at, "errors": self.errors,
            "running": self.game_running(path), "backups": len(self.list_backups(path)),
            "backup_dir": self.backup_root(path),
        }

    # ── игра запущена? ──

    _proc_cache = (0.0, [])

    @classmethod
    def _java_cmdlines(cls):
        """Командные строки запущенных java/javaw (кэш 5 с). Prism/MultiMC передают в них путь инстанса."""
        t, lines = cls._proc_cache
        if time.time() - t < 5:
            return lines
        lines = []
        try:
            import subprocess
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name like 'java%'\" | ForEach-Object { $_.CommandLine }"],
                capture_output=True, text=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            lines = [x.strip().lower().replace("\\", "/") for x in out.stdout.splitlines() if x.strip()]
        except Exception:
            lines = []
        cls._proc_cache = (time.time(), lines)
        return lines

    @classmethod
    def game_running(cls, path):
        """Игра с этой папкой запущена? Ищем путь инстанса в командных строках java.
        Заменять jar при запущенной игре на Windows можно (Java не блокирует переименование) — игра
        доработает на старых файлах, новые подхватит при перезапуске; поэтому это только предупреждение."""
        mods = os.path.normpath(path)
        parent = os.path.dirname(mods)
        inst = os.path.dirname(parent) if os.path.basename(parent).lower() in (".minecraft", "minecraft") else parent
        needles = {os.path.normcase(x).replace("\\", "/").lower() for x in (inst, parent)}
        return any(n and n in line for line in cls._java_cmdlines() for n in needles)

    # ── замена и откат ──

    def backup_root(self, path):
        return os.path.join(os.path.dirname(os.path.normpath(path)), BACKUP_DIR_NAME)

    def list_backups(self, path):
        root = self.backup_root(path)
        try:
            return sorted((d for d in os.listdir(root) if os.path.isfile(os.path.join(root, d, "manifest.json"))), reverse=True)
        except OSError:
            return []

    def apply(self, path, keys, recheck=True):
        """Обновить выбранные моды папки. Возвращает {ok, done:[...], failed:[...]}."""
        rows = {r["key"]: r for r in self.rows.get(path, [])}
        todo = [rows[k] for k in keys if k in rows and rows[k]["update"]]
        if not todo:
            return {"ok": False, "error": "Нечего обновлять"}
        running = self.game_running(path)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        bdir = os.path.join(self.backup_root(path), stamp)
        tmpdir = os.path.join(self.backup_root(path), ".download")
        os.makedirs(tmpdir, exist_ok=True)
        done, failed, manifest, failed_keys = [], [], [], []
        for i, r in enumerate(todo, 1):
            u = r["update"]
            self._set_progress(f"Скачиваю {r['name']} ({i}/{len(todo)})…")
            try:
                tmp = os.path.join(tmpdir, u["filename"])
                self._download(u, tmp)
                os.makedirs(bdir, exist_ok=True)
                old = os.path.join(path, r["filename"])
                new = os.path.join(path, u["filename"])
                shutil.move(old, os.path.join(bdir, r["filename"]))
                if os.path.exists(new):           # имя совпало со старым другим файлом — тоже в бэкап
                    shutil.move(new, os.path.join(bdir, u["filename"] + ".prev"))
                shutil.move(tmp, new)
                manifest.append({"old": r["filename"], "new": u["filename"]})
                done.append(f"{r['name']} {r['version']} → {u['version']}")
            except Exception as e:  # одна неудача не ломает остальные
                failed.append(f"{r['name']}: {e}")
                failed_keys.append(r["key"])
        if manifest:
            with open(os.path.join(bdir, "manifest.json"), "w", encoding="utf-8") as fh:
                json.dump({"at": _now_iso(), "items": manifest}, fh, ensure_ascii=False, indent=2)
        shutil.rmtree(tmpdir, ignore_errors=True)
        self.progress = {"busy": False, "text": ""}
        if done and recheck:
            self.check(only=[path])
        return {"ok": bool(done), "done": done, "failed": failed, "failed_keys": failed_keys, "running": running}

    def apply_many(self, plan):
        """plan: {путь папки: [ключи]} — обновить сразу в нескольких папках (мод во всех, или всё везде)."""
        done, failed, running, retry = [], [], False, {}
        for path, keys in plan.items():
            if not keys:
                continue
            res = self.apply(path, keys, recheck=False)
            if res.get("failed_keys"):
                retry[path] = res["failed_keys"]   # «Повторить» — только то, что не скачалось
            name = (self.folders.get(path) or scan.describe_folder(path)).name
            if res.get("error"):
                failed.append(f"{name}: {res['error']}")
                continue
            done += [f"{name}: {x}" for x in res.get("done", [])]
            failed += [f"{name}: {x}" for x in res.get("failed", [])]
            running = running or res.get("running", False)
        touched = [p for p, k in plan.items() if k]
        if done and touched:
            self.check(only=touched)   # одна перепроверка на все папки, а не по папке за раз
        return {"ok": bool(done), "done": done, "failed": failed, "running": running, "retry": retry}

    def plan_group(self, group):
        """Где этот мод можно обновить: {путь: [ключ]}."""
        return {p: [r["key"] for r in rows if r["group"] == group and r["update"]]
                for p, rows in self.rows.items() if any(r["group"] == group and r["update"] for r in rows)}

    def plan_all(self):
        return {p: [r["key"] for r in rows if r["update"]] for p, rows in self.rows.items() if any(r["update"] for r in rows)}

    DOWNLOAD_TRIES = 3

    def _download(self, u, dest):
        """Скачать с проверкой отпечатка; разрыв связи — ещё пара попыток, недокачанный файл не остаётся."""
        last = None
        for attempt in range(self.DOWNLOAD_TRIES):
            if attempt:
                time.sleep(2 * attempt)
            try:
                self._download_once(u, dest)
                return
            except (OSError, RuntimeError) as e:   # URLError, таймаут, обрыв, IncompleteRead, битый файл
                last = e
                try:
                    os.remove(dest)
                except OSError:
                    pass
        raise RuntimeError(f"не скачался ({self.DOWNLOAD_TRIES} попытки): {getattr(last, 'reason', None) or last}")

    def _download_once(self, u, dest):
        req = urllib.request.Request(u["url"], headers={"User-Agent": sources.UA})
        h512, h256, h1 = hashlib.sha512(), hashlib.sha256(), hashlib.sha1()
        with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as out:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                out.write(chunk)
                h512.update(chunk); h256.update(chunk); h1.update(chunk)
        if u.get("sha512") and h512.hexdigest() != u["sha512"]:
            raise RuntimeError("файл скачался с ошибкой (не сошёлся отпечаток)")
        if u.get("sha256") and h256.hexdigest() != u["sha256"]:
            raise RuntimeError("файл скачался с ошибкой (не сошёлся отпечаток)")
        if u.get("size") and os.path.getsize(dest) != u["size"]:
            raise RuntimeError("файл скачался не целиком")

    @staticmethod
    def _same_file(fp, u):
        """Лежащий файл — ровно тот, что надо скачать (по размеру и отпечатку)? Тогда качать не нужно."""
        try:
            if not os.path.isfile(fp) or (u.get("size") and os.path.getsize(fp) != u["size"]):
                return False
            algo = "sha512" if u.get("sha512") else "sha256" if u.get("sha256") else "sha1" if u.get("sha1") else ""
            if not algo:
                return bool(u.get("size"))
            h = hashlib.new(algo)
            with open(fp, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 16), b""):
                    h.update(chunk)
            return h.hexdigest() == u[algo]
        except OSError:
            return False

    def rollback(self, path):
        """Вернуть последнее обновление этой папки."""
        b = self.list_backups(path)
        if not b:
            return {"ok": False, "error": "Бэкапов нет — откатывать нечего."}
        bdir = os.path.join(self.backup_root(path), b[0])
        man = json.load(open(os.path.join(bdir, "manifest.json"), encoding="utf-8"))
        restored = []
        game = game_dir_of(path)
        for it in (man.get("settings") or []) if game else []:   # перенос: скопированные настройки — как было
            fp = os.path.join(game, it["rel"])
            if os.path.isfile(fp):
                os.remove(fp)
            bk = os.path.join(bdir, "settings", it["rel"])
            if it.get("backup") and os.path.isfile(bk):
                os.makedirs(os.path.dirname(fp), exist_ok=True)
                shutil.move(bk, fp)
        for name in man.get("added", []):     # перенос на другую версию: скачанные файлы убрать
            fp = os.path.join(path, name)
            if os.path.exists(fp):
                os.remove(fp)
        for it in man.get("items", []):
            if not it.get("new"):              # перенос: стоявший до него jar — просто вернуть
                shutil.move(os.path.join(bdir, it["old"]), os.path.join(path, it["old"]))
                restored.append(it["old"])
                continue
            new = os.path.join(path, it["new"])
            if os.path.exists(new):
                os.remove(new)
            prev = os.path.join(bdir, it["new"] + ".prev")
            if os.path.exists(prev):
                shutil.move(prev, new)
            shutil.move(os.path.join(bdir, it["old"]), os.path.join(path, it["old"]))
            restored.append(it["old"])
        shutil.rmtree(bdir, ignore_errors=True)
        self.check(only=[path])
        return {"ok": True, "restored": restored, "at": man.get("at", "")}

    # ── экспорт сборки ──

    LOADER_DEP = {"fabric": "fabric-loader", "quilt": "quilt-loader", "forge": "forge", "neoforge": "neoforge"}
    # Внутри папок настроек — кэши, а не настройки (у Skyblocker/SkyHanni/NEU это десятки тысяч файлов).
    CONFIG_SKIP_DIRS = {"repo", "repos", "cache", "caches", "backup", "backups", "logs", "log", "temp", "tmp",
                        "downloads", "data", "storage", "screenshots", "profiles", "notenoughupdates-repo"}
    CONFIG_MAX_FILE = 1_000_000

    @staticmethod
    def _norm(x):
        return "".join(ch for ch in x.lower() if ch.isalnum())

    def config_entries(self, cfg_dir, mod_ids):
        """Файлы настроек ТОЛЬКО выбранных модов: запись в config относится к моду, если её имя начинается с id мода
        (skyblocker/, sodium-options.json, MouseTweaks.cfg). Всё прочее — данные нашего мода с никами аккаунтов,
        чужие прокси, остатки удалённых модов — в сборку не идёт. Кэши и файлы больше 1 МБ пропускаются."""
        ids = sorted({self._norm(i) for i in mod_ids if len(self._norm(i)) >= 3}, key=len, reverse=True)
        out, skipped = [], 0
        try:
            top = os.listdir(cfg_dir)
        except OSError:
            return out, 0
        for name in top:
            base = self._norm(os.path.splitext(name)[0])
            if not any(base.startswith(i) for i in ids):
                continue
            full = os.path.join(cfg_dir, name)
            if os.path.isfile(full):
                if os.path.getsize(full) <= self.CONFIG_MAX_FILE:
                    out.append(full)
                else:
                    skipped += 1
                continue
            for root, dirs, names in os.walk(full):
                dirs[:] = [d for d in dirs if d.lower() not in self.CONFIG_SKIP_DIRS and not d.startswith(".")
                           and "repo" not in d.lower() and "cache" not in d.lower()]   # item-repo, NEU-REPO, imagecache…
                for n in names:
                    fp = os.path.join(root, n)
                    try:
                        if os.path.getsize(fp) <= self.CONFIG_MAX_FILE:
                            out.append(fp)
                        else:
                            skipped += 1
                    except OSError:
                        pass
        return out, skipped

    def export_info(self, path):
        f = self.folders.get(path) or scan.load_mods(scan.describe_folder(path, self._launcher_of(path)))
        rows = self.rows.get(path) or []
        ignore = [x.lower() for x in self.cfg["ignore"]]
        mods = [{"key": r["key"], "name": r["name"], "id": r["id"], "filename": r["filename"], "source": r["source"],
                 "link": bool(r.get("mr_file")),
                 "private": (r["id"] or "").lower() in ignore and r["id"] != "essential-dependencies"}
                for r in rows]
        cfg_dir = os.path.join(os.path.dirname(os.path.normpath(path)), "config")
        return {"name": f.name, "game": self.cfg["game_version"].get(path) or f.game_version,
                "loader": f.loader or "fabric", "loader_version": f.loader_version, "mods": mods,
                "has_config": os.path.isdir(cfg_dir), "checked": bool(rows)}

    def export_pack(self, path, dest, name, version, keys, include_config, fmt="mrpack"):
        """Сборка из выбранных модов: .mrpack (моды с Modrinth — ссылками, остальные — файлами в overrides)
        или обычный .zip с jar. keys — какие моды включить."""
        f = self.folders.get(path) or scan.load_mods(scan.describe_folder(path, self._launcher_of(path)))
        rows = {r["key"]: r for r in (self.rows.get(path) or [])}
        chosen = [rows[k] for k in keys if k in rows]
        if not chosen:
            return {"ok": False, "error": "Не выбрано ни одного мода"}
        game = self.cfg["game_version"].get(path) or f.game_version
        cfg_dir = os.path.join(os.path.dirname(os.path.normpath(path)), "config")
        tmp = dest + ".part"
        linked = embedded = cfg_files = 0
        self._set_progress("Собираю сборку…")
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
                if fmt == "mrpack":
                    files = []
                    for r in chosen:
                        mr = r.get("mr_file")
                        if mr:
                            files.append({"path": "mods/" + r["filename"],
                                          "hashes": {"sha1": mr["sha1"], "sha512": mr["sha512"]},
                                          "downloads": [mr["url"]], "fileSize": mr["size"]})
                            linked += 1
                        else:
                            z.write(os.path.join(path, r["filename"]), "overrides/mods/" + r["filename"])
                            embedded += 1
                    deps = {"minecraft": game}
                    loader = f.loader or "fabric"
                    if f.loader_version:
                        deps[self.LOADER_DEP.get(loader, loader)] = f.loader_version
                    index = {"formatVersion": 1, "game": "minecraft", "versionId": version or "1.0",
                             "name": name or f.name, "summary": f"Собрано в ModUp: {len(chosen)} модов, Minecraft {game}",
                             "files": files, "dependencies": deps}
                    z.writestr("modrinth.index.json", json.dumps(index, ensure_ascii=False, indent=2))
                    prefix = "overrides/"
                else:
                    for r in chosen:
                        z.write(os.path.join(path, r["filename"]), r["filename"])   # в корне: распаковать прямо в mods
                        embedded += 1
                    prefix = ""
                if include_config and os.path.isdir(cfg_dir):
                    base = os.path.dirname(cfg_dir)
                    entries, _skipped = self.config_entries(cfg_dir, [r["id"] for r in chosen if r["id"]])
                    for full in entries:
                        try:
                            z.write(full, prefix + os.path.relpath(full, base).replace(os.sep, "/"))
                            cfg_files += 1
                        except OSError:
                            pass
            os.replace(tmp, dest)
        finally:
            self.progress = {"busy": False, "text": ""}
            if os.path.exists(tmp):
                os.remove(tmp)
        return {"ok": True, "path": dest, "linked": linked, "embedded": embedded, "config_files": cfg_files,
                "size": os.path.getsize(dest), "loader_version": f.loader_version, "fmt": fmt}

    # ── перенос на другую версию игры ──

    def migrate_plan(self, path, target):
        """Что скачать, чтобы собрать эту папку под версию target. Исходную папку не меняет."""
        target = (target or "").strip()
        if not target:
            return {"ok": False, "error": "Укажи версию игры"}
        f = scan.load_mods(scan.describe_folder(path, self._launcher_of(path)))
        loader = f.loader or "fabric"
        self._set_progress(f"Читаю {len(f.mods)} модов…")
        try:
            ident = sources.modrinth_identify(sorted({m.sha1 for m in f.mods}))
            projects = sources.modrinth_projects({v["project_id"] for v in ident.values()})
        except sources.SourceError as e:
            self.progress = {"busy": False, "text": ""}
            return {"ok": False, "error": f"Modrinth: {e}"}

        def fetch(pid):
            try:
                return pid, sources.modrinth_versions(pid, loader, target)
            except sources.SourceError as e:
                return pid, e

        def fetch_gh(repo):
            try:
                return repo, sources.github_releases(repo, self.cfg["github_token"], self.gh_cache)
            except sources.SourceError as e:
                return repo, e

        live = [m for m in f.mods if not self._ignored(m)]
        guessed = self._guess_modrinth(live, ident, loader)
        mr_ids = {ident[m.sha1]["project_id"] for m in live if m.sha1 in ident}
        slugs = {m.modrinth_slug for m in live
                 if m.sha1 not in ident and m.modrinth_slug and (m.filename in guessed or not self._repo_of(m))}
        repos = {self._repo_of(m) for m in live
                 if m.sha1 not in ident and m.filename not in guessed and self._repo_of(m)}
        self._set_progress(f"Ищу сборки под {target} ({len(mr_ids) + len(slugs)} на Modrinth, {len(repos)} на GitHub)…")
        mr = dict(sources.run_parallel(fetch, sorted(mr_ids | slugs)))
        gh = dict(sources.run_parallel(fetch_gh, sorted(repos), workers=4))
        self._save_gh_cache()

        rows = []
        have_pids = {v["project_id"] for v in ident.values()}
        for m in f.mods:
            row = {"key": m.filename, "name": m.name, "version": m.version or "", "source": "file",
                   "status": "missing", "note": "", "to": None, "sel": False, "page": ""}
            v = ident.get(m.sha1)
            if self._ignored(m):
                row.update(status="skip", note="в списке пропускаемых — перенеси сам")
                rows.append(row)
                continue
            if v:
                p = projects.get(v["project_id"], {})
                row.update(source="modrinth", name=p.get("title") or m.name,
                           version=v.get("version_number") or row["version"],
                           page=f"https://modrinth.com/mod/{p.get('slug') or v['project_id']}")
                if target in (v.get("game_versions") or []):
                    row.update(status="same", sel=True, note=f"этот же файл подходит и для {target}")
                else:
                    self._fill_mr(row, mr.get(v["project_id"]), target,
                                  newest=self.cfg["allow_pre"] or v.get("version_type", "release") != "release")
                    repo = self._repo_of(m)
                    if row["status"] == "missing" and repo:
                        # На Modrinth под новую версию ещё нет, а на GitHub сборка бывает раньше (Firmament).
                        rel = fetch_gh(repo)[1]
                        if not isinstance(rel, Exception):
                            pre = self.cfg["allow_pre"] or bool(re.search(r"(?i)(alpha|beta|pre|rc|snapshot)", m.version or ""))
                            upd, sure = migrate.pick_github(rel, loader, target, newest=pre)
                            if upd and sure:
                                row.update(to=upd, sel=True, source="github", page=f"https://github.com/{repo}", note="с GitHub",
                                           status="beta" if upd["type"] != "release" else "ready")
                            else:
                                row["note"] = f"нет сборки под {target} ни на Modrinth, ни на GitHub"
            elif m.modrinth_slug and (m.filename in guessed or not self._repo_of(m)):
                row.update(source="modrinth", page=f"https://modrinth.com/mod/{m.modrinth_slug}")
                self._fill_mr(row, mr.get(m.modrinth_slug), target)
                if m.filename in guessed and row["status"] != "error":
                    row["note"] = (row["note"] + " · " if row["note"] else "") + "файл не с Modrinth — нашёл мод по названию"
                repo = self._repo_of(m)
                if row["status"] == "missing" and repo:
                    rel = fetch_gh(repo)[1]
                    if not isinstance(rel, Exception):
                        pre = self.cfg["allow_pre"] or bool(re.search(r"(?i)(alpha|beta|pre|rc|snapshot)", m.version or ""))
                        upd, sure = migrate.pick_github(rel, loader, target, newest=pre)
                        if upd and sure:
                            row.update(to=upd, sel=True, source="github", page=f"https://github.com/{repo}", note="с GitHub",
                                       status="beta" if upd["type"] != "release" else "ready")
            elif self._repo_of(m):
                repo = self._repo_of(m)
                row.update(source="github", page=f"https://github.com/{repo}")
                rel = gh.get(repo)
                if isinstance(rel, Exception):
                    row.update(status="error", note=str(rel))
                else:
                    pre = self.cfg["allow_pre"] or bool(re.search(r"(?i)(alpha|beta|pre|rc|snapshot)", m.version or ""))
                    upd, sure = migrate.pick_github(rel, loader, target, newest=pre)
                    if upd and sure:
                        row.update(to=upd, sel=True, status="beta" if upd["type"] != "release" else "ready")
                    elif upd:
                        row.update(to=upd, status="manual",
                                   note=f"в новейшем релизе не указана версия игры — проверь, что он под {target}")
                    else:
                        row["note"] = f"в релизах на GitHub нет сборки под {target}"
            if row["status"] == "missing" and not row["note"]:
                if m.mc_dep is not None and versions.mc_dependency_ok(m.mc_dep, target):
                    row.update(status="same", sel=True, note=f"по описанию мода подходит и для {target}")
                else:
                    row["note"] = "источник неизвестен (нет на Modrinth и не указан GitHub)"
            rows.append(row)

        # Обязательные зависимости новых версий, которых в сборке нет (например, новая библиотека).
        dep_ids = {}
        for r in rows:
            for d in (r.get("deps") or []):
                if d not in have_pids:
                    dep_ids.setdefault(d, []).append(r["name"])
        if dep_ids:
            self._set_progress(f"Проверяю зависимости ({len(dep_ids)})…")
            dep_mr = dict(sources.run_parallel(fetch, sorted(dep_ids)))
            try:
                dep_projects = sources.modrinth_projects(set(dep_ids))
            except sources.SourceError:
                dep_projects = {}
            for pid, need in dep_ids.items():
                p = dep_projects.get(pid, {})
                row = {"key": "dep:" + pid, "name": p.get("title") or pid, "version": "", "source": "modrinth",
                       "status": "missing", "note": "", "to": None, "sel": False, "dep": True,
                       "page": f"https://modrinth.com/mod/{p.get('slug') or pid}"}
                self._fill_mr(row, dep_mr.get(pid), target)
                row["note"] = "нужна для: " + ", ".join(sorted(set(need))) + (f" — {row['note']}" if row["note"] else "")
                row.pop("deps", None)
                rows.append(row)

        for r in rows:
            r.pop("deps", None)
        self._migration = {"path": path, "target": target, "loader": loader, "rows": {r["key"]: r for r in rows}}
        self.progress = {"busy": False, "text": ""}
        order = {"ready": 0, "beta": 1, "same": 2, "manual": 3, "missing": 4, "error": 5, "skip": 6}
        rows.sort(key=lambda r: (order.get(r["status"], 9), r["name"].lower()))
        src_n = os.path.normcase(os.path.normpath(path))
        suggest = [{"path": d.path, "name": d.name, "launcher": d.launcher, "jars": self._jar_count(d.path)}
                   for d in scan.discover()
                   if d.game_version == target and (d.loader or "fabric") == loader
                   and os.path.normcase(os.path.normpath(d.path)) != src_n]
        return {"ok": True, "target": target, "loader": loader, "name": f.name,
                "from": self.cfg["game_version"].get(path) or f.game_version, "rows": rows, "suggest": suggest,
                "extras": self.settings_info(path)}

    def _guess_modrinth(self, mods, ident, loader):
        """Моды, которых Modrinth не узнал по отпечатку (скачаны не с Modrinth): ищем проект по id мода.
        Ставит m.modrinth_slug, возвращает имена файлов, найденных так. Берём, только если проект под
        этот загрузчик и его название похоже на имя мода (короткий id мог совпасть с чужим проектом)."""
        want = {}
        for m in mods:
            if m.sha1 in ident or m.modrinth_slug or not m.mod_id:
                continue
            want[m.filename] = MOD_ALIASES.get(m.mod_id.lower(), m.mod_id.lower())
        if not want:
            return set()
        try:
            projs = sources.modrinth_projects(set(want.values()))
        except sources.SourceError:
            return set()
        by_slug = {(p.get("slug") or "").lower(): p for p in projs.values()}
        found = set()
        for m in mods:
            slug = want.get(m.filename)
            p = by_slug.get(slug or "")
            if not p or p.get("project_type", "mod") != "mod" or loader not in (p.get("loaders") or []):
                continue
            t, n = self._norm(p.get("title") or ""), self._norm(m.name or "")
            if not (m.mod_id.lower() in MOD_ALIASES or (t and n and (t in n or n in t))):
                continue
            # В моде указан GitHub — проект должен вести туда же: id и название совпадают и у чужих модов
            # (ChatPlus от ebicep — «chat-plus», а «chatplus» на Modrinth — другой ChatPlus).
            src = (p.get("source_url") or "").lower()
            if m.github and m.github.lower() not in src:
                continue
            m.modrinth_slug = p["slug"]
            found.add(m.filename)
        return found

    def settings_info(self, mods_path):
        """Что из папки игры можно перенести вместе с модами: [{key, label, files, size}] (только то, что есть)."""
        game = game_dir_of(mods_path)
        if not game:
            return []
        out = []
        for key, label, names, is_dir in SETTINGS_ITEMS:
            files = size = 0
            for _src, _rel, sz in self._settings_files(game, key, names, is_dir):
                files += 1
                size += sz
            if files:
                out.append({"key": key, "label": label, "files": files, "size": size})
        return out

    @staticmethod
    def _settings_files(game, key, names, is_dir):
        """(путь, путь относительно папки игры, размер) — файлы пункта переноса."""
        for name in names:
            base = os.path.join(game, name)
            if not is_dir:
                if os.path.isfile(base):
                    yield base, name, os.path.getsize(base)
                continue
            if not os.path.isdir(base):
                continue
            for root, dirs, fnames in os.walk(base):
                if key == "config":
                    dirs[:] = [d for d in dirs if d.lower() not in MIGRATE_SKIP_DIRS and not d.lower().endswith("-repo")]
                for fn in fnames:
                    fp = os.path.join(root, fn)
                    try:
                        yield fp, os.path.relpath(fp, game), os.path.getsize(fp)
                    except OSError:
                        continue

    def _copy_settings(self, src_game, dst_game, keys, bdir):
        """Скопировать выбранные пункты; что в новой папке уже было и отличалось — в бэкап (откат вернёт)."""
        import filecmp
        done, failed, man = [], [], []
        for key, label, names, is_dir in SETTINGS_ITEMS:
            if key not in keys:
                continue
            n = 0
            for src, rel, _sz in self._settings_files(src_game, key, names, is_dir):
                dst = os.path.join(dst_game, rel)
                try:
                    existed = os.path.isfile(dst)
                    if existed and filecmp.cmp(src, dst, shallow=True):
                        continue
                    if existed:
                        bk = os.path.join(bdir, "settings", rel)
                        os.makedirs(os.path.dirname(bk), exist_ok=True)
                        shutil.copy2(dst, bk)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
                    man.append({"rel": rel, "backup": existed})
                    n += 1
                except OSError as e:
                    failed.append(f"{label}: {rel}: {e}")
            if n:
                done.append(f"{label}: {n} {self._files_word(n)}")
        return done, failed, man

    @staticmethod
    def _files_word(n):
        n10, n100 = n % 10, n % 100
        return "файл" if n10 == 1 and n100 != 11 else "файла" if 2 <= n10 <= 4 and not 12 <= n100 <= 14 else "файлов"

    def _fill_mr(self, row, cands, target, newest=False):
        if isinstance(cands, Exception):
            row.update(status="error", note=str(cands))
            return
        v = migrate.pick_modrinth(cands, newest)
        if not v:
            row["note"] = f"на Modrinth нет сборки под {target}"
            return
        upd = migrate.modrinth_file(v)
        if not upd:
            row["note"] = "у версии на Modrinth нет файла"
            return
        row.update(to=upd, sel=True, status="ready" if upd["type"] == "release" else "beta")
        row["deps"] = [d["project_id"] for d in (v.get("dependencies") or [])
                       if d.get("dependency_type") == "required" and d.get("project_id")]

    @staticmethod
    def _jar_count(path):
        try:
            return sum(1 for n in os.listdir(path) if n.lower().endswith(".jar"))
        except OSError:
            return 0

    def dest_info(self, dest):
        picked = os.path.normpath(dest)
        dest = resolve_mods_dir(picked)
        return {"path": dest, "picked": picked, "jars": self._jar_count(dest), "exists": os.path.isdir(dest)}

    def migrate_apply(self, dest, keys, extras=None, retry=False):
        """Скачать выбранное в папку dest. Стоявшие там jar — в бэкап (откат вернёт как было).
        extras — ключи SETTINGS_ITEMS: что ещё перенести из папки игры (настройки, серверы, паки…).
        Уже лежащий в dest нужный файл (тот же отпечаток) не перекачивается — повторный перенос после
        обрыва связи качает только недостающее. retry=True — «Докачать»: только keys, остальное в папке не трогаем."""
        mg = getattr(self, "_migration", None)
        if not mg:
            return {"ok": False, "error": "Сначала подбери моды"}
        src = mg["path"]
        dest = resolve_mods_dir(dest)
        if not dest or dest == ".":
            return {"ok": False, "error": "Не выбрана папка"}
        if os.path.normcase(dest) == os.path.normcase(os.path.normpath(src)):
            return {"ok": False, "error": "Это та же папка, откуда переносим — выбери папку mods инстанса новой версии"}
        todo = [mg["rows"][k] for k in keys
                if k in mg["rows"] and (mg["rows"][k]["to"] or mg["rows"][k]["status"] == "same")]
        if not todo:
            return {"ok": False, "error": "Ничего не выбрано"}
        os.makedirs(dest, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        bdir = os.path.join(self.backup_root(dest), stamp)
        tmpdir = os.path.join(self.backup_root(dest), ".download")
        os.makedirs(tmpdir, exist_ok=True)

        def want(r):   # что должно лечь в dest: (имя файла, описание для сверки)
            if r["status"] == "same":
                fp = os.path.join(src, r["key"])
                try:
                    return r["key"], {"size": os.path.getsize(fp), "sha1": self._sha1_of(fp)}
                except OSError:
                    return r["key"], None
            return r["to"]["filename"], r["to"]

        wanted = {r["key"]: want(r) for r in todo}
        have = set()   # ключи, чей файл уже лежит в dest целым
        for r in todo:
            name, u = wanted[r["key"]]
            if u and self._same_file(os.path.join(dest, name), u):
                have.add(r["key"])
        keep_names = {wanted[k][0].lower() for k in have}
        moved = []
        if not retry:
            for n in sorted(os.listdir(dest)):
                if n.lower().endswith(".jar") and os.path.isfile(os.path.join(dest, n)) and n.lower() not in keep_names:
                    os.makedirs(bdir, exist_ok=True)
                    shutil.move(os.path.join(dest, n), os.path.join(bdir, n))
                    moved.append({"old": n, "new": ""})
        done, failed, added, failed_keys, kept = [], [], [], [], []
        for i, r in enumerate(todo, 1):
            if r["key"] in have:
                kept.append(r["name"])
                done.append(f"{r['name']} — уже был скачан")
                continue
            self._set_progress(f"Скачиваю {r['name']} ({i}/{len(todo)})…")
            try:
                name = wanted[r["key"]][0]
                fp = os.path.join(dest, name)
                if os.path.exists(fp):   # «Докачать»: на месте битый/другой файл с тем же именем — в бэкап
                    os.makedirs(bdir, exist_ok=True)
                    shutil.move(fp, os.path.join(bdir, name))
                    moved.append({"old": name, "new": ""})
                if r["status"] == "same":
                    shutil.copy2(os.path.join(src, name), fp)
                    done.append(f"{r['name']} {r['version']} (тот же файл)")
                else:
                    u = r["to"]
                    tmp = os.path.join(tmpdir, name)
                    self._download(u, tmp)
                    shutil.move(tmp, fp)
                    done.append(f"{r['name']} → {u['version']}")
                added.append(name)
            except Exception as e:  # одна неудача не ломает остальные
                failed.append(f"{r['name']}: {e}")
                failed_keys.append(r["key"])
        settings_done, settings_man = [], []
        src_game, dst_game = game_dir_of(src), game_dir_of(dest)
        if extras and not retry and src_game and dst_game and os.path.normcase(src_game) != os.path.normcase(dst_game):
            self._set_progress("Переношу настройки, серверы, паки и скриншоты…")
            settings_done, s_failed, settings_man = self._copy_settings(src_game, dst_game, set(extras), bdir)
            failed += s_failed
        if moved or added or settings_man:
            os.makedirs(bdir, exist_ok=True)
            with open(os.path.join(bdir, "manifest.json"), "w", encoding="utf-8") as fh:
                json.dump({"at": _now_iso(), "kind": "migrate", "from": src, "target": mg["target"],
                           "items": moved, "added": added, "settings": settings_man}, fh, ensure_ascii=False, indent=2)
            self._write_report(bdir, mg, keys, done, failed, settings_done, retry=retry)
        shutil.rmtree(tmpdir, ignore_errors=True)
        self.progress = {"busy": False, "text": ""}
        known = {os.path.normcase(os.path.normpath(p)) for p in self.cfg["folders"]}
        if os.path.normcase(dest) not in known:
            self.add_folders([dest])
        if not scan.describe_folder(dest, self._launcher_of(dest)).game_version:
            self.cfg["game_version"][dest] = mg["target"]   # папка не из инстанса — версию игры не угадать
            self.cfg.save()
        left = [] if retry else sorted(r["name"] for r in mg["rows"].values()
                                       if r["key"] not in keys and r["status"] in ("missing", "error", "manual"))
        return {"ok": bool(done), "done": done, "failed": failed, "failed_keys": failed_keys, "kept": len(kept),
                "moved": len(moved), "dest": dest, "left": left, "settings": settings_done, "retry": retry,
                "report": os.path.join(bdir, "report.txt") if os.path.isdir(bdir) else ""}

    @staticmethod
    def _sha1_of(fp):
        h = hashlib.sha1()
        with open(fp, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _write_report(bdir, mg, keys, done, failed, settings_done, retry=False):
        """Отчёт переноса рядом с бэкапом: что перенесено, что нет и почему — можно переслать, если что-то не так."""
        chosen = set(keys)
        lines = [f"ModUp — {'докачка' if retry else 'перенос'} на Minecraft {mg['target']} ({mg['loader']}), {_now_iso()}",
                 f"Откуда: {mg['path']}", ""]
        lines += ["Перенесено:"] + [f"  + {x}" for x in done] + [""]
        if settings_done:
            lines += ["Настройки и файлы:"] + [f"  + {x}" for x in settings_done] + [""]
        if failed:
            lines += ["Не вышло:"] + [f"  ! {x}" for x in failed] + [""]
        rest = [] if retry else [r for r in mg["rows"].values() if r["key"] not in chosen]
        if rest:
            lines.append("Не перенесено:")
            for r in sorted(rest, key=lambda r: r["name"].lower()):
                why = r["note"] or ("снята галочка" if (r["to"] or r["status"] == "same") else r["status"])
                lines.append(f"  - {r['name']} [{r['key']}] — {why}")
        try:
            with open(os.path.join(bdir, "report.txt"), "w", encoding="utf-8-sig") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError:
            pass

    # ── настройки мода ──

    def set_github(self, mod_id, url):
        repo = scan.github_repo(url) or (url.strip() if url.count("/") == 1 else "")
        if not repo:
            return {"ok": False, "error": "Нужна ссылка вида https://github.com/автор/мод"}
        self.cfg["github"][mod_id] = repo
        self.cfg.save()
        return {"ok": True, "repo": repo}

    def ignore(self, mod_id, on=True):
        lst = self.cfg["ignore"]
        if on and mod_id not in lst:
            lst.append(mod_id)
        if not on and mod_id in lst:
            lst.remove(mod_id)
        self.cfg.save()
        return {"ok": True}

    def skip_version(self, group, version):
        self.cfg["skip"][group] = version
        self.cfg.save()
        return {"ok": True}

    def set_game_version(self, path, version):
        if version:
            self.cfg["game_version"][path] = version
        else:
            self.cfg["game_version"].pop(path, None)
        self.cfg.save()
        return {"ok": True}
