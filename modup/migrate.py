"""Перенос сборки на другую версию игры: для каждого мода — сборка под новую версию (Modrinth/GitHub),
недостающие обязательные зависимости, скачивание в ОТДЕЛЬНУЮ папку (исходная не трогается)."""
from . import sources, versions

_RANK = {"release": 0, "beta": 1, "alpha": 2}


def pick_modrinth(cands, newest=False):
    """Новейшая версия под целевую игру: релиз, а если релизов нет — бета/альфа (с пометкой).
    newest — сейчас стоит бета (или пред-релизы разрешены): просто самая свежая, чтобы не откатиться."""
    best = None
    for v in cands or []:
        rank = 0 if newest else -_RANK.get(v.get("version_type", "release"), 0)
        key = (rank, v.get("date_published", ""))
        if best is None or key > best[0]:
            best = (key, v)
    return best[1] if best else None


def modrinth_file(v):
    """Файл версии Modrinth в формате обновления (url, имя, отпечатки) — как pick_modrinth_update."""
    files = v.get("files") or []
    f = next((x for x in files if x.get("primary")), files[0] if files else None)
    if not f:
        return None
    h = f.get("hashes") or {}
    return {"version": v.get("version_number", ""), "type": v.get("version_type", "release"), "url": f["url"],
            "filename": f["filename"], "size": f.get("size", 0), "sha512": h.get("sha512", ""),
            "sha1": h.get("sha1", ""), "published": v.get("date_published", "")}


def pick_github(releases, loader, target, newest=False):
    """(файл, уверенно) — новейший релиз, где jar (или тег) явно под target. Пред-релиз — только если
    стабильного под target нет (или newest — сейчас стоит пред-релиз). Если версии игры не видно ни в имени, ни в теге — (файл, False):
    вероятно подходит, но проверь сам (по умолчанию не выбираем)."""
    sure_stable = sure_pre = unsure = None
    for rel in releases or []:
        if rel.get("draft"):
            continue
        tag = rel.get("tag_name", "") or ""
        tag_fit = versions.name_fits_game(tag, target)
        for a in rel.get("assets") or []:
            name = a.get("name", "")
            if not sources._asset_ok(name, loader):
                continue
            fit = versions.name_fits_game(name, target)
            if fit is False or (fit is None and tag_fit is False):
                continue
            sure = fit is True or (fit is None and tag_fit is True)
            item = (rel, a)
            if sure and (newest or not rel.get("prerelease")):
                sure_stable = sure_stable or item
            elif sure:
                sure_pre = sure_pre or item
            elif unsure is None and not rel.get("prerelease"):
                unsure = item
            break
        if sure_stable:
            break
    chosen, certain = (sure_stable, True) if sure_stable else (sure_pre, True) if sure_pre else (unsure, False)
    if not chosen:
        return None, False
    rel, a = chosen
    digest = a.get("digest") or ""
    return {"version": sources.asset_version(a["name"], rel.get("tag_name", "")),
            "type": "beta" if rel.get("prerelease") else "release", "url": a["browser_download_url"],
            "filename": a["name"], "size": a.get("size", 0),
            "sha256": digest.split(":", 1)[1] if digest.startswith("sha256:") else "",
            "published": rel.get("published_at", "")}, certain


def game_versions():
    """Версии игры с Modrinth (только релизы), новые первыми."""
    tags = sources.http_json(f"{sources.MODRINTH}/tag/game_version") or []
    rel = [t for t in tags if t.get("version_type") == "release"]
    rel.sort(key=lambda t: t.get("date", ""), reverse=True)
    return [t["version"] for t in rel]
