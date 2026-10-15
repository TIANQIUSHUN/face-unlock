#!/usr/bin/env python3
"""以 root 运行、被 pkexec 调起的唯一提权入口。

它只做一件事：改 /etc/gdm3/custom.conf 里的 AutomaticLoginEnable / AutomaticLogin，
不接受其它指令，也不碰 WaylandEnable —— 「仅支持 Xorg」是这一版的边界，
把全局会话类型强行改掉会牵连别的用户和别的应用，那是另一件事，得由人明确决定。

安全要点：
  * 受益用户不采信命令行传进来的名字，而是取 pkexec 自己记录的 PKEXEC_UID。
    否则任何本地进程都能拿这个助手给别的账号开自动登录。
  * 只允许改这两行，其余内容逐行原样保留；原子替换并保留原权限与属主。
  * 首次修改前留一份备份，方便回退。
"""

from __future__ import annotations

import os
import pwd
import re
import shutil
import subprocess
import sys

CONF = "/etc/gdm3/custom.conf"
BACKUP = CONF + ".faceunlock-backup"
ALLOWED = ("on", "off")


def die(msg: str, code: int = 2) -> "None":
    print(f"错误：{msg}", file=sys.stderr)
    raise SystemExit(code)


def caller_user() -> str:
    """pkexec 会把发起者的 uid 写进环境，这是唯一可信来源。"""
    uid = os.environ.get("PKEXEC_UID")
    if not uid:
        die("没有 PKEXEC_UID —— 本脚本必须由 pkexec 调起，不能直接 sudo 跑")
    if int(os.geteuid()) != 0:
        die("当前不是 root，pkexec 提权没有生效")
    try:
        return pwd.getpwuid(int(uid)).pw_name
    except KeyError:
        die(f"PKEXEC_UID={uid} 对应的用户不存在")


def edit(text: str, enable: bool, user: str) -> tuple[str, list[str]]:
    """返回 (新内容, 变更说明)。"""
    lines = text.splitlines()
    changes: list[str] = []

    # 必须有个 [daemon] 段；GDM 的自动登录只认这一段
    if not any(l.strip().lower() == "[daemon]" for l in lines):
        lines.insert(0, "[daemon]")
        lines.insert(1, "")
        changes.append("新建了 [daemon] 段")

    sec = None
    for i, l in enumerate(lines):
        if l.strip().lower() == "[daemon]":
            sec = i
            break
    assert sec is not None
    end = len(lines)
    for j in range(sec + 1, len(lines)):
        if lines[j].strip().startswith("["):
            end = j
            break

    def set_in_daemon(key: str, value: str) -> None:
        nonlocal lines, changes, end
        for k in range(sec + 1, end):
            if re.match(rf"^\s*{key}\s*=", lines[k], re.I):
                old = lines[k]
                lines[k] = f"{key}={value}"
                if old.strip() != lines[k]:
                    changes.append(f"{old.strip()} -> {key}={value}")
                return
        lines.insert(sec + 1, f"{key}={value}")
        # 插入让后面的行号整体后移一位，end 必须跟着走，
        # 否则下一个键落在原 [daemon] 最后一行时会被漏掉，写出重复行
        end += 1
        changes.append("在 [daemon] 里新增 " + key)

    set_in_daemon("AutomaticLoginEnable", "true" if enable else "false")
    if enable:
        set_in_daemon("AutomaticLogin", user)
    else:
        # 关掉时把 AutomaticLogin 一并清掉，避免留下一个"看着像开着"的残留行
        for k in range(sec + 1, end):
            if re.match(r"^\s*AutomaticLogin\s*=", lines[k], re.I):
                changes.append(f"注释掉 {lines[k].strip()}")
                lines[k] = "# " + lines[k].lstrip()

    return "\n".join(lines) + "\n", changes


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in ALLOWED:
        die("用法：set_autologin.py on|off   （请通过 pkexec <polkit-action> 调用）")
    enable = argv[0] == "on"
    user = caller_user()

    if not os.path.exists(CONF):
        die(f"{CONF} 不存在；这台机器可能不是 GDM，无法设置自动登录")
    before = open(CONF, encoding="utf-8", errors="replace").read()
    after, changes = edit(before, enable, user)

    if not changes:
        print(f"已经是目标状态（自动登录 {'开启' if enable else '关闭'}），未做任何修改")
        return 0

    if not os.path.exists(BACKUP):
        shutil.copy2(CONF, BACKUP)
        os.chmod(BACKUP, 0o600)
        print(f"首次修改，原文件已备份到 {BACKUP}（0600，需 sudo 才能读）")

    st = os.stat(CONF)
    tmp = CONF + ".faceunlock.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(after)
    os.chmod(tmp, st.st_mode & 0o777)
    os.chown(tmp, st.st_uid, st.st_gid)
    os.replace(tmp, CONF)

    print(f"已{'开启' if enable else '关闭'}用户 {user} 的 GDM 自动登录：")
    for c in changes:
        print("  - " + c)

    # 语法自检：gdm 的配置文件用 configparser 读，读不动就是写坏了
    import configparser
    cp = configparser.ConfigParser()
    try:
        cp.read(CONF)
    except Exception as exc:
        print(f"警告：改完的配置无法被解析（{exc}），正在回滚", file=sys.stderr)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(before)
        os.chmod(tmp, st.st_mode & 0o777)
        os.chown(tmp, st.st_uid, st.st_gid)
        os.replace(tmp, CONF)
        return 3

    rc = subprocess.run(["systemctl", "is-active", "gdm3", "gdm"],
                        capture_output=True, text=True).returncode
    print("提示：这个改动在下次重启或重启 gdm 之后才影响登录界面。")
    if not enable:
        print("提示：关闭自动登录后，开机需要先用密码登录进桌面，之后人脸才接管锁屏解锁。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
