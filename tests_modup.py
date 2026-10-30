"""Офлайн-проверки ModUp: python tests_modup.py (код выхода 1 — ошибка)."""
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # консоль cp1251 из build.ps1
except AttributeError:
    pass
from modup import versions as v, sources as s

fails = 0
def check(name, ok, detail=""):
    global fails
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""))
    if not ok: fails += 1

# версия игры в имени файла
check("26.1 перед .jar узнаётся", v.mc_tokens("SkyHanni-9.2.0-mc26.1.jar") == [((26, 1), (26, 1))], v.mc_tokens("SkyHanni-9.2.0-mc26.1.jar"))
check("mc26.1 не подходит к 26.2", v.name_fits_game("SkyHanni-9.2.0-mc26.1.jar", "26.2") is False)
check("mc26.2 подходит к 26.2", v.name_fits_game("SkyHanni-9.2.0-mc26.2.jar", "26.2") is True)
check("+26.2 подходит, версия мода 6.11.0 не путается с игрой", v.name_fits_game("skyblocker-6.11.0+26.2.jar", "26.2") is True)
check("26.1 подходит к 26.1.2 (по префиксу)", v.name_fits_game("SkyHanni-9.2.0-mc26.1.jar", "26.1.2") is True)
check("26.1 не подходит к 26.10", v.name_fits_game("x-mc26.1.jar", "26.10") is False)
check("диапазон 1.20.3-1.20.4", v.name_fits_game("opsec-1.20.3-1.20.4+v1.1.7.1.jar", "1.20.4") is True and
      v.name_fits_game("opsec-1.20.3-1.20.4+v1.1.7.1.jar", "1.20.5") is False)
check("OpSec 1.21.11 не подходит к 26.2", v.name_fits_game("opsec-1.21.11+v1.1.7.1.jar", "26.2") is False)
check("версии модов не считаются версиями игры (20.0.9, 26.1.819, 0.155.3, 1.1.7.1)",
      all(not v.mc_tokens(n) for n in ["architectury-fabric-20.0.9.jar", "RoughlyEnoughItems-26.1.819.jar", "Odin-0.3.4.jar", "x-v1.1.7.1.jar"]))
check("без версии игры → None", v.name_fits_game("hm-api.jar", "26.2") is None)

# сравнение версий
check("6.11.0 > 6.10.4+26.2", v.compare("v6.11.0+26.2", "6.10.4+26.2") > 0)
check("1.2 == 1.2.0", v.compare("1.2", "1.2.0") == 0)
check("9.1.0 < 9.2.0", v.compare("9.1.0", "9.2.0") < 0)

# depends.minecraft
check("~26.2 подходит 26.2.1, не 26.3", v.mc_dependency_ok("~26.2", "26.2.1") and not v.mc_dependency_ok("~26.2", "26.3"))
check(">=26.1 <26.2", v.mc_dependency_ok(">=26.1 <26.2", "26.1.2") and not v.mc_dependency_ok(">=26.1 <26.2", "26.2"))
check("список — любая", v.mc_dependency_ok(["26.1.x", "26.2.x"], "26.2"))
check("26.2.x не подходит 26.1.2", not v.mc_dependency_ok("26.2.x", "26.1.2"))

# выбор файла из релиза GitHub
rels = [
    {"tag_name": "9.2.0", "prerelease": True, "assets": [
        {"name": "SkyHanni-9.2.0-mc26.1.jar", "browser_download_url": "u1", "size": 1},
        {"name": "SkyHanni-9.2.0-mc26.2.jar", "browser_download_url": "u2", "size": 1}]},
    {"tag_name": "9.1.0", "prerelease": True, "assets": [
        {"name": "SkyHanni-9.1.0-mc26.1.jar", "browser_download_url": "u3", "size": 1},
        {"name": "SkyHanni-9.1.0-mc26.2.jar", "browser_download_url": "u4", "size": 1}]},
    {"tag_name": "9.0.0", "prerelease": False, "assets": [
        {"name": "SkyHanni-9.0.0-mc26.2.jar", "browser_download_url": "u5", "size": 1}]},
]
u, why = s.pick_github_update("9.1.0", "SkyHanni-9.1.0-mc26.2.jar", rels, "fabric", "26.2", False)
check("SkyHanni 9.1.0 (пред-релиз) → 9.2.0 под mc26.2", u is not None and u["filename"] == "SkyHanni-9.2.0-mc26.2.jar", u and u["filename"])
u, why = s.pick_github_update("9.0.0", "SkyHanni-9.0.0-mc26.2.jar", rels, "fabric", "26.2", False)
check("с релиза 9.0.0 на пред-релизы без разрешения не прыгаем", u is None)
u, why = s.pick_github_update("9.0.0", "SkyHanni-9.0.0-mc26.2.jar", rels, "fabric", "26.3", True)
check("нет сборки под 26.3 — понятная причина", u is None and "26.3" in why, why)
ops = [{"tag_name": "V1.1.7.1", "prerelease": False, "assets": [
    {"name": "opsec-1.21.11+v1.1.7.1.jar", "browser_download_url": "a", "size": 1},
    {"name": "opsec-26.2+v1.1.7.1.jar", "browser_download_url": "b", "size": 1}]}]
