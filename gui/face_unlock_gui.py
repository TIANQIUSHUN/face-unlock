#!/usr/bin/env python3
"""face-unlock 桌面界面。

业务判断全部在 backend.py，这里只做控件与渲染。录脸用 GLib.timeout_add 每帧驱动
backend.Enroller.step()，不开线程：画面、进度、异常都在同一条时间线上。

  face-unlock-gui               正常打开
  face-unlock-gui --render-test 离屏渲染各页并存 PNG，用于自检与排版检查
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
# backend/facelib/common 住在 src/ 下；打包态与开发态都是这个相对关系
for _p in (_HERE, os.path.join(_ROOT, "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib, Pango  # noqa: E402

import backend  # noqa: E402

APP_TITLE = "人脸识别解锁"
PREVIEW_W = 480
TIMER_MS = 33          # ~30fps 驱动一帧；识别本身比这慢，多余帧自然被跳过


def pixbuf_from_rgb(data: bytes, w: int, h: int) -> GdkPixbuf.Pixbuf:
    return GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(data), GdkPixbuf.Colorspace.RGB, False, 8, w, h, w * 3)


def save_pixbuf(pb: GdkPixbuf.Pixbuf, path: str) -> None:
    pb.savev(path, "png", [], [])


class InfoBar(Gtk.Box):
    """顶部横幅，用来放 Wayland 这类「功能不成立」的硬信息，不靠弹窗打扰。"""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._label = Gtk.Label(xalign=0)
        self._label.set_line_wrap(True)
        self._label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self._box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._box.set_margin_top(6)
        self._box.set_margin_bottom(6)
        self._box.set_margin_start(10)
        self._box.set_margin_end(10)
        self._box.pack_start(self._label, True, True, 0)
        self.pack_start(self._box, False, False, 0)
        self.hide()

    def show_msg(self, text: str, kind: str = "info") -> None:
        colors = {"info": "#e8f0fe", "warn": "#fff3cd", "error": "#f8d7da"}
        css = f"banner {{ background: {colors.get(kind, colors['info'])}; border-radius: 6px; padding: 8px; }}"
        self._label.set_text(text)
        self._box.get_style_context().add_class("banner")
        self._apply_css(css)
        self.show_all()

    def clear(self) -> None:
        self.hide()

    def _apply_css(self, css: str) -> None:
        provider = getattr(self, "_css", None)
        if provider is None:
            provider = Gtk.CssProvider()
            Gtk.StyleContext.add_provider_for_screen(
                Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            self._css = provider
        provider.load_from_data(css.encode())


class PageOverview(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.app = app
        self.set_border_width(14)
        self.grid = Gtk.Grid(column_spacing=14, row_spacing=6)
        self._rows: dict[str, Gtk.Label] = {}
        for key, title in [
            ("session", "会话类型"), ("unlock", "解锁功能"), ("service", "守护进程"),
            ("autologin", "开机自动登录"), ("screen", "当前屏幕"), ("monitor", "显示器电源"),
            ("templates", "已录入人脸"), ("paused", "扫描暂停"), ("config", "配置文件"),
            ("data", "数据目录"),
        ]:
            lab = Gtk.Label(label=title + "：", xalign=1)
            lab.get_style_context().add_class("dim-label")
            val = Gtk.Label(xalign=0)
            val.set_line_wrap(True)
            # 只有路径值得复制；状态行开可选中会被 Yaru 主题画成选中态（橙色块）
            val.set_selectable(key in ("config", "data"))
            self._rows[key] = val
            self.grid.attach(lab, 0, len(self._rows) - 1, 1, 1)
            self.grid.attach(val, 1, len(self._rows) - 1, 1, 1)
        self.pack_start(self.grid, False, False, 0)

        self.deps = Gtk.Frame(label="依赖体检")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_border_width(8)
        self._deps_label = Gtk.Label(xalign=0)
        self._deps_label.set_line_wrap(True)
        self._deps_label.set_use_markup(True)
        self._deps_label.set_selectable(True)
        box.pack_start(self._deps_label, False, False, 0)
        self.deps.add(box)
        self.pack_start(self.deps, False, False, 0)

        btns = Gtk.Box(spacing=8)
        r = Gtk.Button(label="重新检查")
        r.connect("clicked", lambda *_: self.refresh())
        btns.pack_start(r, False, False, 0)
        self.enab = Gtk.Button(label="启用服务（开机自启）")
        self.enab.connect("clicked", self._on_enable)
        btns.pack_start(self.enab, False, False, 0)
        self.pack_start(btns, False, False, 0)
        self.refresh()

    def _on_enable(self, _b) -> None:
        ok, msg = backend.enable_service(True)
        self.app.status(f"启用服务：{'成功' if ok else '失败'} — {msg[:120]}", not ok)
        self.refresh()

    def refresh(self) -> None:
        ov = backend.overview()
        r = self._rows
        st = ov["session_type"].upper()
        r["session"].set_text(f"{st}  （依据：{ov['support_reason'].split('（')[-1].rstrip('）')[:60]}）")
        r["unlock"].set_text("可用" if ov["supported"] else "不可用")
        r["service"].set_text(f"{ov['service']['active']}   自启 {ov['service']['enabled']}")
        al = ov["autologin"]
        r["autologin"].set_text(("已开启（" + str(ov["autologin_detail"]) + "）")
                                if al else f"未开启 — {ov['autologin_detail']}")
        r["screen"].set_text({True: "已锁定", False: "未锁定", None: "查询失败"}[ov["screen_locked"]])
        r["monitor"].set_text(ov["monitor"])
        r["templates"].set_text(f"{ov['templates']} 条特征")
        r["paused"].set_text("是（不会扫描）" if ov["paused"] else "否")
        r["config"].set_text(ov["config_path"])
        r["data"].set_text(ov["data_path"])
        lines = []
        for name, okk, detail in ov["deps"]:
            mark = "<span foreground='#1a7f37'>[OK]</span>" if okk else "<span foreground='#b42318'>[FAIL]</span>"
            lines.append(f"{mark} {GLib.markup_escape_text(name)} — {GLib.markup_escape_text(detail)}")
        self._deps_label.set_markup("\n".join(lines))
        self.enab.set_visible(ov["service"]["active"] != "active" or "failed" in ov["service"]["enabled"])


class PageEnroll(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.app = app
        self.set_border_width(14)
        self.enroller = None
        self.timer = None

        self.image = Gtk.Image()
        self.image.set_size_request(PREVIEW_W, 300)
        frame = Gtk.Frame()
        frame.add(self.image)
        self.pack_start(frame, True, True, 0)

        self.status = Gtk.Label(label="点「开始录入」打开摄像头；过程中请跟着提示缓缓转头",
                                xalign=0)
        self.status.set_line_wrap(True)
        self.pack_start(self.status, False, False, 0)

        self.bar = Gtk.ProgressBar()
        self.pack_start(self.bar, False, False, 0)

        row = Gtk.Box(spacing=10)
        row.pack_start(Gtk.Label(label="采集张数"), False, False, 0)
        self.count = Gtk.SpinButton.new_with_range(6, 30, 1)
        self.count.set_value(12)
        row.pack_start(self.count, False, False, 0)
        self.append_cb = Gtk.CheckButton(label="追加到已有模板（不覆盖）")
        row.pack_start(self.append_cb, False, False, 0)
        self.pack_start(row, False, False, 0)

        btns = Gtk.Box(spacing=8)
        self.start_b = Gtk.Button(label="开始录入")
        self.start_b.connect("clicked", self._start)
        self.stop_b = Gtk.Button(label="中止")
        self.stop_b.set_sensitive(False)
        self.stop_b.connect("clicked", self._stop)
        self.clear_b = Gtk.Button(label="清空已录入")
        self.clear_b.connect("clicked", self._clear)
        btns.pack_start(self.start_b, False, False, 0)
        btns.pack_start(self.stop_b, False, False, 0)
        btns.pack_start(self.clear_b, False, False, 0)
        self.pack_start(btns, False, False, 0)

        hint = Gtk.Label(xalign=0)
        hint.set_line_wrap(True)
        hint.set_markup(
            "<b>提示</b>：录入时画面里只能有一张脸——身后海报、手机屏幕或镜子里的人像会让程序"
            "拒绝判定归属。跟着提示缓缓转头，多角度样本能让暗光、侧脸时更稳定。")
        self.pack_start(hint, False, False, 0)

    def _start(self, _b) -> None:
        self._teardown()
        self.enroller = backend.Enroller(target=int(self.count.get_value()),
                                        append=self.append_cb.get_active())
        self.enroller.start()
        self.start_b.set_sensitive(False)
        self.stop_b.set_sensitive(True)
        self.status.set_text(self.enroller.message)
        self.timer = GLib.timeout_add(TIMER_MS, self._tick)

    def _tick(self) -> bool:
        if self.enroller is None:
            return False
        frame, msg, n, target, finished = self.enroller.step()
        self.status.set_text(msg)
        self.bar.set_fraction(n / max(1, target))
        if frame is not None:
            data, w, h = self.enroller.preview_rgb(frame)
            if data:
                self.image.set_from_pixbuf(pixbuf_from_rgb(data, w, h))
        if finished:
            self._finish()
            return False
        return True

    def _finish(self) -> None:
        self.timer = None
        self.start_b.set_sensitive(True)
        self.stop_b.set_sensitive(False)
        n = self._rows_done()
        self.app.status(f"录入结束：{n} 条特征已写入" if n else "录入未产生新模板", n == 0)
        self.enroller = None

    def _rows_done(self) -> int:
        import facelib
        return len(facelib.load_templates(backend.common.template_path())[0])

    def _stop(self, _b) -> None:
        if self.enroller:
            self.enroller.finish(commit=False)
            self.enroller = None
        self._teardown()
        self.status.set_text("已中止，本次采集未保存")
        self.bar.set_fraction(0)

    def _teardown(self) -> None:
        if self.timer:
            GLib.source_remove(self.timer)
        self.timer = None
        self.start_b.set_sensitive(True)
        self.stop_b.set_sensitive(False)

    def _clear(self, _b) -> None:
        d = Gtk.MessageDialog(transient_for=self.app, modal=True,
                              message_type=Gtk.MessageType.WARNING, text="清空人脸模板？",
                              secondary_text="清空后自动解锁会停止工作，直到重新录入。原文件会留一个 .bak 备份。")
        d.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_DELETE, Gtk.ResponseType.YES)
        if d.run() == Gtk.ResponseType.YES:
            ok, msg = backend.clear_templates()
            self.app.status(msg, not ok)
        d.destroy()


class PageSettings(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.app = app
        self.set_border_width(14)
        self.spins: dict[str, Gtk.SpinButton] = {}
        self.switches: dict[str, Gtk.Switch] = {}
        # 程序化 load() 期间为 True：此时信号回调必须直接放行，
        # 否则「打开设置页」就等于「替用户写一遍配置」，自动登录那项还会弹 pkexec 授权框
        self._loading = False

        box = Gtk.Frame(label="开关")
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        inner.set_border_width(10)
        for key, (title, desc) in backend.BOOL_FIELDS.items():
            row = Gtk.Box(spacing=10)
            sw = Gtk.Switch(valign=Gtk.Align.CENTER)
            sw.connect("state-set", self._on_switch, key)
            self.switches[key] = sw
            lab = Gtk.Label(xalign=0)
            lab.set_markup(f"<b>{title}</b>\n<small>{desc}</small>")
            lab.set_line_wrap(True)
            row.pack_start(sw, False, False, 0)
            row.pack_start(lab, True, True, 0)
            inner.pack_start(row, False, False, 0)
        self.al_row = Gtk.Box(spacing=10)
        self.al_sw = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.al_sw.connect("state-set", self._on_autologin)
        al_lab = Gtk.Label(xalign=0)
        al_lab.set_markup("<b>开机自动登录</b>\n<small>写 /etc/gdm3/custom.conf，需要管理员授权。"
                          "关闭后开机要先用密码登录进桌面，人脸只接管之后的锁屏。</small>")
        al_lab.set_line_wrap(True)
        self.al_row.pack_start(self.al_sw, False, False, 0)
        self.al_row.pack_start(al_lab, True, True, 0)
        inner.pack_start(self.al_row, False, False, 0)
        box.add(inner)
        self.pack_start(box, False, False, 0)

        gridbox = Gtk.Frame(label="参数（改完点保存，会自动重启服务）")
        grid = Gtk.Grid(column_spacing=14, row_spacing=6)
        grid.set_border_width(10)
        cfg = backend.get_params()
        for i, (key, (title, desc, kind, (lo, hi))) in enumerate(backend.PARAM_FIELDS.items()):
            lab = Gtk.Label(label=title + "：", xalign=1)
            lab.get_style_context().add_class("dim-label")
            adj = Gtk.Adjustment(value=float(cfg.get(key, str(lo))), lower=lo, upper=hi,
                                 step_increment=(1 if kind == "int" else 0.01),
                                 page_increment=(5 if kind == "int" else 0.1))
            sp = Gtk.SpinButton(adjustment=adj, digits=(0 if kind == "int" else 3))
            sp.set_tooltip_text(f"{desc}\n允许范围 {lo} ~ {hi}   配置键 {key}")
            self.spins[key] = sp
            grid.attach(lab, 0, i, 1, 1)
            grid.attach(sp, 1, i, 1, 1)
        gridbox.add(grid)
        self.pack_start(gridbox, False, False, 0)

        row = Gtk.Box(spacing=8)
        save = Gtk.Button(label="保存并重启服务")
        save.connect("clicked", self._save)
        reset = Gtk.Button(label="重新读取")
        reset.connect("clicked", lambda *_: self.load())
        test = Gtk.Button(label="测一次相似度")
        test.connect("clicked", self._test)
        row.pack_start(save, False, False, 0)
        row.pack_start(reset, False, False, 0)
        row.pack_start(test, False, False, 0)
        self.pack_start(row, False, False, 0)
        self.load()

    def load(self) -> None:
        self._loading = True
        try:
            cfg = backend.get_params()
            for k, sp in self.spins.items():
                sp.set_value(float(cfg.get(k, sp.get_adjustment().get_lower())))
            for k, sw in self.switches.items():
                sw.set_state(cfg.get(k, "1") == "1")
            al, _ = backend.autologin_state()
            self.al_sw.set_state(bool(al))
        finally:
            self._loading = False

    def _on_switch(self, sw, state, key) -> bool:
        if self._loading:
            return False          # 程序化赋值，不是用户操作，别去动系统
        ok, msg = backend.set_params({key: "1" if state else "0"})
        if ok and key == "ENABLED":
            # ENABLED 已改成热加载，这里只是让界面与 systemd 状态立刻一致
            backend.restart_service()
        title = backend.BOOL_FIELDS[key][0]
        self.app.status(f"{title}：{msg}", not ok)
        # 接管事件：写失败就把控件拨回去，界面上绝不留下一个没生效的开关状态
        sw.set_state(state if ok else not state)
        return True

    def _on_autologin(self, sw, state) -> bool:
        if self._loading:
            return False          # 这条尤其要紧：否则每次开界面都会弹一次 pkexec 改 GDM 配置
        if not backend.unlock_supported()[0]:
            self.app.status("当前不是 X11 会话，自动解锁本就不可用，未改动自动登录", True)
            sw.set_state(not state)
            return True
        ok, msg = backend.set_autologin(state)
        self.app.status(("自动登录已" + ("开启" if state else "关闭")) if ok
                        else f"自动登录修改失败：{msg}", not ok)
        if not ok:
            sw.set_state(not state)   # 改系统没成功就不让界面上留下一个假状态
        return True

    def _save(self, _b) -> None:
        values = {}
        for k, sp in self.spins.items():
            values[k] = sp.get_text()
        for k, sw in self.switches.items():
            values[k] = "1" if sw.get_state() else "0"
        ok, msg = backend.set_params(values)
        if ok:
            ok2, msg2 = backend.restart_service()
            msg += "；服务" + ("已重启生效" if ok2 else f"重启失败：{msg2[:80]}")
        self.app.status(msg, not ok)
        self.app.overview.refresh()

    def _test(self, _b) -> None:
        """一次相似度测量要开摄像头跑十几帧，先让界面画出来再干，否则会像卡死。"""
        self.app.status("正在测量：请正对摄像头坐着…")
        GLib.timeout_add(150, self._test_run)

    def _test_run(self) -> bool:
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "src", "face_unlockd.py")
        rc, out = backend.run([sys.executable, script, "--test"], timeout=40)
        tail = "\n".join(out.splitlines()[-6:]) or f"(无输出 exit={rc})"
        self.app.status(tail, rc not in (0, 2))
        return False


class PageLog(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.app = app
        self.set_border_width(14)
        self.view = Gtk.TextView()
        self.view.set_editable(False)
        self.view.set_monospace(True)
        self.view.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        scroll = Gtk.ScrolledWindow()
        scroll.add(self.view)
        self.pack_start(scroll, True, True, 0)
        row = Gtk.Box(spacing=8)
        for label, fn in (("刷新", self.refresh), ("打开完整日志", self._open_full)):
            b = Gtk.Button(label=label)
            b.connect("clicked", fn)
            row.pack_start(b, False, False, 0)
        self.pack_start(row, False, False, 0)
        self.refresh()

    def _open_full(self, *_):
        backend.run(["systemctl", "--user", "start", "face-unlockd.service"])
        uri = "journalctl --user -u " + backend.UNIT
        self.app.status("终端里执行：" + uri + " -f")

    def refresh(self, *_):
        text = backend.logs(120)
        buf = self.view.get_buffer()
        buf.set_text(text or "(暂无日志)")
        buf.place_cursor(buf.get_end_iter())
        return False


class Window(Gtk.Window):
    def __init__(self):
        super().__init__(title=APP_TITLE)
        self.set_default_size(880, 620)
        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        hb.set_title(APP_TITLE)
        self.set_titlebar(hb)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(vbox)

        # 状态栏先建：页面构造期间就会触发信号回调，那些回调会往状态栏写提示。
        # 放在页面后面就会 AttributeError —— 而那个异常恰好掩盖了一个更糟的后果（见下）。
        self.statusbar = Gtk.Label(xalign=0)
        self.statusbar.set_padding(6, 6)
        self.statusbar.set_line_wrap(True)
        self.statusbar.hide()
        vbox.pack_end(self.statusbar, False, False, 0)

        self.banner = InfoBar()
        vbox.pack_start(self.banner, False, False, 0)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        sw = Gtk.StackSwitcher()
        sw.set_stack(self.stack)
        box = Gtk.Box()
        box.pack_start(sw, False, False, 0)
        vbox.pack_start(box, False, False, 0)

        self.overview = PageOverview(self)
        self.enroll = PageEnroll(self)
        self.settings = PageSettings(self)
        self.log = PageLog(self)
        for name, page in (("总览", self.overview), ("录入人脸", self.enroll),
                           ("设置", self.settings), ("日志", self.log)):
            self.stack.add_titled(page, name, name)
        # 少了这一行，整个页面区就不在窗口层级里：标签栏照常显示、切换照常工作，
        # 但内容永远空白。我第一版就漏了它，而旧的 render-test 把 page 从 stack 里
        # 取出来单独渲染，恰好绕过了这个缺失，所以没抓到。
        vbox.pack_start(self.stack, True, True, 0)

        self.connect("delete-event", self._on_close)
        self._guard_session()
        GLib.timeout_add_seconds(5, self._auto_refresh)

    def _guard_session(self) -> None:
        sup, why = backend.unlock_supported()
        if not sup:
            self.banner.show_msg("自动解锁不可用：" + why, "error")
            self.settings.al_sw.set_sensitive(False)
            self.settings.switches["ENABLED"].set_sensitive(False)
            self.enroll.start_b.set_sensitive(True)   # 录脸本身不依赖 X，仍允许

    def _auto_refresh(self) -> bool:
        if self.stack.get_visible_child_name() == "总览":
            self.overview.refresh()
        return True

    def status(self, text: str, is_error: bool = False) -> None:
        ctx = self.statusbar.get_style_context()
        if is_error:
            ctx.add_class("errorbar")
        else:
            ctx.remove_class("errorbar")
        self.statusbar.set_text(("⚠ " if is_error else "") + text)
        self.statusbar.show()

    def _on_close(self, *_):
        if self.enroll.timer:
            self.enroll._stop(None)      # 别让摄像头在线程关掉后还被占着
        Gtk.main_quit()
        return False


def descendants(widget, acc=None):
    """从 widget 出发遍历整棵控件树。用来断言「页面真的挂在窗口上」。"""
    acc = set() if acc is None else acc
    acc.add(widget)
    if isinstance(widget, Gtk.Container):
        for child in widget.get_children():
            descendants(child, acc)
    return acc


PAGES = ("总览", "录入人脸", "设置", "日志")


def check_tree() -> tuple[bool, list[str]]:
    """不需要显示窗口就能抓出「容器没挂进布局」这类问题。"""
    win = Window()
    win.show_all()
    while Gtk.events_pending():
        Gtk.main_iteration()
    tree = descendants(win)
    missing = [n for n in PAGES if win.stack.get_child_by_name(n) not in tree]
    if win.stack not in tree:
        missing.append("GtkStack 本体")
    win.destroy()
    return (not missing), missing


def render_test(outdir: str) -> int:
    """离屏渲染每页并存 PNG：不用把窗口显示到屏幕上，就能检查排版与文案。

    每页都新建一个 Window 再把那一页搬进 OffscreenWindow —— page 只能有一个父容器，
    搬走之后 destroy 会连 page 一起销毁，所以绝不能复用同一个 Window 逐页渲染。
    """
    os.makedirs(outdir, exist_ok=True)
    names = ("总览", "录入人脸", "设置", "日志")
    rc = 0
    for name in names:
        win = Window()
        win.show_all()
        off = Gtk.OffscreenWindow()
        off.set_default_size(900, 660)
        # 搬走的是窗口的整体内容（含标签栏 + stack），不是单个 page。
        # 之前只搬 page，等于把 stack 这一层跳过，才让「stack 没挂进布局」的 bug 溜过去。
        content = win.get_child()
        win.remove(content)
        off.add(content)
        win.stack.set_visible_child_name(name)
        off.show_all()
        for _ in range(40):
            while Gtk.events_pending():
                Gtk.main_iteration()
        pb = off.get_pixbuf()
        if pb is None:
            print(f"  渲染失败: {name}（离屏表面为 None）")
            rc = 1
        else:
            path = os.path.join(outdir, f"page-{name}.png")
            save_pixbuf(pb, path)
            print(f"  已存 {path}  {pb.get_width()}x{pb.get_height()}")
        off.destroy()
        win.destroy()
    return rc


def main() -> int:
    if "--render-test" in sys.argv:
        out = sys.argv[sys.argv.index("--render-test") + 1] if len(
            sys.argv) > sys.argv.index("--render-test") + 1 and not sys.argv[
            sys.argv.index("--render-test") + 1].startswith("--") else "/tmp/face-unlock-gui"
        return render_test(out)
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        print("需要图形会话（DISPLAY 或 WAYLAND_DISPLAY）。请在桌面终端里运行。", file=sys.stderr)
        return 1
    Gtk.init(None)
    win = Window()
    win.show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
