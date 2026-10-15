#!/usr/bin/env python3
"""face-ctl —— 一个口子管所有日常操作。"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
import facelib
from common import log

SERVICE = "face-unlockd.service"
LOCK_SERVICE = "face-lock-at-boot.service"
UNIT_DIR = os.path.join(os.path.expanduser("~/.config/systemd/user"))


def sh(*cmd: str, env: dict | None = None) -> int:
    return subprocess.call(list(cmd), env=env or dict(os.environ))


def systemctl(*args: str) -> int:
    return sh("systemctl", "--user", *args)


# ---------- status ----------

def cmd_status(_args) -> int:
    cfg = common.load_config()
    print(f"配置: {common.config_path()}")
    print(f"数据: {common.data_dir()}")
    print()

    print("依赖体检")
    all_ok = True
    for name, ok, detail in common.check_deps(cfg):
        mark = " OK " if ok else "FAIL"
        all_ok &= ok
        print(f"  [{mark}] {name:16s} {detail}")
    print()

    paused = common.is_paused()
    until = common.pause_until()
    state = "未知"
    out = subprocess.run(["systemctl", "--user", "is-active", SERVICE],
                         capture_output=True, text=True)
    if out.returncode in (0, 1, 3):
        state = out.stdout.strip() or "unknown"
    print("运行状态")
    print(f"  守护进程   : {state}")
    print(f"  扫描暂停   : {'是' + (f'（{int(until - time.time())}s 后自动恢复）' if until and until != float('inf') else '（永久，face-ctl resume 恢复）') if paused else '否'}")
    try:
        print(f"  屏幕       : {'已锁' if common.is_screen_locked() else '未锁'}")
    except Exception as exc:
        print(f"  屏幕       : 查询失败 {exc}")
    print(f"  当前阈值   : {cfg['THRESHOLD']}  连续帧 {cfg['STREAK']}  防静态图 {cfg['ANTI_SPOOF']}")

    if not all_ok or state != "active":
        print("\n有未就绪项，按上面 FAIL 的提示处理后运行 face-ctl doctor 复查")
        return 1
    return 0


def cmd_doctor(args) -> int:
    return cmd_status(args)


# ---------- 密码 ----------

def cmd_set_password(_args) -> int:
    import getpass
    p1 = getpass.getpass("登录密码（输入时不显示）: ")
    if not p1:
        log("空密码，取消")
        return 1
    p2 = getpass.getpass("再输一遍确认: ")
    if p1 != p2:
        log("两次不一致，未写入")
        return 1
    common.write_secret(p1)
    log(f"已写入 {common.secret_path()}，权限 0600")
    print()
    print("请把风险看一遍：这个文件里是你的明文登录密码。")
    print("注意：这个文件里是明文密码。若本机未启用全盘加密，任何能物理接触机器、")
    print("能插 U 盘启动的人都能读走它 —— 和读走你 home 目录下其它文件一样容易。")
    print("要删掉它：face-ctl off && rm " + common.secret_path())
    return 0


# ---------- 服务 ----------

def cmd_service_start(_args) -> int:
    systemctl("daemon-reload")
    rc = systemctl("restart", SERVICE)
    time.sleep(1.0)
    return cmd_status(_args)


def cmd_on(_args) -> int:
    _set_cfg("ENABLED", "1")
    if os.path.exists(common.disable_flag()):
        os.unlink(common.disable_flag())
    log("已启用")
    return 0


def cmd_off(_args) -> int:
    _set_cfg("ENABLED", "0")
    log("已停用（守护进程不会再开摄像头；密码文件仍在，需要的话自行删除）")
    return 0


def cmd_pause(args) -> int:
    secs = getattr(args, "seconds", None)
    p = common.disable_flag()
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("" if secs is None else str(time.time() + secs))
    log(f"已暂停扫描" + ("" if secs is None else f"，{secs}s 后自动恢复"))
    return 0


def cmd_resume(_args) -> int:
    p = common.disable_flag()
    if os.path.exists(p):
        os.unlink(p)
        log("已恢复扫描")
    else:
        log("本来就没暂停")
    return 0


def cmd_restart(args) -> int:
    cmd_service_start(args)
    return 0


def cmd_stop(_args) -> int:
    return systemctl("stop", SERVICE)


def cmd_lock(_args) -> int:
    common.lock_screen()
    log("已锁屏，守护进程应在此后 1~3 秒内开始扫描；journalctl 能看到过程")
    return 0


def cmd_logs(args) -> int:
    n = getattr(args, "lines", 60)
    return sh("journalctl", "--user", "-u", SERVICE, "-n", str(n), "--no-pager")


def cmd_install_service(args) -> int:
    """从 .in 模板渲染单元文件。install.sh 做的是同一件事，这个入口留着方便
    改完模板后单独重装服务，而不必跑完整安装。"""
    pkg = _pkg_dir()
    tpl_dir = os.path.join(pkg, "systemd")
    os.makedirs(UNIT_DIR, exist_ok=True)
    for unit in (SERVICE, LOCK_SERVICE):
        tpl = os.path.join(tpl_dir, unit + ".in")
        if not os.path.exists(tpl):
            log(f"缺少单元模板 {tpl}")
            return 1
        with open(tpl, encoding="utf-8") as fh:
            body = fh.read().replace("__INSTALL_DIR__", pkg)
        if "__INSTALL_DIR__" in body:
            log("模板里还有未替换的占位符，检查 .in 文件")
            return 1
        dest = os.path.join(UNIT_DIR, unit)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(body)
        log(f"写入 {dest}")
    systemctl("daemon-reload")
    systemctl("enable", SERVICE, LOCK_SERVICE)
    return cmd_service_start(args)


def cmd_uninstall_service(_args) -> int:
    systemctl("stop", SERVICE, LOCK_SERVICE)
    systemctl("disable", SERVICE, LOCK_SERVICE)
    for unit in (SERVICE, LOCK_SERVICE):
        p = os.path.join(UNIT_DIR, unit)
        if os.path.exists(p):
            os.unlink(p)
            log(f"删除 {p}")
    systemctl("daemon-reload")
    return 0


# ---------- 调阈值 ----------

def cmd_tune(_args) -> int:
    cfg = common.load_config()
    thr = float(cfg["THRESHOLD"])
    print("这一步要两个人：你本人，加上一个不是你本人的人（或拿你的照片/手机视频试）。")
    print("只做本人测试的话，你只能知道「会不会拒自己」，不知道「会不会放别人」——")
    print("而后一个才是这套东西的真实风险。\n")
    print("== 第一步：本人（正常坐好） ==")
    subprocess.call([sys.executable, os.path.join(os.path.dirname(__file__),
                  "face_unlockd.py"), "--test"])
    print("\n== 第二步：本人（戴帽子 / 侧一点 / 关灯，任选你会遇到的情况） ==")
    input("准备好了按回车继续… ")
    subprocess.call([sys.executable, os.path.join(os.path.dirname(__file__),
                  "face_unlockd.py"), "--test"])
    print("\n== 第三步：别人，或你的手机里放一张你的照片对准镜头 ==")
    input("换好目标按回车继续… ")
    subprocess.call([sys.executable, os.path.join(os.path.dirname(__file__),
                  "face_unlockd.py"), "--test"])
    print(f"\n当前阈值 {thr:.3f}。")
    print("规则：取「本人最差那次」和「别人最高那次」的正中间。")
    print("如果两者重叠（别人最高分 >= 本人最低分），这台摄像头配 2D 识别就")
    print("不足以做门禁，只能提高到只挡陌生人，或者放弃免密。")
    return 0


def _set_cfg(key: str, value: str) -> None:
    path = common.config_path()
    if not os.path.exists(path):
        common.write_default_config()
    lines, hit = [], False
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip().startswith(key + "="):
                lines.append(f"{key}={value}\n")
                hit = True
            else:
                lines.append(line)
    if not hit:
        lines.append(f"{key}={value}\n")
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    os.chmod(path, 0o600)


def _pkg_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    ap = argparse.ArgumentParser(prog="face-ctl", description="人脸无感解锁控制入口")
    sp = ap.add_subparsers(dest="cmd")

    sp.add_parser("status").set_defaults(func=cmd_status)
    sp.add_parser("doctor").set_defaults(func=cmd_doctor)
    sp.add_parser("test").set_defaults(
        func=lambda a: subprocess.call(
            [sys.executable, os.path.join(os.path.dirname(__file__), "face_unlockd.py"), "--test"]))
    sp.add_parser("set-password").set_defaults(func=cmd_set_password)
    sp.add_parser("install-service").set_defaults(func=cmd_install_service)
    sp.add_parser("uninstall-service").set_defaults(func=cmd_uninstall_service)
    sp.add_parser("on").set_defaults(func=cmd_on)
    sp.add_parser("off").set_defaults(func=cmd_off)
    p = sp.add_parser("pause"); p.add_argument("seconds", nargs="?", type=int, default=None)
    p.set_defaults(func=cmd_pause)
    sp.add_parser("resume").set_defaults(func=cmd_resume)
    sp.add_parser("restart").set_defaults(func=cmd_restart)
    sp.add_parser("stop").set_defaults(func=cmd_stop)
    sp.add_parser("lock").set_defaults(func=cmd_lock)
    lg = sp.add_parser("logs"); lg.add_argument("--lines", type=int, default=60)
    lg.set_defaults(func=cmd_logs)
    sp.add_parser("tune").set_defaults(func=cmd_tune)

    args = ap.parse_args()
    if not getattr(args, "cmd", None):
        ap.print_help()
        return 0
    return args.func(args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
