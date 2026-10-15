#!/usr/bin/env python3
"""GUI 回归测试。需要图形会话（DISPLAY）。

钉住三类曾经真实发生过的错误：

  A. 启动不得触碰系统
     PageSettings 构造时会用 set_state() 初始化开关，而 Gtk.Switch.set_state() 会触发
     state-set 回调 → 回调里本来会回写配置、并对自动登录发起 pkexec。等于"打开界面"
     就替用户改了一遍 GDM 配置。现在由 _loading 闸门隔开，这里断言启动期间零系统调用。

  B. 每个页面必须真的挂在窗口层级上
     GtkStack 建好、页面也 add_titled 了，但忘了 pack_start(stack) —— 标签栏照常显示、
     切换照常工作，内容却永远空白。

  C. 页面内容不得为空白
     用像素方差判定，而不是"文件生成成功"。空的容器照样能渲染出一张 PNG。
     注意本项的局限：它只能发现"整窗全空"。若某个容器没挂进布局、而标签栏等框架还在，
     像素方差依然很高 —— 那种情况由 B 项（可达性 + 分配尺寸）负责。

  .venv/bin/python tests/gui_test.py
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [os.path.join(ROOT, "gui"), os.path.join(ROOT, "src")]

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

import backend  # noqa: E402
import face_unlock_gui as G  # noqa: E402

PASS, FAIL = [], []


def ck(name, cond, extra=""):
    if cond:
        PASS.append(name)
        print(f"  [通过] {name}  {extra}")
    else:
        FAIL.append(name)
        print(f"  [失败] {name}  {extra}")


def pump(n=40):
    for _ in range(n):
        while Gtk.events_pending():
            Gtk.main_iteration()


def main() -> int:
    if not os.environ.get("DISPLAY"):
        print("需要 DISPLAY（请在桌面终端运行）")
        return 1

    calls: list[str] = []
    orig = {n: getattr(backend, n) for n in
            ("set_params", "set_autologin", "restart_service", "enable_service")}

    def rec(name, ret):
        def f(*a, **k):
            calls.append(f"{name}{a[:1]}")
            return ret
        return f

    try:
        backend.set_params = rec("set_params", (True, "stub"))
        backend.set_autologin = rec("set_autologin", (True, "stub"))
        backend.restart_service = rec("restart_service", (True, "stub"))
        backend.enable_service = rec("enable_service", (True, "stub"))

        print("== A. 启动期间不得触碰系统 ==")
        win = G.Window()
        win.show_all()
        pump()
        ck("构造+显示窗口期间没有任何写操作",
           calls == [], f"实际调用 {calls}")
        ck("没有对自动登录发起 pkexec",
           not any(c.startswith("set_autologin") for c in calls), str(calls))

        print("\n== B. 页面必须挂在窗口层级上 ==")
        tree = G.descendants(win)
        ck("GtkStack 本体在窗口树里", win.stack in tree)
        # 光"在树里"不够：pack 了但尺寸塌成 0 也一样看不见内容
        pump()
        w, h = win.stack.get_allocated_width(), win.stack.get_allocated_height()
        ck("GtkStack 有实际分配尺寸（不是 0）", w > 100 and h > 100, f"{w}x{h}")
        for n in G.PAGES:
            page = win.stack.get_child_by_name(n)
            ck(f"页面「{n}」可达且有子控件", page in tree and len(page.get_children()) > 0,
               f"子控件 {len(page.get_children()) if page else '—'}")

        print("\n== C. 用户操作仍然生效（闸门没把正常路径也堵死）==")
        for key in ("ENABLED", "SCAN_ONLY_WHEN_SCREEN_ON"):
            sw = win.settings.switches[key]
            cur = sw.get_state()
            calls.clear()
            win.settings._on_switch(sw, not cur, key)
            ck(f"手动切「{key}」会写配置", any(c.startswith("set_params") for c in calls), str(calls))
        calls.clear()
        win.settings._on_autologin(win.settings.al_sw, True)
        ck("手动切自动登录会走提权路径",
           any(c.startswith("set_autologin") for c in calls), str(calls))
        ck("上述手动操作后窗口仍存活（回退状态没有递归）", win.get_property("visible"))

        print("\n== D. 逐页像素方差：内容不得为空白 ==")
        import numpy as np
        for n in G.PAGES:
            off = Gtk.OffscreenWindow()
            off.set_default_size(900, 660)
            content = win.get_child()
            win.remove(content)
            off.add(content)
            win.stack.set_visible_child_name(n)
            off.show_all()
            pump(60)
            pb = off.get_pixbuf()
            if pb is None:
                ck(f"页面「{n}」渲染成功", False, "离屏表面为 None")
            else:
                px = np.frombuffer(pb.get_pixels(), np.uint8).reshape(pb.get_height(), pb.get_width(), -1)
                gray = px[:, :, :3].reshape(px.shape[0], -1).std()
                # 纯色（空白容器）的标准差接近 0；有控件和文字时远大于此
                ck(f"页面「{n}」有实际内容", gray > 2.0, f"灰度标准差 {gray:.2f}（阈值 2.0）")
            # 先摘出 offscreen 再放回窗口：content 一次只能有一个父容器
            off.remove(content)
            win.add(content)
            off.destroy()
        win.destroy()
    finally:
        for n, f in orig.items():
            setattr(backend, n, f)

    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print("   - " + f)
    print("=" * 56)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())