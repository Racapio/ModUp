"""Поиск инстансов и чтение папки модов: версия игры, загрузчик, сведения о каждом jar."""
import hashlib
import json
import os
import re
import string
import zipfile
from dataclasses import dataclass, field

from . import versions

LOADER_UIDS = {
    "net.fabricmc.fabric-loader": "fabric",
    "org.quiltmc.quilt-loader": "quilt",
    "net.neoforged": "neoforge",
    "net.minecraftforge": "forge",
}


@dataclass
class ModFile:
    path: str
    filename: str
    sha1: str
    sha512: str
    size: int
    mod_id: str = ""
    name: str = ""
    version: str = ""
    github: str = ""          # owner/repo из fabric.mod.json (contact.sources/homepage/issues)
    modrinth_slug: str = ""   # если в моде ссылка на страницу Modrinth (modrinth.com/mod/<slug>)
    mc_dep: object = None     # depends.minecraft
    icon_bytes: bytes = b""   # иконка из jar (для модов не с Modrinth)


@dataclass
class Folder:
    path: str                 # папка mods
    name: str
    launcher: str             # prism / multimc / folder
    game_version: str = ""
    loader: str = ""
    loader_version: str = ""
    mods: list = field(default_factory=list)
    disabled: list = field(default_factory=list)


