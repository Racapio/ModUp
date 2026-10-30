"""Версии модов и Minecraft: сравнение, узнавание версии игры в имени файла, диапазоны из fabric.mod.json."""
import re

_NUM = re.compile(r"\d+")


def vtuple(text):
    """«6.10.4» → (6, 10, 4). Нечисловые части игнорируются."""
    return tuple(int(x) for x in _NUM.findall(text or ""))


def core_version(v):
    """Версия самого мода без префикса v и без «+версия-игры»: «v6.11.0+26.2» → «6.11.0»."""
    v = (v or "").strip()
    v = re.sub(r"^[vV](?=\d)", "", v)
    return v.split("+")[0]


def compare(a, b):
    """-1 / 0 / 1 по числовым частям основной версии; «1.2» == «1.2.0»."""
    ta, tb = vtuple(core_version(a)), vtuple(core_version(b))
    n = max(len(ta), len(tb))
    ta += (0,) * (n - len(ta))
    tb += (0,) * (n - len(tb))
    return (ta > tb) - (ta < tb)


def looks_like_mc(t):
    """Похоже ли число на версию Minecraft: 1.14–1.21(.x) или новая схема 26.1, 26.1.2, 27.3…
    Отсекает версии самих модов вроде 6.11.0, 20.0.9, 26.1.819, 1.1.7.1."""
    if len(t) not in (2, 3):
        return False
    if t[0] == 1:
        return 14 <= t[1] <= 30 and (len(t) == 2 or t[2] <= 20)
    if 26 <= t[0] <= 40:
        return 1 <= t[1] <= 12 and (len(t) == 2 or t[2] <= 20)
    return False


# Конец числа — не цифра и не «.цифра» (иначе «26.1.jar» не узнавался бы: за версией идёт «.jar»).
_TOKEN = re.compile(r"(?<![\d.])(\d+\.\d+(?:\.\d+)?)(?:-(\d+\.\d+(?:\.\d+)?))?(?!\d|\.\d)")


def mc_tokens(name):
    """Версии игры, упомянутые в имени файла/теге: список (от, до) — одиночная версия даёт от == до."""
    out = []
    for m in _TOKEN.finditer(name or ""):
        a, b = vtuple(m.group(1)), vtuple(m.group(2)) if m.group(2) else None
        if b is not None and looks_like_mc(a) and looks_like_mc(b):
            out.append((a, b))
        elif looks_like_mc(a):
            out.append((a, a))
            if b is not None and looks_like_mc(b):
                out.append((b, b))
    return out


def _prefix_eq(token, game):
    return game[: len(token)] == token


def token_matches(tok, game):
    """Подходит ли упоминание версии игре: «26.1» подходит к 26.1.2 (по префиксу), диапазон — включительно."""
    lo, hi = tok
    if lo == hi:
        return _prefix_eq(lo, game)
    g = game + (0,) * (3 - len(game))
    lo3 = lo + (0,) * (3 - len(lo))
    hi3 = hi + (99,) * (3 - len(hi))
    return lo3 <= g <= hi3


def name_fits_game(name, game_version):
    """True — в имени есть подходящая версия игры; False — есть только чужие; None — версии игры в имени нет."""
    game = vtuple(game_version)
    toks = mc_tokens(name)
    if not toks:
        return None
    return any(token_matches(t, game) for t in toks)


# ── диапазоны из fabric.mod.json (depends.minecraft) ──

def _cmp_tuple(a, b):
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return (a > b) - (a < b)


def _one_constraint(c, game):
    c = c.strip()
    if not c or c == "*":
        return True
    m = re.match(r"^(>=|<=|>|<|=|~|\^)?\s*v?([\dxX*.]+)(?:[-+].*)?$", c)
    if not m:
        return True  # незнакомая запись — не придираемся
    op, ver = m.group(1) or "", m.group(2)
    parts = ver.split(".")
    if any(p in ("x", "X", "*") for p in parts):
        fixed = tuple(int(p) for p in parts if p.isdigit())
        return game[: len(fixed)] == fixed
    v = vtuple(ver)
    r = _cmp_tuple(game, v)
    if op == ">=":
        return r >= 0
    if op == "<=":
        return r <= 0
    if op == ">":
        return r > 0
    if op == "<":
        return r < 0
    if op == "~":  # ~26.1.2 → >=26.1.2 <26.2 ; ~26.1 → тот же minor
        return r >= 0 and game[:2] == v[:2]
    if op == "^":
        return r >= 0 and game[:1] == v[:1]
    # «26.2» без оператора — точное совпадение по указанной длине
    return game[: len(v)] == v


def mc_dependency_ok(dep, game_version):
    """depends.minecraft: строка («>=26.1 <26.2», «~26.2») или список строк (любая подходит)."""
    if dep is None:
        return True
    game = vtuple(game_version)
    if not game:
        return True
    alts = dep if isinstance(dep, list) else [dep]
    for alt in alts:
        if not isinstance(alt, str):
            return True
        if all(_one_constraint(c, game) for c in re.split(r"\s+", alt.strip()) if c):
            return True
    return False