u, why = s.pick_github_update("1.1.7.1", "opsec-26.2+v1.1.7.1.jar", ops, "fabric", "26.2", False)
check("OpSec той же версии — обновления нет", u is None and why == "", why)
check("версия OpSec из тега V1.1.7.1", s.asset_version("opsec-26.2+v1.1.7.1.jar", "V1.1.7.1") == "1.1.7.1")
check("forge-сборку не берём для fabric", not s._asset_ok("mod-1.0-forge.jar", "fabric") and s._asset_ok("mod-fabric-forge-compat.jar", "fabric"))
check("-sources.jar не берём", not s._asset_ok("mod-1.0-sources.jar", "fabric"))

# перенос на другую версию
from modup import migrate as mg
cands = [{"version_type": "beta", "date_published": "2026-10-20", "version_number": "2.0-beta", "files": [{"url": "b", "filename": "b.jar", "primary": True}]},
         {"version_type": "release", "date_published": "2026-10-01", "version_number": "1.9", "files": [{"url": "r", "filename": "r.jar", "primary": True}]}]
check("перенос: релиз важнее более свежей беты", mg.pick_modrinth(cands)["version_number"] == "1.9")
check("перенос: только бета — берём бету", mg.pick_modrinth(cands[:1])["version_number"] == "2.0-beta")
check("перенос: нет версий — None", mg.pick_modrinth([]) is None)
u, sure = mg.pick_github(rels, "fabric", "26.2")
check("перенос GitHub: стабильный 9.0.0 под mc26.2 важнее пред-релизов", sure and u["filename"] == "SkyHanni-9.0.0-mc26.2.jar", u and u["filename"])
u, sure = mg.pick_github(rels[:2], "fabric", "26.2")
check("перенос GitHub: нет стабильного — новейший пред-релиз", sure and u["filename"] == "SkyHanni-9.2.0-mc26.2.jar" and u["type"] == "beta", u and u["filename"])
u, sure = mg.pick_github(rels, "fabric", "26.2", newest=True)
check("перенос GitHub: стоит пред-релиз — новейший 9.2.0, не откат на 9.0.0", sure and u["filename"] == "SkyHanni-9.2.0-mc26.2.jar", u and u["filename"])
check("перенос: стоит бета — самая свежая", mg.pick_modrinth(cands, newest=True)["version_number"] == "2.0-beta")
u, sure = mg.pick_github(rels, "fabric", "26.3")
check("перенос GitHub: под 26.3 ничего", u is None)
noinfo = [{"tag_name": "v2.1", "prerelease": False, "assets": [{"name": "hm-api-2.1.jar", "browser_download_url": "h", "size": 1}]}]
u, sure = mg.pick_github(noinfo, "fabric", "26.2")
check("перенос GitHub: версия игры не указана — «проверь сам»", u is not None and not sure)
old = [{"tag_name": "v1.0+26.1", "prerelease": False, "assets": [{"name": "mod-1.0.jar", "browser_download_url": "o", "size": 1}]}]
u, sure = mg.pick_github(old, "fabric", "26.2")
check("перенос GitHub: тег под 26.1 — для 26.2 не берём", u is None)