_GH = re.compile(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
_MR = re.compile(r"modrinth\.com/(?:mod|project|plugin)/([A-Za-z0-9_.-]+)")


def github_repo(url):
    m = _GH.search(url or "")
    if not m:
        return ""
    owner, repo = m.group(1), m.group(2)
    repo = re.sub(r"\.git$", "", repo)
    return f"{owner}/{repo}"


def _read_mod_json(z):
    for name in ("fabric.mod.json", "quilt.mod.json"):
        try:
            raw = z.read(name).decode("utf-8", "replace")
        except KeyError:
            continue
        try:
            data = json.loads(raw, strict=False)
        except ValueError:
            continue
        if name == "quilt.mod.json":
            q = data.get("quilt_loader", {})
            meta = q.get("metadata", {})
            return {"id": q.get("id", ""), "name": meta.get("name", ""), "version": q.get("version", ""),
                    "contact": meta.get("contact", {}), "depends": {}, "icon": meta.get("icon", "")}
        return data
    return None


def read_jar(path):
    with open(path, "rb") as f:
        data = f.read()
    mf = ModFile(path=path, filename=os.path.basename(path), sha1=hashlib.sha1(data).hexdigest(),
                 sha512=hashlib.sha512(data).hexdigest(), size=len(data))
    try:
        with zipfile.ZipFile(path) as z:
            meta = _read_mod_json(z)
            if meta:
                mf.mod_id = str(meta.get("id", "") or "")
                mf.name = str(meta.get("name", "") or "") or mf.mod_id
                mf.version = str(meta.get("version", "") or "")
                contact = meta.get("contact") or {}
                for key in ("sources", "homepage", "issues"):
                    repo = github_repo(str(contact.get(key, "") or ""))
                    if repo:
                        mf.github = repo
                        break
                for v in contact.values():
                    m = _MR.search(str(v or ""))
                    if m:
                        mf.modrinth_slug = m.group(1)
                        break
                deps = meta.get("depends") or {}
                if isinstance(deps, dict):
                    mf.mc_dep = deps.get("minecraft")
                icon = meta.get("icon")
                if isinstance(icon, dict):
                    icon = icon.get("64") or icon.get("128") or next(iter(icon.values()), "")
                if isinstance(icon, str) and icon:
                    try:
                        b = z.read(icon.lstrip("/"))
                        if len(b) < 200_000:
                            mf.icon_bytes = b
                    except KeyError:
                        pass
    except (zipfile.BadZipFile, OSError):
        pass
    if not mf.name:
        mf.name = re.sub(r"\.jar$", "", mf.filename)
    return mf


def loader_version_of(inst_dir):
    """Версия загрузчика (Fabric Loader и т.п.) из mmc-pack.json — нужна для экспорта сборки."""
    try:
        pack = json.load(open(os.path.join(inst_dir, "mmc-pack.json"), encoding="utf-8"))
        for c in pack.get("components", []):
            if c.get("uid", "") in LOADER_UIDS:
                return c.get("version", "")
    except (OSError, ValueError):
        pass
    return ""


def _instance_info(inst_dir):
    """mmc-pack.json (Prism/MultiMC): версия игры и загрузчик; instance.cfg: имя."""
    game, loader, name = "", "", os.path.basename(inst_dir)
    try:
        pack = json.load(open(os.path.join(inst_dir, "mmc-pack.json"), encoding="utf-8"))
        for c in pack.get("components", []):
            uid = c.get("uid", "")
            if uid == "net.minecraft":
                game = c.get("version", "")
            elif uid in LOADER_UIDS:
                loader = LOADER_UIDS[uid]
    except (OSError, ValueError):
        pass
    try:
        for line in open(os.path.join(inst_dir, "instance.cfg"), encoding="utf-8", errors="replace"):
            if line.startswith("name="):
                name = line.split("=", 1)[1].strip() or name
                break
    except OSError:
        pass
    return game, loader, name


def mods_dir_of(inst_dir):
    for sub in (".minecraft", "minecraft"):
        p = os.path.join(inst_dir, sub, "mods")
        if os.path.isdir(p):
            return p
    return ""


def describe_folder(mods_path, launcher_hint=""):
    """Папка mods → Folder без чтения модов. Если это инстанс Prism/MultiMC — версия из mmc-pack.json."""
    mods_path = os.path.normpath(mods_path)
    parent = os.path.dirname(mods_path)
    inst_dir = os.path.dirname(parent) if os.path.basename(parent).lower() in (".minecraft", "minecraft") else parent
    game, loader, name = "", "", os.path.basename(parent) or mods_path
    launcher = launcher_hint or "folder"
    lver = ""
    if os.path.isfile(os.path.join(inst_dir, "mmc-pack.json")):
        game, loader, name = _instance_info(inst_dir)
        lver = loader_version_of(inst_dir)
        if not launcher_hint:
            launcher = "instance"
    return Folder(path=mods_path, name=name, launcher=launcher, game_version=game, loader=loader, loader_version=lver)


def guess_game_version(folder):
    """Для простой папки: версия игры по Fabric API (его depends/имя файла) или по большинству модов."""
    votes = {}
    for m in folder.mods:
        toks = versions.mc_tokens(m.filename)
        if m.mod_id == "fabric-api" and toks:
            lo = toks[-1][0]
            return ".".join(map(str, lo))
        for lo, hi in toks:
            if lo == hi:
                k = ".".join(map(str, lo))
                votes[k] = votes.get(k, 0) + 1
    return max(votes, key=votes.get) if votes else ""


def load_mods(folder):
    folder.mods, folder.disabled = [], []
    try:
        names = sorted(os.listdir(folder.path), key=str.lower)
    except OSError:
        return folder
    for f in names:
        p = os.path.join(folder.path, f)
        if not os.path.isfile(p):
            continue
        if f.lower().endswith(".jar"):
            folder.mods.append(read_jar(p))
        elif f.lower().endswith(".jar.disabled"):
            folder.disabled.append(f)
    if not folder.loader:
        folder.loader = "fabric" if any(m.mod_id for m in folder.mods) else ""
    if not folder.game_version:
        folder.game_version = guess_game_version(folder)
    return folder


# ── поиск лаунчеров ──

def _launcher_roots():
    roots = []
    appdata = os.environ.get("APPDATA", "")
    for sub, kind in (("PrismLauncher", "prism"), ("MultiMC", "multimc"), ("PolyMC", "prism")):
        p = os.path.join(appdata, sub)
        if os.path.isdir(p):
            roots.append((p, kind))
    # Портативные: в корне дисков и на один уровень ниже ищем prismlauncher.cfg / multimc.cfg.
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        if not os.path.isdir(drive):
            continue
        try:
            top = [os.path.join(drive, d) for d in os.listdir(drive)]
        except OSError:
            continue
        for d in top:
            if not os.path.isdir(d):
                continue
            # Портативный — с файлом настроек; установленный в свою папку — узнаём по exe
            # (настройки тогда в %APPDATA%, а инстансы — обычно рядом, в instances).
            for marker, kind in (("prismlauncher.cfg", "prism"), ("multimc.cfg", "multimc"),
                                 ("prismlauncher.exe", "prism"), ("MultiMC.exe", "multimc")):
                if os.path.isfile(os.path.join(d, marker)):
                    roots.append((d, kind))
                    break
    seen, out = set(), []
    for p, k in roots:
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            out.append((p, k))
    return out


def _instances_dir(root):
    for cfg in ("prismlauncher.cfg", "multimc.cfg", "polymc.cfg"):
        try:
            for line in open(os.path.join(root, cfg), encoding="utf-8", errors="replace"):
                if line.startswith("InstanceDir="):
                    v = line.split("=", 1)[1].strip()
                    return v if os.path.isabs(v) else os.path.join(root, v)
        except OSError:
            pass
    return os.path.join(root, "instances")


def discover():
    """Все инстансы Prism/MultiMC на ПК: список Folder (без чтения модов)."""
    out, seen = [], set()
    for root, kind in _launcher_roots():
        idir = _instances_dir(root)
        try:
            entries = sorted(os.listdir(idir), key=str.lower)
        except OSError:
            continue
        for e in entries:
            inst = os.path.join(idir, e)
            if not os.path.isdir(inst) or e.startswith((".", "_")):
                continue
            md = mods_dir_of(inst)
            if not md:
                continue
            key = os.path.normcase(os.path.abspath(md))
            if key in seen:      # тот же Prism найден и по настройкам в %APPDATA%, и по exe
                continue
            seen.add(key)
            game, loader, name = _instance_info(inst)
            out.append(Folder(path=os.path.normpath(md), name=name, launcher=kind, game_version=game, loader=loader))
    return out
