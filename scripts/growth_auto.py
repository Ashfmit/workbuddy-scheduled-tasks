# -*- coding: utf-8 -*-
"""growth_auto.py — WorkBuddy 成长中心自动领取 + 兜底签到。

设计为被 WorkBuddy 定时任务（每天 07:30）以 `--now --once` 调用，一次跑完四件事；
也可以独立常驻运行（不带参数时每 30s 看表，07:30 窗口触发，同一天只跑一次）。

全部走「CDP 注入 → 页面内 window.wb.http」通道，不读登录凭据、不解密、不逆向、
零 AI 调用——页面里本来就持有登录态，注入的 JS 只是借它发请求：

  1. 兜底签到   POST /v2/billing/meter/checkin-activity-status
                未签则 POST /v2/billing/meter/daily-checkin（幂等）
  2. 断签补登   GET  /v2/activity/growth/streak（补登卡余额 + 已补记录）
                GET  /v2/activity/growth/heatmap（当月 score==0 断点）
                POST /v2/activity/growth/makeup-cards/use {target_date}
                每轮最多用 1 张；无卡 / 无断点 / 数据异常 → 跳过，绝不瞎补
  3. 连登兑换   GET  /v2/activity/growth/streak 的 redemption_status
                非 claimed/locked 的档位 → POST /v2/activity/growth/redeem
                {tier:"7d"|"14d"|"28d", client_token:"u-<uuid>"}
                14d=+50积分+3能量+1补登卡+1抽奖；28d=+150积分+5能量+1补登卡+1抽奖
  4. 盲盒抽奖   GET  /v2/activity/growth/lottery/chances
                有机会 → POST /v2/activity/growth/lottery/draw {client_token}
                每轮最多 1 次（写路径不可逆）；中实物奖只在日志里提醒填地址

接口契约均经 2026-10-27（WorkBuddy 5.9.2）实测核实：
  - 查询类一律 GET；写类一律 POST（POST 查询路由 404）
  - tier 必须传档位标识（"14d"），传天数或档位名会被服务端拒
  - draw/redeem 必须带 client_token，缺了直接 400

用法：
  python growth_auto.py --now --once   执行一轮后退出（定时任务用这个）
  python growth_auto.py --now          立即执行一轮（调试/手动补跑）
  python growth_auto.py                常驻：每 30s 看表，07:30 窗口触发，同一天只跑一次
"""

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp_client import WS, list_targets  # noqa: E402  最小依赖的 CDP 客户端

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "growth.log")
LOCK_PATH = os.path.join(HERE, ".growth_auto.pid")
STATE_PATH = os.path.join(HERE, "growth_state.json")

DEFAULT_PORT = 9222
RUN_HOUR, RUN_MINUTE = 7, 30            # 常驻模式的触发窗口 07:30–07:59
TICK_SECONDS = 30                        # 常驻轮询间隔
FIRST_RUN_DELAY = 60                     # 常驻启动后首次执行延迟（验证连通性）


# ---------------------------------------------------------------- 日志与进程设施

_STDOUT_IS_FILE = False


def ensure_streams() -> None:
    """pythonw.exe 没有控制台，sys.stdout/stderr 是 None，print 会直接崩进程。"""
    global _STDOUT_IS_FILE
    if sys.stdout is not None and sys.stderr is not None:
        return
    fh = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = fh
        _STDOUT_IS_FILE = True
    if sys.stderr is None:
        sys.stderr = fh


