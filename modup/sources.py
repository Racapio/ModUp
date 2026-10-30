"""Где искать обновления: Modrinth (по отпечатку файла) и GitHub Releases (по ссылке из мода или указанной вручную)."""
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import versions

from .version import VERSION

UA = f"ModUp/{VERSION} (mod updater; github.com/Racapio/ModUp)"
MODRINTH = "https://api.modrinth.com/v2"
GITHUB = "https://api.github.com"


class SourceError(Exception):
    pass


def http_json(url, data=None, headers=None, timeout=25):
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        detail = ""
        if e.code == 403 and "github" in url:
            detail = " (лимит GitHub: 60 запросов в час без токена — добавь токен в настройках)"
        raise SourceError(f"HTTP {e.code}{detail}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SourceError(f"нет связи ({getattr(e, 'reason', e)})") from None


# ── Modrinth ──

_TYPE_RANK = {"release": 0, "beta": 1, "alpha": 2}


def modrinth_identify(sha1_list):
    """sha1 → версия Modrinth (project_id, id, version_number, version_type, date_published…)."""
    if not sha1_list:
        return {}
    out = {}
    for i in range(0, len(sha1_list), 500):
        part = http_json(f"{MODRINTH}/version_files", {"hashes": sha1_list[i:i + 500], "algorithm": "sha1"}) or {}
        out.update(part)
    return out


def modrinth_projects(ids):
    if not ids:
        return {}
    q = urllib.parse.quote(json.dumps(sorted(ids)))
    res = http_json(f"{MODRINTH}/projects?ids={q}") or []
    return {p["id"]: p for p in res}


def modrinth_versions(project_id, loader, game_version):
    params = {}
    if loader:
        params["loaders"] = json.dumps([loader])
    if game_version:
        params["game_versions"] = json.dumps([game_version])
    url = f"{MODRINTH}/project/{project_id}/version"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return http_json(url) or []


def pick_modrinth_update(current, candidates, allow_pre):
    """Новейшая подходящая версия новее текущей; пред-релизы — если разрешены или сам мод сейчас пред-релиз."""
    cur_rank = _TYPE_RANK.get(current.get("version_type", "release"), 0)
    max_rank = 2 if allow_pre else cur_rank
    best = None
    for v in candidates:
        if _TYPE_RANK.get(v.get("version_type", "release"), 0) > max_rank:
            continue
        if best is None or v.get("date_published", "") > best.get("date_published", ""):
            best = v
    if not best or best["id"] == current["id"]:
        return None
    if best.get("date_published", "") <= current.get("date_published", ""):
        return None
    files = best.get("files") or []
    f = next((x for x in files if x.get("primary")), files[0] if files else None)
    if not f:
        return None
    return {
        "version": best.get("version_number", ""),
        "type": best.get("version_type", "release"),
        "url": f["url"],
        "filename": f["filename"],
        "size": f.get("size", 0),
        "sha512": (f.get("hashes") or {}).get("sha512", ""),
        "sha1": (f.get("hashes") or {}).get("sha1", ""),
        "changelog": (best.get("changelog") or "")[:4000],
        "page": "",
        "published": best.get("date_published", ""),
    }


# ── GitHub ──

def github_releases(repo, token="", cache=None):
    """Релизы репозитория. cache — dict {repo: {etag, data, at}}: ответ моложе 10 мин берём без запроса,
    старше — спрашиваем с If-None-Match (ответ 304 «не изменилось» GitHub не считает в лимит 60/час),
    а если лимит всё же кончился — отдаём последний сохранённый."""
    import time as _t
    c = cache.get(repo) if cache is not None else None
    if c and _t.time() - c.get("at", 0) < 600:
        return c["data"]
    h = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    if c and c.get("etag"):
        h["If-None-Match"] = c["etag"]
    req = urllib.request.Request(f"{GITHUB}/repos/{repo}/releases?per_page=15", headers=h)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.loads(r.read().decode("utf-8") or "[]")
            if cache is not None:
                cache[repo] = {"etag": r.headers.get("ETag", ""), "data": data, "at": _t.time()}
            return data
    except urllib.error.HTTPError as e:
        if e.code == 304 and c:
            c["at"] = _t.time()
            return c["data"]
        if e.code == 404:
            raise SourceError("репозиторий не найден") from None
        if e.code in (403, 429) and c:
            return c["data"]          # лимит — показываем последнее известное
        if e.code in (403, 429):
            raise SourceError("лимит GitHub (60 проверок в час без токена) — подожди или добавь токен в настройках") from None
        raise SourceError(f"HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        if c:
            return c["data"]
        raise SourceError(f"нет связи ({getattr(e, 'reason', e)})") from None


_BAD_ASSET = re.compile(r"(?i)(-sources|-dev|-javadoc|-slim|-shadow-dev)\.jar$")
_OTHER_LOADER = {"fabric": ("forge", "neoforge", "quilt"), "quilt": ("forge", "neoforge"),
                 "forge": ("fabric", "neoforge", "quilt"), "neoforge": ("fabric", "quilt")}


def _asset_ok(name, loader):
    n = name.lower()
    if not n.endswith(".jar") or _BAD_ASSET.search(n):
        return False
    for other in _OTHER_LOADER.get(loader, ()):
        if re.search(rf"(?<![a-z]){other}(?![a-z])", n) and loader not in n:
            return False
    return True


def asset_version(name, tag):
    """Версия мода из имени файла или тега: берём первое число вида x.y(.z…), которое не версия игры."""
    for src in (tag, name):
        for m in re.finditer(r"(?<![\d.])v?(\d+(?:\.\d+)+)", src or ""):
            t = versions.vtuple(m.group(1))
            if not versions.looks_like_mc(t):
                return m.group(1)
    return versions.core_version(tag or name)


def pick_github_update(installed_version, installed_filename, releases, loader, game_version, allow_pre):
    """Новейший релиз с jar под эту версию игры и загрузчик, новее установленного. Или None.
    Второе значение — понятная причина, если подходящей сборки нет (для подсказки в окне)."""
    inst_is_pre = bool(re.search(r"(?i)(alpha|beta|pre|rc|snapshot)", installed_version or "")) or         _installed_is_prerelease_tag(installed_version, releases)
    installed_has_mc = bool(versions.mc_tokens(installed_filename))
    newest_seen = ""
    for rel in releases:
        if rel.get("draft"):
            continue
        tag = rel.get("tag_name", "") or ""
        if rel.get("prerelease") and not (allow_pre or inst_is_pre):
            continue
        tag_fit = versions.name_fits_game(tag, game_version)
        chosen = None
        for a in rel.get("assets") or []:
            name = a.get("name", "")
            if not _asset_ok(name, loader):
                continue
            fit = versions.name_fits_game(name, game_version)
            if fit is False:
                continue
            if fit is None and not (tag_fit is True or (tag_fit is None and not installed_has_mc)):
                continue
            chosen = a
            break
        if not chosen:
            continue
        ver = asset_version(chosen["name"], tag)
        newest_seen = newest_seen or ver
        if versions.compare(ver, installed_version or "0") <= 0:
            return None, ""
        digest = chosen.get("digest") or ""
        return {
            "version": ver,
            "type": "beta" if rel.get("prerelease") else "release",
            "url": chosen["browser_download_url"],
            "filename": chosen["name"],
            "size": chosen.get("size", 0),
            "sha256": digest.split(":", 1)[1] if digest.startswith("sha256:") else "",
            "changelog": (rel.get("body") or "")[:4000],
            "page": rel.get("html_url", ""),
            "published": rel.get("published_at", ""),
        }, ""
    return None, f"в релизах нет сборки под {game_version or 'эту версию'}"


def _installed_is_prerelease_tag(installed_version, releases):
    """Установлена версия, которая на GitHub помечена пред-релизом (SkyHanni 9.1.0) — тогда и обновления
    среди пред-релизов разрешены."""
    core = versions.core_version(installed_version)
    for rel in releases:
        if rel.get("prerelease") and versions.compare(asset_version("", rel.get("tag_name", "")), core) == 0:
            return True
    return False


def run_parallel(fn, items, workers=6):
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, items))
