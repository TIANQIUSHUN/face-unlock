#!/usr/bin/env python3
"""解锁流程测试。

用假的 xdotool 打桩（真装 xdotool 之前也能跑），验证 handle_lock 的全部判定分支：
  * 屏幕没锁时，一个按键都不许发出去
  * 本人 + 连续帧达标 -> 注入密码，且注入前确认过仍处于锁定
  * 画面完全静止 -> 防静态图拦下，不注入
  * 非本人 -> 不注入，超时收场
  * 一直解不开 -> 注入次数封顶，不死循环
  * 摄像头被占用 -> 不崩

真实人脸帧从当前摄像头抓；抓不到就明确报错而不是假装通过。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'通过' if cond else '失败'}] {name}  {detail}")


def main() -> int:
    import numpy as np
    import cv2

    tmp = tempfile.mkdtemp(prefix="face-flow-")
    bin_dir = os.path.join(tmp, "bin"); os.makedirs(bin_dir, exist_ok=True)
    os.environ["FACE_UNLOCK_DIR"] = tmp
    os.environ["FACE_UNLOCK_CONFIG"] = os.path.join(tmp, "flowconfig")
    os.environ["PATH"] = bin_dir + os.pathsep + os.environ["PATH"]

    keys_log = os.path.join(tmp, "keys.log")
    unlocked_file = os.path.join(tmp, "unlocked")

    # ---- 假 xdotool：记录调用；收到 Return 就模拟"锁屏解开了"
    fake = os.path.join(bin_dir, "xdotool")
    with open(fake, "w") as fh:
        fh.write(f"""#!/bin/sh
echo "$@" >> {keys_log}
for a in "$@"; do
  if [ "$a" = "Return" ]; then
     case "$(echo "$*" )" in
       *type*) : ;;                       # type 参数里含密码，不当作提交
       *) touch {unlocked_file} ;;
     esac
  fi