def log(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    if not _STDOUT_IS_FILE:
        try:
            if sys.stdout is not None:
                print(line, flush=True)
        except Exception:  # noqa: BLE001
            pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return str(pid) in (proc.stdout or "")
    except Exception:  # noqa: BLE001
        return False


def acquire_lock() -> bool:
    try:
        if os.path.exists(LOCK_PATH):
            pid = int((open(LOCK_PATH, encoding="utf-8").read() or "0").strip() or 0)
            if pid and _pid_alive(pid):
                log(f"已有常驻进程在跑（PID {pid}），本次退出。")
                return False
    except Exception:  # noqa: BLE001
        pass
    with open(LOCK_PATH, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
    return True


def release_lock() -> None:
    try:
        if os.path.exists(LOCK_PATH):
            pid = int((open(LOCK_PATH, encoding="utf-8").read() or "0").strip() or 0)
            if pid == os.getpid():
                os.remove(LOCK_PATH)
    except Exception:  # noqa: BLE001
        pass


def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=1)
    except OSError:
        pass


# ---------------------------------------------------------------- 页面内执行体

JS_GROWTH = r"""
(function(){
  if(!window.wb||!window.wb.http||!window.wb.http.post)
    return JSON.stringify({__noBridge:true});
  var R={signin:null, makeup:null, redeem:[], lottery:null, credits:0};
  function tok(){
    try{ if(window.crypto&&window.crypto.randomUUID) return 'u-'+window.crypto.randomUUID(); }catch(e){}
    return 'u-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,10);
  }
  function P(path, body, method){
    var p;
    try{ p=(method==='GET')?window.wb.http.get(path):window.wb.http.post(path, body||{}); }
    catch(e){ return Promise.resolve({__err:String(e)}); }
    return p.then(function(d){ return (d&&d.data!==undefined)?d.data:d; },
                  function(e){ return {__err:String((e&&e.message)||e)}; });
  }
  var chain=Promise.resolve();
  function step(fn){ chain=chain.then(fn); return chain; }

  /* 1. 兜底签到：已签则空转，未签才领（幂等） */
  step(function(){
    return P('/v2/billing/meter/checkin-activity-status',{},'POST').then(function(s){
      if(s&&s.__err){ R.signin={failed:'查询失败 '+s.__err}; return; }
      if(s&&s.today_checked_in){ R.signin={already:true,streak:s.streak_days}; return; }
      return P('/v2/billing/meter/daily-checkin',{},'POST').then(function(c){
        if(c&&!c.__err&&c.credit!=null){
          R.signin={claimed:true,credit:c.credit,streak:c.streak_days};
          R.credits+=Number(c.credit)||0;
        }else{
          R.signin={failed:String((c&&c.__err)||c)};
        }
      });
    });
  });

  /* 2+3 共用的 streak/heatmap 查询（一次拉取，补登和兑换复用） */
  var ST=null, HM=null;
  step(function(){
    return P('/v2/activity/growth/streak',null,'GET').then(function(st){
      if(st&&!st.__err&&st.streak) ST=st; else R.makeup={failed:'连登状态不可用'};
      return P('/v2/activity/growth/heatmap',null,'GET').then(function(hm){
        if(hm&&!hm.__err&&Array.isArray(hm.cells)) HM=hm;
      });
    });
  });

  /* 2. 断签补登：当月、服务端今天之前、score==0、未补过；最近断点优先；每轮最多 1 张 */
  step(function(){
    if(!ST||R.makeup){ return; }
    var cards=ST.makeup_cards;
    cards=(cards&&typeof cards==='object'&&cards.balance!=null)?cards.balance:cards;
    cards=Number(cards)||0;
    if(cards<=0){ R.makeup={skipped:'无补登卡',balance:0}; return; }
    if(!HM){ R.makeup={skipped:'日历不可用'}; return; }
    var today=(HM.today&&HM.today.date)||'';
    var month=String(today).slice(0,7);
    var madeUp=(ST.streak&&ST.streak.makeup_dates)||[];
    var cands=[];
    HM.cells.forEach(function(c){
      if(c&&typeof c.date==='string'&&c.date.slice(0,7)===month&&
         c.date<today&&c.score===0&&madeUp.indexOf(c.date)<0) cands.push(c.date);
    });
    cands.sort().reverse();
    if(!cands.length){ R.makeup={skipped:'当月无断点',balance:cards}; return; }
    return P('/v2/activity/growth/makeup-cards/use',{target_date:cands[0]},'POST').then(function(u){
      if(u&&!u.__err){
        var left=(u.makeup_cards&&u.makeup_cards.balance!=null)?u.makeup_cards.balance:(cards-1);
        R.makeup={used:cands[0],left:left,more:cands.length-1};
      }else{
        R.makeup={failed:cands[0],err:String((u&&u.__err)||u)};
      }
    });
  });

  /* 3. 连登兑换：补登会改连登天数，补过就重新拉 streak 再判断档位 */
  step(function(){
    var q=(R.makeup&&R.makeup.used)?P('/v2/activity/growth/streak',null,'GET'):Promise.resolve(ST);
    return q.then(function(st){
      if(!st||st.__err||!st.redemption_status){ R.redeem.push({skipped:'兑换状态不可用'}); return; }
      var rs=st.redemption_status, tiers=rs.tiers||[], jobs=[];
      tiers.forEach(function(t){
        var s=rs['tier_'+t.tier+'_status'];
        if(s==null) return;                      // 字段缺失不猜，接口改版时不对着空气 POST
        if(s==='claimed'||s==='locked') return;  // 已领 / 未解锁
        jobs.push(t.tier);
      });
      if(!jobs.length){
        R.redeem.push({skipped:'无可兑档位',
          t7:rs.tier_7d_status,t14:rs.tier_14d_status,t28:rs.tier_28d_status});
        return;
      }
      var jc=Promise.resolve();
      jobs.forEach(function(tier){
        jc=jc.then(function(){
          return P('/v2/activity/growth/redeem',{tier:tier,client_token:tok()},'POST').then(function(r){
            if(r&&!r.__err){
              var d={tier:tier,ok:true};
              ['credit','energy','cards','chances'].forEach(function(k){
                if(r[k+'_granted']!=null) d[k]=r[k+'_granted'];
              });
              R.redeem.push(d);
            }else{
              R.redeem.push({tier:tier,failed:String((r&&r.__err)||r)});
            }
          });
        });
      });
      return jc;
    });
  });

  /* 4. 盲盒：有机会才抽，每轮一次；实物奖只提醒不代填 */
  step(function(){
    return P('/v2/activity/growth/lottery/chances',null,'GET').then(function(c){
      var bal=(c&&!c.__err)?(Number(c.balance)||0):0;
      if(bal<=0){ R.lottery={skipped:'无抽奖机会'}; return; }
      return P('/v2/activity/growth/lottery/draw',{client_token:tok()},'POST').then(function(d){
        if(d&&!d.__err){
          R.lottery={prize:String(d.prize_name||d.prize||'未知'),
                     need_address:!!(d.need_address||d.require_address),left:bal-1};
        }else{
          R.lottery={failed:String((d&&d.__err)||d)};
        }
      });
    });
  });

  return chain.then(function(){ return JSON.stringify(R); });
})()
"""


def read_response(ws: WS, want_id: int, tries: int = 8) -> dict | None:
    for _ in range(tries):
        payload = ws.recv_frame()
        if payload is None:
            return None
        try:
            msg = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if msg.get("id") == want_id:
            return msg
    return None


def run_once(port: int) -> dict | None:
    """执行一轮成长中心。返回页面回传的 dict；连不上返回 None。"""
    pages = list_targets(port)
    if pages is None:
        return None
    for page in pages:
        title = str(page.get("title") or page.get("url", ""))[:40]
        ws = WS(page["webSocketDebuggerUrl"], timeout=45)
        try:
            ws.connect()
            ws.send_text(json.dumps({
                "id": 1,
                "method": "Runtime.evaluate",
                "params": {"expression": JS_GROWTH, "returnByValue": True, "awaitPromise": True},
            }))
            resp = read_response(ws, 1)
            if resp is None:
                log(f"页面 [{title}] 无响应")
                continue
            if resp.get("error"):
                log(f"页面 [{title}] CDP 错误：{resp['error']}")
                continue
            result = resp.get("result", {})
            if result.get("exceptionDetails"):
                log(f"页面 [{title}] JS 异常：{result['exceptionDetails']}")
                continue
            raw = result.get("result", {}).get("value")
            if not isinstance(raw, str):
                continue
            data = json.loads(raw)
            if data.get("__noBridge"):
                continue
            return data
        except Exception as exc:  # noqa: BLE001
            log(f"页面 [{title}] 异常：{exc}")
        finally:
            ws.close()
    return None


def summarize(r: dict) -> str:
    """把一轮结果压成一行中文摘要。"""
    parts: list[str] = []
    s = r.get("signin") or {}
    if s.get("already"):
        parts.append(f"签到：已签（连登 {s.get('streak')}）")
    elif s.get("claimed"):
        parts.append(f"签到：补领 +{s.get('credit')}")
    elif s.get("failed"):
        parts.append(f"签到失败：{s['failed']}")
    m = r.get("makeup") or {}
    if m.get("used"):
        parts.append(f"补登 {m['used']}（剩 {m.get('left')} 张卡"
                     + (f"，另有 {m['more']} 天待补" if m.get("more") else "") + "）")
    elif m.get("skipped"):
        parts.append(f"补登：{m['skipped']}")
    elif m.get("failed"):
        parts.append(f"补登失败：{m.get('err') or m.get('failed')}")
    redeem = r.get("redeem") or []
    for item in redeem:
        if item.get("ok"):
            bits = [f"{k}+{item[k]}" for k in ("credit", "energy", "cards", "chances") if k in item]
            parts.append(f"兑换 {item['tier']}：{' '.join(bits) or '成功'}")
        elif item.get("skipped"):
            parts.append(f"兑换：{item['skipped']}"
                         + (f"（7d:{item.get('t7')} 14d:{item.get('t14')} 28d:{item.get('t28')}）"
                            if item.get("t7") is not None else ""))
        elif item.get("failed"):
            parts.append(f"兑换 {item.get('tier')} 失败：{item['failed']}")
    lo = r.get("lottery") or {}
    if lo.get("prize"):
        parts.append(f"盲盒：{lo['prize']}" + ("（实物奖，待填收件信息）" if lo.get("need_address") else ""))
    elif lo.get("skipped"):
        parts.append(f"盲盒：{lo['skipped']}")
    elif lo.get("failed"):
        parts.append(f"盲盒失败：{lo['failed']}")
    return "；".join(parts) if parts else "无动作"


def execute(port: int, force: bool = False) -> bool:
    """跑一轮并落状态。返回是否真的执行了。"""
    today = datetime.now().strftime("%Y-%m-%d")
    state = load_state()
    if not force and state.get("last_run_date") == today:
        return False
    result = run_once(port)
    if result is None:
        # 一帧超时（页面串行发 6-8 个请求，网络差时 45s 等不到回帧）会让整轮丢掉；
        # 所有业务操作幂等，重试一次不会重复到账，值得救。
        log("首轮失败（端口未开或页面无响应），5 秒后重试一次")
        time.sleep(5)
        result = run_once(port)
    if result is None:
        log("客户端未连接（端口未开或页面无桥），本轮跳过")
        return False
    log("成长中心一轮完成 → " + summarize(result))
    if result.get("credits"):
        log(f"  本轮积分 +{result['credits']}")
    state["last_run_date"] = today
    state["last_result"] = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "summary": summarize(result),
    }
    save_state(state)
    return True


