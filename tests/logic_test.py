#!/usr/bin/env python3
"""判定逻辑测试（打桩引擎，不需要人、不需要摄像头里有人）。

flow_test.py 验的是「真人能不能被认出」，本文件验的是「认出来之后那一段程序的行为对不对」，
尤其是几条安全性质：
  * 屏幕没锁 —— 一个按键都不发（否则按键会落进你正在用的窗口）
  * 注入前会二次确认仍处于锁定
  * 密码错误重复几次后封顶，不死循环
  * 完全静止的画面不放行
  * pause 能立即止血
  * 摄像头异常不崩

  .venv/bin/python tests/logic_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

PASS, FAIL = [], []
LOG = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'通过' if cond else '失败'}] {name}  {detail}", flush=True)


class StubEngine:
    """顶替 facelib.FaceEngine，行为完全由脚本决定。"""

    def __init__(self, frames, kinds, sims, repeat_last=True):
        self.frames = frames
        self.kinds = kinds            # ok / noface / multi / small / none
        self.sims = sims
        self.repeat_last = repeat_last
        self.i = 0
        self.opened = 0
        self.camera_broken = False
        self._cur_kind = "ok"

    class _Cap:
        def release(self):
            pass

    def open_camera(self):
        self.opened += 1
        if self.camera_broken:
            raise RuntimeError("/dev/video0 busy")
        return StubEngine._Cap()

    def grab(self, cap):
        """只负责取帧。抛 NoFace 是 best_face 的职责，不要混。"""
        import facelib
        idx = self.i
        if idx >= len(self.frames):
            if not self.repeat_last:
                self._cur_kind = "eof"
                return None
            idx = len(self.frames) - 1
        k = self.kinds[min(idx, len(self.kinds) - 1)]
        self.i += 1
        self._cur_kind = k
        if k == "none":
            return None
        return self.frames[idx]

    def best_face(self, frame, require_single=True):
        import facelib
        msgs = {"noface": "画面里没有人脸", "multi": "画面里有 2 张脸，无法判定解锁给谁",
                "small": "人脸太小（60x90px），请靠近一点"}
        if self._cur_kind in msgs:
            raise facelib.NoFace(msgs[self._cur_kind])
        return np.zeros(15, np.float32)

    class _Rec:
        @staticmethod
        def alignCrop(frame, face):
            return frame

    recognizer = _Rec()

    def embedding(self, frame, face):
        return np.zeros(128, np.float32)

    def max_similarity(self, probe, templates):
        idx = max(0, self.i - 1)
        s = self.sims[min(idx, len(self.sims) - 1)]
        return s

    # 供 handle_lock 使用
    det_size = (640, 352)


import numpy as np  # noqa: E402


def make_frames(n, distinct=True):
    if distinct:
        return [np.random.RandomState(1000 + k).randint(0, 255, (112, 112, 3), dtype=np.uint8)
                for k in range(n)]
    f = np.random.RandomState(7).randint(0, 255, (112, 112, 3), dtype=np.uint8)
    return [f.copy() for _ in range(n)]


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="face-logic-")
    bindir = os.path.join(tmp, "bin"); os.makedirs(bindir)
    os.environ["FACE_UNLOCK_DIR"] = tmp
    os.environ["FACE_UNLOCK_CONFIG"] = os.path.join(tmp, "config")
    os.environ["PATH"] = bindir + os.pathsep + os.environ["PATH"]

    keys_log = os.path.join(tmp, "keys.log")
    fake = os.path.join(bindir, "xdotool")
    with open(fake, "w") as fh:
        fh.write(f'''#!/bin/sh
echo "$*" >> {keys_log}
exit 0
''')
    os.chmod(fake, 0o755)

    import common
    import facelib
    import face_unlockd as fd

    common.write_default_config(force=True)
    for k, v in {"STREAK": "3", "STREAK_WINDOW_S": "10", "LOCK_SETTLE_S": "0",
                 "SCAN_WINDOW_S": "1.5", "SCAN_FPS": "200", "MAX_ATTEMPTS": "2",
                 "WAKE_POLL_S": "0.05", "SCAN_ONLY_WHEN_SCREEN_ON": "1",
                 "ANTI_SPOOF": "1", "ANTI_SPOOF_MIN_DIFF": "2.5",
                 "THRESHOLD": "0.42", "WAKE_KEYS": "space BackSpace"}.items():
        lines = [f"{k}={v}\n" if l.startswith(k + "=") else l
                 for l in open(common.config_path(), encoding="utf-8")]
        open(common.config_path(), "w", encoding="utf-8").writelines(lines)

    facelib.save_templates(common.template_path(),
                           np.random.rand(3, 128).astype(np.float32), {"enrolled_at": "x"})
    common.write_secret("S3cret-Pass")

    def calls():
        if not os.path.exists(keys_log):
            return []
        return [l.strip() for l in open(keys_log)]

    def reset():
        if os.path.exists(keys_log):
            os.unlink(keys_log)

    def build(frames, kinds, sims, lock_script=None, repeat_last=True,
              monitor=True, monitor_script=None):
        """lock_script: 每次问「锁了吗」的答案序列，超出则重复最后一个。
        monitor=True/False -> 打桩亮屏判定（默认 True = 永远当作亮屏）；
                              monitor="real" -> 用真实 _monitor_on（依赖物理屏幕状态）；
        monitor=True/False -> 打桩亮屏判定；
        monitor_script -> 按被问次数依次回答。"""
        u = fd.Unlocker(common.load_config())
        u.engine = StubEngine(frames, kinds, sims, repeat_last)
        # 默认必须打桩成「亮屏」，否则测试会跟着测试机显示器的物理状态漂移：
        # 屏幕灭了就有 9 项一起返回「熄屏」，看起来像产品坏了。要测真实路径请传 monitor="real"。
        if not (monitor == "real" and monitor_script is None):
            st = {"n": 0}
            def mon():
                if monitor_script is None:
                    return monitor
                i = st["n"]; st["n"] += 1
                return monitor_script[i] if i < len(monitor_script) else monitor_script[-1]
            u._monitor_on = mon
        state = {"n": 0}

        def still():
            i = state["n"]
            state["n"] += 1
            if lock_script is None:
                return True
            return lock_script[i] if i < len(lock_script) else lock_script[-1]
        u._still_locked = still
        return u

    print("== 1. 屏幕没锁：绝不发按键 ==")
    reset()
    u = build(make_frames(6), ["ok"] * 6, [0.9] * 6, lock_script=[False])
    out = u.handle_lock()
    check("未锁时立刻返回", out in ("已解锁", "外部已解锁"), out)
    check("零次 xdotool", calls() == [], str(calls()))
    check("未锁时连摄像头都不打开", u.engine.opened == 0, f"open_camera 调用 {u.engine.opened} 次")

    print("\n== 2. 本人 + 连续帧达标：注入密码 ==")
    reset()
    u = build(make_frames(8), ["ok"] * 8, [0.9] * 8,
              lock_script=[True] * 12 + [False])
    u._wait_unlocked = lambda timeout: True
    out = u.handle_lock()
    c = calls()
    print(f"     xdotool 序列: {c}")
    check("结果为成功", out == "成功", out)
    check("注入的正是配置里的密码", any("S3cret-Pass" in x and x.startswith("type") for x in c))
    check("注入前有 wake：space 与 BackSpace 成对（净效果为空）",
          "key --clearmodifiers space" in c and "key --clearmodifiers BackSpace" in c)
    check("注入后有提交 Return", "key --clearmodifiers Return" in c)
    check("全程没有 ctrl+/Delete 等会毁掉焦点窗口内容的键",
          not any("ctrl" in x or "Delete" in x for x in c), str(c))
    check("type 带 --clearmodifiers（避免把大小写/修饰键状态搞乱）",
          any("--clearmodifiers" in x for x in c if x.startswith("type")))

    print("\n== 3. 检不出人脸：不放行 ==")
    reset()
    u = build(make_frames(4), ["noface"] * 4, [0.0] * 4)
    out = u.handle_lock()
    check("全程无脸 -> 未解锁", out != "成功", out)
    check("全程无脸 -> 零注入", calls() == [], str(calls()))

    print("\n== 4. 画面里有两张脸：拒绝判定归属 ==")
    reset()
    u = build(make_frames(6), ["multi"] * 6, [0.9] * 6)
    out = u.handle_lock()
    check("多人画面不放行", out != "成功", out)
    check("多人画面零注入", calls() == [], str(calls()))

    print("\n== 5. 相似度不够（非本人）：不放行 ==")
    reset()
    u = build(make_frames(6), ["ok"] * 6, [0.30] * 6)
    out = u.handle_lock()
    check("低于阈值不放行", out != "成功", out)
    check("低于阈值零注入", calls() == [], str(calls()))

    print("\n== 5b. 阈值边界：0.419 不放、0.420 放 ==")
    reset()
    u = build(make_frames(6), ["ok"] * 6, [0.419] * 6)
    u._wait_unlocked = lambda timeout: True
    check("0.419 < 阈值 0.42 -> 拒绝", u.handle_lock() != "成功")
    reset()
    u = build(make_frames(6), ["ok"] * 6, [0.420] * 6)
    u._wait_unlocked = lambda timeout: True
    check("0.420 >= 阈值 0.42 -> 放行", u.handle_lock() == "成功")

    print("\n== 6. 完全静止的画面（照片贴镜头）：防静态图拦下 ==")
    reset()
    u = build(make_frames(12, distinct=False), ["ok"] * 12, [0.95] * 12)
    out = u.handle_lock()
    check("静止画面即使匹配也不放行", out != "成功", out)
    check("静止画面零注入", calls() == [], str(calls()))

    print("\n== 7. 注入前二次确认：中途解锁了就不发键 ==")
    reset()
    # 前 5 次问锁=是（够凑齐 STREAK），凑齐后 handle_lock 会再问一次 -> 给 False
    u = build(make_frames(10), ["ok"] * 10, [0.9] * 10,
              lock_script=[True] * 4 + [False])
    out = u.handle_lock()
    check("凑齐后发键前再次确认锁定，发现已解锁则不发", calls() == [], str(calls()))
    check("返回外部已解锁", out == "外部已解锁", out)

    print("\n== 8. 密码一直错：次数封顶，不死循环 ==")
    reset()
    u = build(make_frames(400, distinct=True), ["ok"] * 400, [0.9] * 400)
    u._wait_unlocked = lambda timeout: False
    t0 = time.time()
    out = u.handle_lock()
    n_type = sum(1 for x in calls() if x.startswith("type"))
    check(f"注入次数封顶在 MAX_ATTEMPTS=2", n_type <= 2, f"实际 type {n_type} 次")
    check("结果为尝试用尽/窗到期而不是卡死", out in ("尝试用尽", "扫描窗到期"), out)
    check("整体在扫描窗到期时间附近结束，没有卡死", time.time() - t0 < 12,
          f"{time.time()-t0:.1f}s")

    print("\n== 9. 摄像头打不开：接住异常 ==")
    reset()
    u = build(make_frames(4), ["ok"] * 4, [0.9] * 4)
    u.engine.camera_broken = True
    out = u.handle_lock()
    check("打不开摄像头不抛异常", isinstance(out, str), out)
    check("打不开时零注入", calls() == [], str(calls()))

    print("\n== 10. 扫描中途摄像头断流 ==")
    reset()
    u = build(make_frames(4), ["ok", "ok", "none", "none"], [0.9] * 4, repeat_last=False)
    out = u.handle_lock()
    check("读到 None 帧不会崩", isinstance(out, str), out)

    print("\n== 11. pause 立即止血 ==")
    reset()
    u = build(make_frames(30), ["ok"] * 30, [0.9] * 30)
    with open(common.disable_flag(), "w") as fh:
        fh.write(str(time.time() + 60))
    out = u.handle_lock()
    check("扫描中检测到暂停标志", out == "已暂停", out)
    check("暂停时零注入", calls() == [], str(calls()))
    os.unlink(common.disable_flag())

    print("\n== 12. 时间窗：慢速凑帧不算数 ==")
    reset()
    u = fd.Unlocker(common.load_config())
    u.engine = StubEngine(make_frames(10), ["ok"] * 10, [0.9] * 10)
    u._still_locked = lambda: True
    u._wait_unlocked = lambda timeout: True
    u.streak_window = 0.001          # 窗只有 1ms，任何两帧都超窗
    reset()
    out = u.handle_lock()
    check("超出时间窗的匹配不放行", out != "成功", out)
    check("超窗零注入", calls() == [], str(calls()))


    print("\n== 13. 熄屏时：不开摄像头，不发按键 ==")
    reset()
    u = build(make_frames(8), ["ok"] * 8, [0.9] * 8, monitor=False)
    out = u.handle_lock()
    check("熄屏直接结束本次窗", out == "熄屏", out)
    check("熄屏时没打开摄像头", u.engine.opened == 0, f"open_camera {u.engine.opened} 次")
    check("熄屏时零注入", calls() == [], str(calls()))

    print("\n== 14. 扫描中途屏幕熄灭：立即收窗 ==")
    reset()
    # 回答序列：第1次=进循环前的预检，之后每帧一次。第3次给 False，
    # 保证在凑齐 STREAK=3 之前就熄屏，而不是已经注入完了才熄。
    u = build(make_frames(60), ["ok"] * 60, [0.9] * 60,
              monitor_script=[True, True, False])
    out = u.handle_lock()
    check("中途熄屏被检出并结束", out == "熄屏", out)
    check("中途熄屏零注入", calls() == [], str(calls()))

    print("\n== 15. 读不到 DPMS 状态：必须 fail-open 照常扫 ==")
    reset()
    orig = common.monitor_power
    common.monitor_power = lambda env: (None, False)
    u = fd.Unlocker(common.load_config())
    u.engine = StubEngine(make_frames(10), ["ok"] * 10, [0.9] * 10)
    u._still_locked = lambda: True
    u._wait_unlocked = lambda timeout: True
    out = u.handle_lock()
    common.monitor_power = orig
    check("读不到状态时仍然扫描并解锁", out == "成功", out)
    check("fail-open 确实注入了", any(x.startswith("type") for x in calls()), str(calls()))

    print("\n== 16. 门控关闭（SCAN_ONLY_WHEN_SCREEN_ON=0）：熄屏也扫 ==")
    # 注意：这里必须打桩底层 common.monitor_power，不能打桩 _monitor_on ——
    # 打桩 _monitor_on 会把门控开关那段判定整体绕过，测了等于没测。
    reset()
    cp = common.config_path()
    orig_mp = common.monitor_power
    common.monitor_power = lambda env: ("off", True)      # 真实熄屏，且 DPMS 已启用
    try:
        u = fd.Unlocker(common.load_config())
        u.engine = StubEngine(make_frames(10), ["ok"] * 10, [0.9] * 10)
        u._still_locked = lambda: True
        u._wait_unlocked = lambda timeout: True
        out_on = u.handle_lock()
        check("门控开着时熄屏被挡住", out_on == "熄屏", out_on)

        body = open(cp, encoding="utf-8").read()
        open(cp, "w", encoding="utf-8").write(body.replace("SCAN_ONLY_WHEN_SCREEN_ON=1",
                                                           "SCAN_ONLY_WHEN_SCREEN_ON=0"))
        reset()
        u = fd.Unlocker(common.load_config())
        u.engine = StubEngine(make_frames(10), ["ok"] * 10, [0.9] * 10)
        u._still_locked = lambda: True
        u._wait_unlocked = lambda timeout: True
        out_off = u.handle_lock()
        check("关掉门控后同样熄屏也照常扫描", out_off == "成功", out_off)
        check("关掉门控后确实注入了", any(x.startswith("type") for x in calls()), str(calls()))
        body = open(cp, encoding="utf-8").read()
        open(cp, "w", encoding="utf-8").write(body.replace("SCAN_ONLY_WHEN_SCREEN_ON=0",
                                                           "SCAN_ONLY_WHEN_SCREEN_ON=1"))
    finally:
        common.monitor_power = orig_mp

    print("\n== 17. 熄屏 -> 亮屏的恢复延迟 ==")
    reset()
    u = build(make_frames(4), ["ok"] * 4, [0.9] * 4, monitor=False)
    t0 = time.time()
    flip = {"on_at": t0 + 0.4}
    u._monitor_on = lambda: time.time() >= flip["on_at"]
    got = u.wait_for_screen_on()
    dt = time.time() - t0
    check("wait_for_screen_on 在亮屏后立刻返回", got == "亮屏", got)
    check(f"恢复延迟 {dt*1000:.0f}ms <= 0.4s+轮询(0.05s)", dt <= 0.55, f"{dt*1000:.0f}ms")

    print(f"\n{'='*58}")
    print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
    if FAIL:
        print("失败项："); [print("   - " + x) for x in FAIL]
    print(f"临时目录 {tmp}  清理： rm -rf {tmp}")
    print(f"{'='*58}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