done
exit 0
""")
    os.chmod(fake, 0o755)

    import common
    import facelib

    # 测试用的快配置
    common.write_default_config(force=True)
    for k, v in {"STREAK": "3", "STREAK_WINDOW_S": "5", "LOCK_SETTLE_S": "0",
                 "SCAN_WINDOW_S": "6", "WAKE_POLL_S": "0.05", "SCAN_FPS": "20", "MAX_ATTEMPTS": "2",
                 "BACKOFF_S": "0", "ANTI_SPOOF_MIN_DIFF": "2.5",
                 "MIN_FACE_PX": "40"}.items():
        lines = [f"{k}={v}\n" if l.startswith(k + "=") else l
                 for l in open(common.config_path(), encoding="utf-8")]
        open(common.config_path(), "w", encoding="utf-8").writelines(lines)
    cfg = common.load_config()

    def reset():
        for f in (keys_log, unlocked_file):
            if os.path.exists(f):
                os.unlink(f)

    def typed():
        if not os.path.exists(keys_log):
            return []
        return [l.strip() for l in open(keys_log)]

    # ---- 抓真实人脸帧
    print("== 准备：抓当前摄像头里的真实人脸帧 ==")
    e = facelib.FaceEngine(det_size=common.cfg_size(cfg, "DET_SIZE"),
                           min_face_px=40,
                           camera_index=common.cfg_int(cfg, "CAMERA_INDEX"))
    cap = e.open_camera()
    frames = []
    for _ in range(120):
        fr = e.grab(cap)
        if fr is None:
            continue
        try:
            f = e.best_face(fr, require_single=True)
        except facelib.NoFace:
            continue
        frames.append((fr, f))
        if len(frames) >= 10:
            break
    cap.release()
    if len(frames) < 5:
        print(f"\n抓到 {len(frames)} 帧有效人脸，不足以测流程。")
        print("请正对摄像头坐好（脸占画面一半高）后重跑： .venv/bin/python tests/flow_test.py")
        return 1
    embs = [e.embedding(fr, f) for fr, f in frames]
    print(f"  抓到 {len(frames)} 帧有效人脸，特征 {embs[0].shape}")

    tpl = np.stack(embs[:4]).astype(np.float32)
    facelib.save_templates(common.template_path(), tpl, {"enrolled_at": "flowtest"})
    common.write_secret("Test-Passw0rd!")
    print(f"  模板 {len(tpl)} 条 + 测试密码已写入 {tmp}")

    # ---- 打桩：锁状态改成读文件，摄像头帧改成预置列表
    import face_unlockd as fd

    def patched_locked(_self):
        return not os.path.exists(unlocked_file)

    fd.Unlocker._still_locked = patched_locked

    def feed(engine_self, cap_self, seq):
        i = [0]
        def g(_c):
            fr = seq[i[0] % len(seq)][0]
            i[0] += 1
            return fr
        return g

    def make_unlocker():
        u = fd.Unlocker(common.load_config())
        return u

    print("\n== 分支 1：屏幕没锁，绝不发按键 ==")
    reset(); open(unlocked_file, "w").close()      # 视为未锁
    u = make_unlocker()
    outcome = u.handle_lock()
    check("未锁时直接返回，不打开扫描", outcome in ("已解锁", "外部已解锁"), outcome)
    check("未锁时零次 xdotool 调用", typed() == [], f"{len(typed())} 次")

    print("\n== 分支 2：本人 -> 连续帧达标 -> 注入密码 ==")
    reset(); unlocked_file and os.path.exists(unlocked_file) and os.unlink(unlocked_file)
    u = make_unlocker()
    seq = frames[4:]
    u.engine.grab = lambda c, s=seq: feed(e, None, s)(None)   # type: ignore
    outcome = u.handle_lock()
    calls = typed()
    print(f"     xdotool 调用序列：{calls}")
    check("本次处理结果为成功", outcome == "成功", outcome)
    check("确实调用过 type 注入", any(c.startswith("type") for c in calls))
    check("type 的参数就是配置的密码",
          any("Test-Passw0rd!" in c for c in calls), )
    check("注入前先 wake（space/BackSpace，净效果为空）",
          any("space" in c for c in calls) and any("BackSpace" in c for c in calls))
    check("注入后有提交（key --clearmodifiers Return）",
          any(c == "key --clearmodifiers Return" for c in calls), )
    check("没有出现 ctrl+a / Delete 这类会毁掉焦点窗口内容的键",
          not any("ctrl" in c or "Delete" in c for c in calls), str(calls))
    check("解锁后 fake 锁屏确实被解开", os.path.exists(unlocked_file))

    print("\n== 分支 3：完全静止的画面（照片贴镜头）==")
    reset()
    u = make_unlocker()
    static = [frames[4]]
    u.engine.grab = lambda c, s=static: feed(e, None, s)(None)   # type: ignore
    outcome = u.handle_lock()
    check("静止画面被防静态图拦下，未注入", outcome != "成功" and typed() == [],
          f"{outcome} / xdotool 调用 {len(typed())} 次")

    print("\n== 分支 4：非本人 ==")
    reset()
    facelib.save_templates(common.template_path(),
                           np.random.rand(4, 128).astype(np.float32), {"enrolled_at": "随机"})
    u = make_unlocker()
    u.engine.grab = lambda c, s=frames[4:]: feed(e, None, s[4:] if len(s) > 4 else s)(None)  # type: ignore
    outcome = u.handle_lock()
    check("非本人不会被放行", outcome != "成功", outcome)
    check("非本人时零次注入", typed() == [], f"{len(typed())} 次")

    print("\n== 分支 5：注入用尽也不解开 -> 次数封顶 ==")
    reset()
    facelib.save_templates(common.template_path(), tpl, {"enrolled_at": "flowtest"})
    u = make_unlocker()
    u.engine.grab = lambda c, s=frames[4:]: feed(e, None, s)(None)   # type: ignore
    # 让 fake xdotool 不置为已解锁：删掉 unlocked 文件但阻止它被创建
    os.chmod(tmp, 0o755)
    real_locked = fd.Unlocker._still_locked
    fd.Unlocker._wait_unlocked = lambda self, timeout: False
    outcome = u.handle_lock()
    n_type = sum(1 for c in typed() if c.startswith("type"))
    check("注入次数不超过 MAX_ATTEMPTS", n_type <= 2, f"type 调用 {n_type} 次，上限 2")
    check("用尽后收场而不是死循环", outcome in ("尝试用尽", "扫描窗到期"), outcome)
    fd.Unlocker._wait_unlocked = real_locked

    print("\n== 分支 6：摄像头打不开 ==")
    reset()
    u = make_unlocker()
    def boom():
        raise RuntimeError("/dev/video0 被别的程序占用")
    u.engine.open_camera = boom
    outcome = u.handle_lock()
    check("摄像头异常被接住，不崩", isinstance(outcome, str), outcome)

    print("\n== 分支 7：扫描途中被 pause ==")
    reset()
    fd.Unlocker._still_locked = patched_locked
    u = make_unlocker()
    u.engine.grab = lambda c, s=frames[4:]: feed(e, None, s)(None)   # type: ignore
    with open(common.disable_flag(), "w") as fh:
        fh.write(str(time.time() + 60))
    outcome = u.handle_lock()
    check("扫描中 face-ctl pause 能立即止血", outcome == "已暂停", outcome)
    check("暂停期间零注入", typed() == [], f"{len(typed())} 次")

    print(f"\n{'='*58}\n通过 {len(PASS)}  失败 {len(FAIL)}")
    if FAIL:
        print("失败项："); [print("   - " + x) for x in FAIL]
    print(f"临时目录：{tmp}\n清理： rm -rf {tmp}")
    print(f"{'='*58}")
    print("说明：这里跑的是假 xdotool。真机注入是否能把 GNOME 锁屏打开，")
    print("      必须装上 xdotool 后 face-ctl lock 实测一次。")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
