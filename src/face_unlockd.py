#!/usr/bin/env python3
"""无感解锁守护进程。

两个条件同时成立才开摄像头：**屏幕锁定** 且 **屏幕亮着**。熄屏期间只做轻量 `xset q`
轮询（实测 <1ms），鼠标一动 DPMS 翻回 on，下一个轮询周期（默认 0.25s）就开窗扫描 ——
「息屏后回来点鼠标」不再有死区。认出本人后把登录密码注入当前获得键盘焦点的输入框。
抓帧全程 headless，只有注入那一下用 xdotool 碰 X。

安全边界（重要，别绕过）：
  * 注入前重新确认 GetActive==true。屏幕没锁时绝不发任何按键，否则按键会落到你
    正在用的窗口里 —— 那就是往你的编辑器里打字。
  * 亮屏判定必须 fail-open：读不到 DPMS 状态就当作「亮着」照常扫。读不到就不扫会让
    门禁静默失效，比原来的死区严重得多。
  * 只有真的注入过密码却没解开才罚退避；「没看见人」「熄屏」「窗到期」都不算失败。
  * 不使用 ctrl+a / Delete 之类会清掉焦点窗口内容的按键。重试依赖 GNOME 自己在
    密码错误后清空输入框。
  * 单次锁屏最多注入 MAX_ATTEMPTS 次，然后退避，避免无限错密。
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
import facelib
from common import log, warn

_shutdown = False

# 连续扫描时的心跳间隔。窗长改成 600s 之后，2s 一次会把 journald 灌满，
# 8s 足够定位问题又不刷屏。
HEARTBEAT_S = 8.0


def _on_signal(signum, _frame):
    global _shutdown
    _shutdown = True
    log(f"收到信号 {signum}，准备退出")


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


class Unlocker:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.engine = facelib.FaceEngine(
            det_size=common.cfg_size(cfg, "DET_SIZE"),
            score_threshold=common.cfg_float(cfg, "DET_SCORE"),
            min_face_px=common.cfg_int(cfg, "MIN_FACE_PX"),
            camera_index=common.cfg_int(cfg, "CAMERA_INDEX"),
            capture_size=common.cfg_size(cfg, "CAPTURE_SIZE"),
        )
        self.threshold = common.cfg_float(cfg, "THRESHOLD")
        self.streak_n = common.cfg_int(cfg, "STREAK")
        self.streak_window = common.cfg_float(cfg, "STREAK_WINDOW_S")
        self.anti_spoof = common.cfg_int(cfg, "ANTI_SPOOF") == 1
        self.min_diff = float(cfg.get("ANTI_SPOOF_MIN_DIFF", "2.5"))
        # 亮屏门控：熄屏时不占摄像头，动一下鼠标立刻恢复扫描。
        # 读不到 DPMS 状态时一律当作亮着 —— 宁可多扫，也不能因为读不到就静默不扫。
        self.screen_gate = common.cfg_int(cfg, "SCAN_ONLY_WHEN_SCREEN_ON") == 1
        self._monitor_warned = False
        self.templates, self.meta = facelib.load_templates(common.template_path())
        if len(self.templates) == 0:
            raise RuntimeError("还没有人脸模板，先运行 face-enroll")
        self.secret = common.read_secret()
        self.xenv = common.x_environment(cfg)
        self.frame_interval = 1.0 / max(1.0, common.cfg_float(cfg, "SCAN_FPS"))
        log(f"就绪：{len(self.templates)} 条模板，阈值 {self.threshold:.3f}，"
            f"连续 {self.streak_n} 帧，防静态图={self.anti_spoof}，"
            f"亮屏门控={'开' if self.screen_gate else '关'}，{self.xenv['DISPLAY']}")

    # ---------- 键盘注入 ----------

    def _xdotool(self, *args: str) -> int:
        return subprocess_run(["xdotool", *args], env=self.xenv)

    def wake_prompt(self) -> None:
        """把 GNOME 锁屏从时钟页唤醒到密码框。space+BackSpace 净效果为空，
        即使落进了输入框也不会留下字符。"""
        wake = self.cfg.get("WAKE_KEYS", "space BackSpace").split()
        for key in wake:
            self._xdotool("key", "--clearmodifiers", key)
            time.sleep(0.12)

    def inject_password(self) -> None:
        self._xdotool("type", "--clearmodifiers",
                      "--delay", self.cfg.get("TYPE_DELAY_MS", "25"), self.secret)
        time.sleep(0.15)
        self._xdotool("key", "--clearmodifiers", "Return")

    # ---------- 亮屏判定 ----------

    def _monitor_on(self) -> bool:
        """屏幕是否亮着。门控关闭、DPMS 未启用、或读不到状态时一律返回 True。

        这一条必须 fail-open：如果 xset 读不到就不扫，门禁会在你最需要它的时候静默失效，
        那比原来的 61 秒死区严重得多。
        """
        if not self.screen_gate:
            return True
        state, enabled = common.monitor_power(self.xenv)
        if state is None or not enabled:
            if not self._monitor_warned:
                warn(f"读不到 DPMS 亮屏状态（{common.describe_monitor(self.xenv)}），"
                     f"降级为持续扫描")
                self._monitor_warned = True
            return True
        self._monitor_warned = False
        return state == "on"

    def wait_for_screen_on(self) -> str:
        """熄屏期间只做轻量轮询，不碰摄像头。鼠标一动 DPMS 就翻回 on，下个轮询周期即恢复。"""
        poll = common.cfg_float(self.cfg, "WAKE_POLL_S")
        announced = False
        while not _shutdown:
            if self.disabled():
                return "已暂停"
            if not self._still_locked():
                return "外部已解锁"
            if self._monitor_on():
                return "亮屏"
            if not announced:
                log("屏幕熄灭中，不占摄像头；动一下鼠标就立刻开始扫")
                announced = True
            time.sleep(poll)
        return "退出"

    # ---------- 一个扫描窗 ----------

    def handle_lock(self) -> str:
        """锁屏且亮屏期间的一次连续扫描。

        返回的标签决定上层要不要罚退避：只有「尝试用尽」（真的注入了密码却没解开）才罚，
        「熄屏」「扫描窗到期」「没看见人」都不算失败，直接无缝开下一个窗。
        """
        cfg = self.cfg
        settle = common.cfg_float(cfg, "LOCK_SETTLE_S")
        window = common.cfg_float(cfg, "SCAN_WINDOW_S")
        max_attempts = common.cfg_int(cfg, "MAX_ATTEMPTS")

        time.sleep(settle)
        if not self._still_locked():
            return "已解锁"
        if not self._monitor_on():
            return "熄屏"

        try:
            cap = self.engine.open_camera()
        except Exception as exc:
            warn(f"摄像头打不开：{exc}")
            return "摄像头不可用"

        streak: list[tuple[float, np.ndarray]] = []
        deadline = time.monotonic() + window
        attempts = 0
        last_beat = 0.0
        best_sim = -1.0
        result = "扫描窗到期"
        try:
            while time.monotonic() < deadline and not _shutdown:
                if self.disabled():
                    log("已被 face-ctl pause 暂停，停止本次扫描")
                    return "已暂停"
                if not self._still_locked():
                    return "外部已解锁"
                if not self._monitor_on():
                    log("屏幕熄灭，结束本次扫描窗")
                    return "熄屏"
                frame = self.engine.grab(cap)
                if frame is None:
                    warn("读帧失败，摄像头可能被占用")
                    time.sleep(0.5)
                    continue

                now = time.monotonic()
                if now - last_beat > HEARTBEAT_S:
                    log(f"扫描中… 当前最佳相似度 {best_sim:.3f}/{self.threshold:.3f}")
                    last_beat = now

                try:
                    face = self.engine.best_face(frame)
                    crop = self.engine.recognizer.alignCrop(frame, face)
                    emb = self.engine.embedding(frame, face)
                except facelib.NoFace as exc:
                    if streak:
                        log(f"确认中断：{exc}")
                    streak.clear()
                    best_sim = -1.0
                    time.sleep(self.frame_interval)
                    continue

                sim = self.engine.max_similarity(emb, self.templates)
                best_sim = max(best_sim, sim)
                if sim < self.threshold:
                    if streak:
                        log(f"确认中断：相似度 {sim:.3f} 低于阈值")
                    streak.clear()
                    best_sim = sim if sim > 0 else -1.0
                    time.sleep(self.frame_interval)
                    continue

                streak.append((now, crop))
                # 只认时间窗内的连续帧
                while streak and now - streak[0][0] > self.streak_window:
                    streak.pop(0)
                log(f"匹配 {len(streak)}/{self.streak_n}  sim={sim:.3f}")

                if len(streak) < self.streak_n:
                    time.sleep(self.frame_interval)
                    continue

                if self.anti_spoof:
                    d = facelib.FaceEngine.frame_diff(streak[0][1], streak[-1][1])
                    if d < self.min_diff:
                        log(f"画面过于静止（diff={d:.2f}<{self.min_diff}），"
                            f"疑似静态图像，继续观察")
                        time.sleep(self.frame_interval)
                        continue

                attempts += 1
                if attempts > max_attempts:
                    warn(f"本窗已达最大注入次数 {max_attempts}，退避后再来")
                    result = "尝试用尽"
                    break

                log(f"识别通过（sim={sim:.3f}），第 {attempts} 次注入密码")
                if not self._still_locked():
                    return "外部已解锁"
                self.wake_prompt()
                self.inject_password()

                if self._wait_unlocked(timeout=4.0):
                    log("解锁成功")
                    return "成功"
                warn("注入后屏幕仍然锁着，重试（依赖锁屏在错密后自行清空输入框）")
                streak.clear()
                time.sleep(1.0)
        finally:
            cap.release()
        return result

    # ---------- 小工具 ----------

    def _still_locked(self) -> bool:
        try:
            return common.is_screen_locked()
        except Exception as exc:
            warn(f"查锁状态失败：{exc}")
            return False

    def _wait_unlocked(self, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if not self._still_locked():
                return True
            time.sleep(0.2)
        return False

    def disabled(self) -> bool:
        return common.is_paused()


def subprocess_run(cmd: list[str], env: dict) -> int:
    import subprocess
    try:
        return subprocess.run(cmd, env=env, capture_output=True,
                              text=True, timeout=10).returncode
    except Exception as exc:
        warn(f"{cmd[0]} 调用失败：{exc}")
        return -1


def run_forever() -> int:
    cfg = common.load_config()
    try:
        u = Unlocker(cfg)
    except Exception as exc:
        warn(f"启动失败：{exc}")
        return 1

    idle = common.cfg_float(cfg, "IDLE_POLL_S")
    backoff = common.cfg_float(cfg, "BACKOFF_S")
    dbus_errors = 0
    enabled = cfg.get("ENABLED", "1") == "1"
    last_cfg_read = time.monotonic()

    while not _shutdown:
        # ENABLED 单独热加载，让 face-ctl off 不需要重启服务就生效。
        # 其它键（阈值、STREAK 等）仍在 Unlocker 初始化时绑定，改了要 face-ctl restart。
        if time.monotonic() - last_cfg_read > 2.0:
            last_cfg_read = time.monotonic()
            fresh = common.load_config()
            now_enabled = fresh.get("ENABLED", "1") == "1"
            if now_enabled != enabled:
                log(f"ENABLED 热加载：{enabled} -> {now_enabled}")
            enabled = now_enabled

        if not enabled or u.disabled():
            time.sleep(1.0)
            continue
        try:
            locked = common.is_screen_locked()
            dbus_errors = 0
        except Exception as exc:
            dbus_errors += 1
            warn(f"锁状态查询异常（第 {dbus_errors} 次）：{exc}")
            if dbus_errors >= 10:
                warn("连续失败过多，退出交给 systemd 重启")
                return 1
            time.sleep(2.0)
            continue

        if not locked:
            time.sleep(idle)
            continue

        # 已经锁屏。熄屏时先轻量等亮屏，别把摄像头举给一间黑屋子。
        if u.screen_gate and not u._monitor_on():
            state = u.wait_for_screen_on()
            if state != "亮屏":
                continue
            log("亮屏事件触发，立即开窗扫描")

        outcome = u.handle_lock()
        log(f"本次扫描窗结束：{outcome}")
        if outcome == "尝试用尽":
            # 只有真的注入过密码却没解开才罚退避：那说明密码可能不对，或者有人在硬试。
            # 「没看见人」「熄屏」「窗到期」都不算失败，直接进下一轮。
            end = time.monotonic() + backoff
            while time.monotonic() < end and not _shutdown:
                if not u._still_locked():
                    break
                time.sleep(idle)
        elif outcome == "摄像头不可用":
            time.sleep(5.0)
    return 0


def measure_once() -> int:
    """face-ctl test 用：只报相似度，不注入密码。"""
    cfg = common.load_config()
    engine = facelib.FaceEngine(
        det_size=common.cfg_size(cfg, "DET_SIZE"),
        score_threshold=common.cfg_float(cfg, "DET_SCORE"),
        min_face_px=common.cfg_int(cfg, "MIN_FACE_PX"),
        camera_index=common.cfg_int(cfg, "CAMERA_INDEX"),
        capture_size=common.cfg_size(cfg, "CAPTURE_SIZE"))
    templates, meta = facelib.load_templates(common.template_path())
    if len(templates) == 0:
        warn("没有模板，先 face-enroll")
        return 1
    thr = common.cfg_float(cfg, "THRESHOLD")
    log(f"阈值 {thr:.3f}，模板 {len(templates)} 条（{meta.get('enrolled_at','?')}）")
    cap = engine.open_camera()
    sims = []
    try:
        for i in range(10):
            frame = engine.grab(cap)
            if frame is None:
                continue
            try:
                emb = engine.embed(frame)
            except facelib.NoFace as exc:
                log(f"  第{i+1}帧：{exc}")
                continue
            s = engine.max_similarity(emb, templates)
            sims.append(s)
            log(f"  第{i+1}帧：sim={s:.3f}  {'通过' if s >= thr else '不足'}")
            time.sleep(0.25)
    finally:
        cap.release()
    if not sims:
        warn("10 帧里一帧有效人脸都没有")
        return 2
    log(f"本人相似度 min={min(sims):.3f} max={max(sims):.3f} mean={np.mean(sims):.3f}")
    margin = min(sims) - thr
    if margin < 0.03:
        warn(f"最低值离阈值只剩 {margin:.3f}，戴帽子/侧脸/暗光下容易拒你自己；"
             f"建议把 THRESHOLD 降到 {min(sims)-0.05:.3f} 再补录一些角度")
    else:
        log(f"最低值高出阈值 {margin:.3f}，余量可以")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="只测相似度，不解锁")
    args = ap.parse_args()
    if args.test:
        return measure_once()
    return run_forever()


if __name__ == "__main__":
    raise SystemExit(main())