def in_window(now: datetime) -> bool:
    return (now.hour, now.minute) >= (RUN_HOUR, RUN_MINUTE) and \
           (now.hour, now.minute) < (RUN_HOUR, RUN_MINUTE + 30)


def main() -> int:
    ensure_streams()
    ap = argparse.ArgumentParser(description="WorkBuddy 成长中心自动领取 + 兜底签到")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"调试端口，默认 {DEFAULT_PORT}")
    ap.add_argument("--now", action="store_true", help="立即执行一轮（忽略日期去重）")
    ap.add_argument("--once", action="store_true", help="执行一轮后退出")
    args = ap.parse_args()

    if args.now or args.once:
        # 一次性模式也要防并发：定时任务和手动补跑撞车时，两个 CDP 会话会
        # 并发打同一批接口，同档位兑换可能双 POST（服务端是否重复发奖未实测）。
        if not acquire_lock():
            return 0
        try:
            execute(args.port, force=args.now)
        finally:
            release_lock()
        return 0

    if not acquire_lock():
        return 0
    log(f"常驻启动：每 {TICK_SECONDS}s 看表，{RUN_HOUR:02d}:{RUN_MINUTE:02d} 窗口触发，端口 {args.port}")
    started_at = time.time()
    first_pending = True
    try:
        while True:
            now = datetime.now()
            if first_pending and time.time() - started_at >= FIRST_RUN_DELAY:
                # 首轮不看时间窗：装上的当天就能在日志里验证整条链路
                execute(args.port)
                first_pending = False
            elif in_window(now):
                execute(args.port)
            time.sleep(TICK_SECONDS)
    except KeyboardInterrupt:
        log("收到中断，退出")
    finally:
        release_lock()
    return 0


if __name__ == "__main__":
    sys.exit(main())
