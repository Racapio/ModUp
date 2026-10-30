"""Самообновление ModUp: новая версия — релиз в публичном репозитории GitHub (Racapio/ModUp).

При запуске окно спрашивает check(); по кнопке «Обновить» — apply(): скачать ModUp.exe из релиза, сверить
SHA-256 (GitHub публикует его у каждого файла релиза), переименовать запущенный exe в ModUp.old.exe
(Windows не даёт перезаписать запущенный файл, но переименовать — даёт), положить новый на его место и
запустить. Старый удаляется при следующем запуске (cleanup_old). Что-то не так — остаётся старая версия.
"""
import hashlib
import os
import re
import subprocess
import sys
import urllib.request

from . import sources
from .version import VERSION

REPO = "Racapio/ModUp"
ASSET = "ModUp.exe"


def _vtuple(s):
    nums = re.findall(r"\d+", s or "")
    return tuple(int(x) for x in nums[:4]) if nums else ()


def is_newer(remote, local=VERSION):
    r, l = _vtuple(remote), _vtuple(local)
    return bool(r) and r > l


def exe_path():
    """Путь запущенного ModUp.exe; None — запуск из исходников (обновлять нечего)."""
    return os.path.abspath(sys.executable) if getattr(sys, "frozen", False) else None


def cleanup_old():
    exe = exe_path()
    if not exe:
        return
    old = os.path.join(os.path.dirname(exe), "ModUp.old.exe")
    try:
        if os.path.exists(old):
            os.remove(old)
    except OSError:
        pass   # прошлый процесс ещё не закрылся — уберём в следующий раз


def check():
    """{available, version, current, notes, url, size, sha256, page} — есть ли версия новее."""
    rel = sources.http_json(f"{sources.GITHUB}/repos/{REPO}/releases/latest",
                            headers={"Accept": "application/vnd.github+json"}) or {}
    tag = rel.get("tag_name") or ""
    asset = next((a for a in rel.get("assets") or [] if a.get("name") == ASSET), None)
    out = {"available": False, "current": VERSION, "version": tag.lstrip("vV"), "notes": (rel.get("body") or "").strip(),
           "page": rel.get("html_url") or f"https://github.com/{REPO}/releases", "can_apply": bool(exe_path())}
    if not asset or rel.get("draft") or rel.get("prerelease") or not is_newer(tag):
        return out
    digest = asset.get("digest") or ""
    out.update(available=True, url=asset["browser_download_url"], size=asset.get("size", 0),
               sha256=digest.split(":", 1)[1] if digest.startswith("sha256:") else "")
    return out


def apply(info, progress=None):
    """Скачать и подменить exe. Возвращает путь нового exe (его надо запустить и закрыть текущий)."""
    exe = exe_path()
    if not exe:
        raise RuntimeError("ModUp запущен не из exe — обновлять нечего")
    if not info.get("sha256"):
        raise RuntimeError("у файла релиза нет отпечатка SHA-256 — не ставлю непроверенный файл")
    folder = os.path.dirname(exe)
    new = os.path.join(folder, "ModUp.new.exe")
    old = os.path.join(folder, "ModUp.old.exe")
    req = urllib.request.Request(info["url"], headers={"User-Agent": sources.UA})
    h = hashlib.sha256()
    got, total = 0, info.get("size") or 0
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(new, "wb") as out:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                out.write(chunk)
                h.update(chunk)
                got += len(chunk)
                if progress and total:
                    progress(f"Скачиваю ModUp {info['version']}… {got * 100 // total}%")
    except PermissionError:
        raise RuntimeError(f"нет прав на запись в папку {folder} — перенеси ModUp.exe, например, на рабочий стол")
    if h.hexdigest() != info["sha256"].lower() or (total and got != total):
        os.remove(new)
        raise RuntimeError("файл скачался с ошибкой (не сошёлся отпечаток) — попробуй ещё раз")
    if os.path.exists(old):
        os.remove(old)
    os.replace(exe, old)          # запущенный exe переименовать можно
    try:
        os.replace(new, exe)
    except OSError:
        os.replace(old, exe)      # не вышло — вернуть как было
        raise
    return exe


def restart(exe):
    """Запустить новый exe отдельным процессом (текущий окно закроет само)."""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([exe], cwd=os.path.dirname(exe), close_fds=True, creationflags=flags)
