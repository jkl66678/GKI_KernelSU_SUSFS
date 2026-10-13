import os
import re
import json
import base64
import time
import http.client
import urllib.request
import urllib.error
import binascii
from datetime import datetime

REPO_URL = "https://android.googlesource.com/kernel/common"
BASE_URL = f"{REPO_URL}/+/refs/heads"
REFS_URL = f"{REPO_URL}/+refs"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
DATA_DIR = os.path.join(PROJECT_ROOT, "data")

# (android版本, 内核版本): (起始日期, 结束日期, deprecated截止日期)
TARGETS = {
    ("android12", "5.10"): ("2021-08", "2025-12", "2024-08"),
    ("android13", "5.15"): ("2022-06", "2025-12", "2024-09"),
    ("android14", "6.1"): ("2023-06", None, "2024-09"),
    ("android15", "6.6"): ("2024-10", None, ""),
    ("android16", "6.12"): ("2025-06", None, ""),
}

ERRORS = (
    urllib.error.HTTPError,
    urllib.error.URLError,
    TimeoutError,
    http.client.RemoteDisconnected,
    ConnectionResetError,
    OSError,
    binascii.Error,
)


def get_end_date(end: str | None) -> str:
    if end is not None:
        return end
    return datetime.now().strftime("%Y-%m")


def make_date_range(start: str, end: str) -> list[str]:
    """
    生成从 start 到 end 的日期列表。

    GKI 从 2025 年下半年起将 android14-6.1 等分支的发布节奏从月度改为季度，
    仅 3、6、9、12 月有对应分支。只生成季度月份，并保留起始月份
    （起始月份可能不是季度月，但它是该目标的首个有效分支）。
    """
    sy, sm = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    dates = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        if (y, m) == (sy, sm) or m in (3, 6, 9, 12):
            dates.append(f"{y}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return dates


def try_fetch(url: str) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            return base64.b64decode(resp.read()).decode("utf-8", errors="replace")
    except ERRORS:
        return None


def fetch_refs() -> tuple[set[str], set[str]] | None:
    """一次性获取上游全部分支名与 tag 名，失败返回 None"""
    result = []
    for kind in ("heads", "tags"):
        url = f"{REFS_URL}/{kind}?format=JSON"
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except ERRORS:
            return None
        if raw.startswith(")]}'"):
            raw = raw.split("\n", 1)[1]
        try:
            result.append(set(json.loads(raw).keys()))
        except ValueError:
            return None
    return result[0], result[1]


def resolve_ref(branch: str, heads: set[str], tags: set[str]) -> str | None:
    """
    按 build.yml 的规则解析分支在上游的实际位置：
      活跃分支 -> refs/heads/
      已迁移   -> refs/heads/deprecated/
      已删除   -> refs/tags/_rN（编号最大的发布 tag）
      都没有   -> None
    """
    if branch in heads:
        return f"refs/heads/{branch}"
    if f"deprecated/{branch}" in heads:
        return f"refs/heads/deprecated/{branch}"

    prefix = f"{branch}_r"
    best, best_n = None, -1
    for tag in tags:
        if tag.startswith(prefix) and tag[len(prefix):].isdigit():
            n = int(tag[len(prefix):])
            if n > best_n:
                best, best_n = tag, n
    return f"refs/tags/{best}" if best else None


def fetch_branch_makefile(branch: str, dep_cutoff: str) -> str | None:
    """
    无 refs 时的回退路径：先按 dep_cutoff 猜活跃 / deprecated 路径，
    都失败则查一次 tag 列表做最后回退。只在 fetch_refs() 失败时用到。
    """
    if dep_cutoff and branch.rsplit("-", 1)[-1] <= dep_cutoff:
        paths = [f"deprecated/{branch}", branch]
    else:
        paths = [branch, f"deprecated/{branch}"]

    for p in paths:
        text = try_fetch(f"{BASE_URL}/{p}/Makefile?format=TEXT")
        if text is not None:
            return text
        time.sleep(0.3)

    # 路径都失败：分支可能已被上游删除，只剩发布 tag
    refs = fetch_refs()
    if refs is None:
        return None
    ref = resolve_ref(branch, *refs)
    if ref is None:
        return None
    return try_fetch(f"{REPO_URL}/+/{ref}/Makefile?format=TEXT")


def fetch_makefile(
    android_ver: str,
    kernel_ver: str,
    date: str,
    dep_cutoff: str,
    refs: tuple[set[str], set[str]] | None = None,
) -> str | None:
    """获取日期分支的 Makefile。有 refs 时直接按上游实际位置取，否则走回退路径。"""
    branch = f"{android_ver}-{kernel_ver}-{date}"

    if refs is not None:
        ref = resolve_ref(branch, *refs)
        if ref is None:
            return None
        return try_fetch(f"{REPO_URL}/+/{ref}/Makefile?format=TEXT")

    return fetch_branch_makefile(branch, dep_cutoff)


def refresh_refs(
    entries: list[dict],
    android_ver: str,
    kernel_ver: str,
    refs: tuple[set[str], set[str]],
) -> int:
    """为每条记录写入 / 更新 ref 字段（上游实际路径），返回变更条数。"""
    heads, tags = refs
    changed = 0
    for entry in entries:
        branch = f"{android_ver}-{kernel_ver}-{entry['date']}"
        ref = resolve_ref(branch, heads, tags)
        if entry.get("ref") == ref:
            continue
        if ref is None:
            entry.pop("ref", None)
        else:
            entry["ref"] = ref
        changed += 1
    return changed


def fetch_lts(android_ver: str, kernel_ver: str) -> str | None:
    lts_branch = f"{android_ver}-{kernel_ver}-lts"
    return try_fetch(f"{BASE_URL}/{lts_branch}/Makefile?format=TEXT")


def parse_version(makefile_text: str) -> tuple[str, str, str] | None:
    vals = {}
    for key in ("VERSION", "PATCHLEVEL", "SUBLEVEL"):
        m = re.search(rf"^{key}\s*=\s*(\d+)", makefile_text, re.MULTILINE)
        if not m:
            return None
        vals[key] = m.group(1)
    return vals["VERSION"], vals["PATCHLEVEL"], vals["SUBLEVEL"]


def json_path(android_ver: str, kernel_ver: str) -> str:
    return os.path.join(DATA_DIR, android_ver, f"{kernel_ver}.json")