# куда класть моды при переносе и перенос настроек (во временной папке)
import os, tempfile, json as _json
os.environ.setdefault("MODUP_HOME", tempfile.mkdtemp())
from modup import engine as en
with tempfile.TemporaryDirectory() as td:
    empty = os.path.join(td, "empty"); os.makedirs(empty)
    check("пустая папка → создаём в ней mods", en.resolve_mods_dir(empty) == os.path.join(empty, "mods"))
    inst = os.path.join(td, "inst"); os.makedirs(inst); open(os.path.join(inst, "instance.cfg"), "w").close()
    check("новый инстанс без .minecraft → .minecraft\\mods", en.resolve_mods_dir(inst) == os.path.join(inst, ".minecraft", "mods"))
    os.makedirs(os.path.join(inst, ".minecraft"))
    check("инстанс с .minecraft → .minecraft\\mods", en.resolve_mods_dir(inst) == os.path.join(inst, ".minecraft", "mods"))
    md = os.path.join(td, "x", "mods"); os.makedirs(md)
    check("выбрали саму mods — она и есть", en.resolve_mods_dir(md) == md)
    check("папка игры с mods → её mods", en.resolve_mods_dir(os.path.dirname(md)) == md)

    old = os.path.join(td, "old"); new = os.path.join(td, "new")
    for p in ("mods", "config/skyhanni/repo", "resourcepacks", "screenshots"):
        os.makedirs(os.path.join(old, p))
    os.makedirs(os.path.join(new, "mods"))
    for rel, body in (("options.txt", "fov:0.5"), ("servers.dat", "srv"), ("config/sodium.json", "{}"),
                      ("config/skyhanni/config.json", "{\"a\":1}"), ("config/skyhanni/repo/big.json", "кэш"),
                      ("resourcepacks/pack.zip", "zip"), ("screenshots/s.png", "png")):
        open(os.path.join(old, rel), "w", encoding="utf-8").write(body)
    open(os.path.join(new, "options.txt"), "w", encoding="utf-8").write("fov:0.0")
    e = en.Engine()
    info = {x["key"]: x for x in e.settings_info(os.path.join(old, "mods"))}
    check("перенос: видны настройки, серверы, config, паки, скриншоты",
          set(info) == {"options", "servers", "config", "resourcepacks", "screenshots"}, sorted(info))
    check("перенос: кэш repo в config не считается", info["config"]["files"] == 2, info["config"]["files"])
    bdir = os.path.join(td, "bk")
    done, failed, man = e._copy_settings(old, new, {"options", "servers", "config", "resourcepacks", "screenshots"}, bdir)
    check("перенос: options.txt заменён, старый — в бэкапе",
          open(os.path.join(new, "options.txt"), encoding="utf-8").read() == "fov:0.5" and os.path.isfile(os.path.join(bdir, "settings", "options.txt")))
    check("перенос: скриншоты и паки на месте, кэш не скопирован",
          os.path.isfile(os.path.join(new, "screenshots", "s.png")) and os.path.isfile(os.path.join(new, "resourcepacks", "pack.zip"))
          and not os.path.exists(os.path.join(new, "config", "skyhanni", "repo")))
    check("перенос: без ошибок", not failed, failed)
    # откат возвращает настройки как было
    rb = os.path.join(new, en.BACKUP_DIR_NAME, "2026-01-01_00-00-00"); os.makedirs(os.path.join(rb, "settings"))
    os.replace(os.path.join(bdir, "settings", "options.txt"), os.path.join(rb, "settings", "options.txt"))
    _json.dump({"at": "t", "kind": "migrate", "items": [], "added": [], "settings": man}, open(os.path.join(rb, "manifest.json"), "w", encoding="utf-8"))
    e.check = lambda only=None: None
    e.rollback(os.path.join(new, "mods"))
    check("откат: options.txt вернулся, скопированное убрано",
          open(os.path.join(new, "options.txt"), encoding="utf-8").read() == "fov:0.0" and not os.path.exists(os.path.join(new, "servers.dat")))

# мод не с Modrinth (другой отпечаток) — ищем проект по id
from modup import scan as sc
_real = s.modrinth_projects
s.modrinth_projects = lambda ids: {"A": {"id": "A", "slug": "sodium", "title": "Sodium", "loaders": ["fabric"], "project_type": "mod"},
                                   "B": {"id": "B", "slug": "essential", "title": "Essential Mod", "loaders": ["fabric"], "project_type": "mod"},
                                   "C": {"id": "C", "slug": "iq", "title": "Inventory Quantifier", "loaders": ["fabric"], "project_type": "mod"},
                                   "D": {"id": "D", "slug": "chatplus", "title": "ChatPlus", "loaders": ["fabric"], "project_type": "mod", "source_url": "https://github.com/CPTProgrammer/ChatPlus"}}
mods = [sc.ModFile(path="", sha512="", size=0, filename="sodium-fabric-0.9.2+mc26.1.2.jar", sha1="x1", mod_id="sodium", name="Sodium"),
        sc.ModFile(path="", sha512="", size=0, filename="Essential_1-5-1_fabric_26-1-2.jar", sha1="x2", mod_id="essential-container", name=""),
        sc.ModFile(path="", sha512="", size=0, filename="iq.jar", sha1="x3", mod_id="iq", name="Item Quality"),
        sc.ModFile(path="", sha512="", size=0, filename="chatplus-2.9.0.jar", sha1="x4", mod_id="chatplus", name="ChatPlus", github="ebicep/ChatPlus")]
found = en.Engine()._guess_modrinth(mods, {}, "fabric")
s.modrinth_projects = _real
check("по id: Sodium и Essential (essential-container) найдены", {"sodium-fabric-0.9.2+mc26.1.2.jar", "Essential_1-5-1_fabric_26-1-2.jar"} <= found, sorted(found))
check("по id: чужой проект с тем же коротким id не берём", "iq.jar" not in found)
check("по id: тот же id и название, но другой GitHub — не берём", "chatplus-2.9.0.jar" not in found)

# самообновление: сравнение версий
from modup import updater as up
check("обнова: v1.4.1 новее 1.4.0", up.is_newer("v1.4.1", "1.4.0"))
check("обнова: та же версия — не новее", not up.is_newer("v1.4.0", "1.4.0"))
check("обнова: 1.10.0 новее 1.9.9 (числа, не строки)", up.is_newer("1.10.0", "1.9.9"))
check("обнова: пустой тег — нет", not up.is_newer("", "1.4.0"))
check("обнова: из исходников exe нет — не подменяем", up.exe_path() is None)

print("\nВсе проверки пройдены." if not fails else f"\nПРОВАЛЕНО: {fails}")
sys.exit(1 if fails else 0)
