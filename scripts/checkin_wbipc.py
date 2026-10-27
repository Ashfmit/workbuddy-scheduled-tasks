# -*- coding: utf-8 -*-
"""Buddy 加油站每日签到 —— 走 WorkBuddy 端上 WBIPC 宿主通道，鉴权由宿主注入。

用法:
    python checkin_wbipc.py              # 查状态 -> 未签则领取 -> 复核
    python checkin_wbipc.py --dry-run    # 只查状态，不领取

退出码: 0 = 成功/今日已签/活动未开启   1 = 领取后复核失败   2 = 不在 WorkBuddy 环境

不读取、不打印、不落盘任何 token。
"""
import argparse
import base64
import json
import os
import sys

LIB_REL = os.path.join("resources", "app.asar.unpacked", "resources",
                       "plugins", "workbuddy-builtin", "skills", "library")


def find_library_dir():
    """定位 WorkBuddy 内置 skills/library（含 workbuddy_ipc），跨盘符/跨安装目录可用。"""
    cands = []

    env_dir = os.environ.get("WORKBUDDY_SKILL_LIB")
    if env_dir:
        cands.append(env_dir)

    roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")]
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        roots.append(os.path.join(local_appdata, "Programs"))
    roots += ["C:\\Program Files", "D:\\Program Files", "E:\\Program Files"]
    for r in roots:
        if r:
            cands.append(os.path.join(r, "WorkBuddy", LIB_REL))

    # 兜底：常见系统盘上的任意 Program Files* / 自定义安装目录
    import glob
    for drive in ("C:", "D:", "E:", "F:"):
        for pat in (drive + "\\Program Files*\\WorkBuddy", drive + "\\*\\WorkBuddy",
                    drive + "\\WorkBuddy"):
            for base in glob.glob(pat):
                cands.append(os.path.join(base, LIB_REL))

    seen = set()
    for c in cands:
        c = os.path.normpath(c)
        if c in seen:
            continue
        seen.add(c)
        if os.path.isfile(os.path.join(c, "workbuddy_ipc", "__init__.py")):
            return c
    return None


lib_dir = find_library_dir()
if not lib_dir:
    print("ERROR: 未找到 WorkBuddy 内置库目录（workbuddy_ipc）。")
    print("       请设置环境变量 WORKBUDDY_SKILL_LIB 指向 .../skills/library 后重试。")
    sys.exit(2)
sys.path.insert(0, lib_dir)

from workbuddy_ipc import connect, NotWorkBuddyEnv  # noqa: E402

STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
CLAIM_PATH = "/v2/billing/meter/daily-checkin"
STATUS_FIELDS = ("active", "activity_name", "start_time", "end_time", "today_checked_in",
                 "streak_days", "daily_credit", "streak_bonus_credit", "total_credits")


def fetch(pipe, method, path, body=None):
    req = {"method": method, "path": path,
           "headers": {"accept": "application/json", "content-type": "application/json"}}
    if body is not None:
        req["body_b64"] = base64.b64encode(json.dumps(body).encode()).decode()
    return pipe.invoke("http.fetch", req, timeout=35)


def body_of(resp):
    raw = resp.get("body_b64")
    if not raw:
        return {}
    try:
        return json.loads(base64.b64decode(raw))
    except Exception:
        return {}


def status(pipe):
    return body_of(fetch(pipe, "POST", STATUS_PATH, body={})).get("data", {}) or {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只查状态，不领取")
    args = ap.parse_args()

    try:
        client = connect(client_id="daily-checkin", client_kind="skill")
    except NotWorkBuddyEnv:
        print("ERROR: 未检测到 WorkBuddy 宿主通道（客户端未运行或 endpoint.json 不存在）。")
        sys.exit(2)

    with client as wb:
        pipe = wb.get_pipe("wb.request")
        st = status(pipe)
        print("STATUS:", json.dumps({k: st.get(k) for k in STATUS_FIELDS}, ensure_ascii=False))

        if not st.get("active"):
            print("ACTION: skip (活动未开启)")
            return 0
        if st.get("today_checked_in"):
            print("ACTION: skip (今日已签) | streak=%s | balance=%s"
                  % (st.get("streak_days"), st.get("total_credits")))
            return 0
        if args.dry_run:
            print("ACTION: dry-run (今日未签，未领取)")
            return 0

        resp = fetch(pipe, "POST", CLAIM_PATH, body={})
        print("CHECKIN:", resp.get("status"), json.dumps(body_of(resp), ensure_ascii=False)[:400])

        v = status(pipe)
        ok = v.get("today_checked_in") is True
        print("VERIFY: %s | streak=%s | balance=%s"
              % (ok, v.get("streak_days"), v.get("total_credits")))
        if not ok:
            print("ERROR: 领取后复核未通过。")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
