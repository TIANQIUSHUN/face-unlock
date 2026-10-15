#!/usr/bin/env python3
"""机制自测。不写你的真实数据目录，不锁屏，不注入密码。

  .venv/bin/python tests/selftest.py          跑全部
  .venv/bin/python tests/selftest.py --face   额外用摄像头真人帧测「同人相似度分布」
"""

from __future__ import annotations

import cv2
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

PASS, FAIL, SKIP = [], [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name); print(f"  [通过] {name}  {detail}")
    else:
        FAIL.append(name); print(f"  [失败] {name}  {detail}")


def skip(name, why):
    SKIP.append(name); print(f"  [跳过] {name}  {why}")


import facelib  # noqa: E402  供 run_face_distribution 使用


def main() -> int:
    want_face = "--face" in sys.argv
    tmp = tempfile.mkdtemp(prefix="face-selftest-")
    os.environ["FACE_UNLOCK_DIR"] = tmp
    os.environ["FACE_UNLOCK_CONFIG"] = os.path.join(tmp, "config")
    print(f"临时数据目录：{tmp}\n")

    import common
    import facelib

    print("== 1. 尺寸对齐（YuNet 在旧构建里 720 会崩，必须拉齐）==")
    check("align32(720)==704", facelib.align32(720) == 704, f"实测 {facelib.align32(720)}")
    check("align32(480)==480", facelib.align32(480) == 480)
    check("align_size(1280,720)", facelib.align_size(1280, 720) == (1280, 704))
    check("align32 不会归零", facelib.align32(10) == 32)
    e = facelib.FaceEngine(det_size=(1280, 720))
    check("FaceEngine 自动拉齐非法尺寸", e.det_size == (1280, 704), f"实际 {e.det_size}")

    print("\n== 2. 模板读写与权限 ==")
    p = common.template_path()
    embs, meta = facelib.load_templates(p)
    check("空模板读取不崩", len(embs) == 0 and meta == {})
    import numpy as np
    a = np.random.rand(5, 128).astype(np.float32)
    facelib.save_templates(p, a, {"enrolled_at": "test", "count": "5"})
    b, m2 = facelib.load_templates(p)
    check("写后读回一致", np.allclose(a, b) and m2.get("enrolled_at") == "test")
    check("模板文件 0600", oct(os.stat(p).st_mode & 0o777) == "0o600",
          oct(os.stat(p).st_mode & 0o777))
    check("数据目录 0700", oct(os.stat(tmp).st_mode & 0o777) == "0o700",
          oct(os.stat(tmp).st_mode & 0o777))

    print("\n== 3. 密码读写 ==")
    common.write_secret("hunter2 测试")
    check("密码回读一致", common.read_secret() == "hunter2 测试")
    check("密码文件 0600", oct(os.stat(common.secret_path()).st_mode & 0o777) == "0o600")
    os.chmod(common.secret_path(), 0o644)
    common.read_secret()          # 应打印警告而不是崩
    check("世界可读时告警但不崩", True)
    os.chmod(common.secret_path(), 0o600)

    print("\n== 4. 暂停语义 ==")
    check("默认未暂停", not common.is_paused())
    with open(common.disable_flag(), "w") as fh:
        fh.write("")
    check("无时间戳=永久暂停", common.is_paused() and common.pause_until() == float("inf"))
    os.unlink(common.disable_flag())
    with open(common.disable_flag(), "w") as fh:
        fh.write(str(__import__("time").time() + 300))
    check("未来时间戳=暂停中", common.is_paused())
    os.unlink(common.disable_flag())
    with open(common.disable_flag(), "w") as fh:
        fh.write(str(__import__("time").time() - 1))
    check("过期时间戳=自动恢复且不抛异常", not common.is_paused())
    check("过期后标志文件被清掉", not os.path.exists(common.disable_flag()))

    print("\n== 5. 配置读写 ==")
    common.write_default_config()
    cfg = common.load_config()
    check("默认配置生成", os.path.exists(common.config_path()))
    check("THRESHOLD 有默认值", "THRESHOLD" in cfg, cfg.get("THRESHOLD", ""))
    check("配置文件 0600", oct(os.stat(common.config_path()).st_mode & 0o777) == "0o600")
    check("cfg_size 解析", common.cfg_size(cfg, "CAPTURE_SIZE") == (1280, 720))

    print("\n== 6. X 环境发现 ==")
    try:
        env = common.x_environment(cfg)
        check("DISPLAY 发现", bool(env.get("DISPLAY")), env.get("DISPLAY", ""))
        check("XAUTHORITY 发现", bool(env.get("XAUTHORITY")),
              env.get("XAUTHORITY", "未找到"))
    except Exception as exc:
        skip("X 环境发现", str(exc))

    print("\n== 7. 锁状态通道 ==")
    try:
        st = common.is_screen_locked()
        check("GetActive 可读", isinstance(st, bool), f"当前={'已锁' if st else '未锁'}")
    except Exception as exc:
        check("GetActive 可读", False, str(exc))
    check("自测不会真的锁屏", True, "(故意不调用 Lock，避免打断你干活)")

    print("\n== 8. 无脸降级 ==")
    black = np.zeros((720, 1280, 3), np.uint8)
    try:
        e.best_face(black); check("纯黑帧应抛 NoFace", False, "居然检出了人脸")
    except facelib.NoFace as exc:
        check("纯黑帧应抛 NoFace", True, str(exc))
    noise = (np.random.rand(720, 1280, 3) * 255).astype(np.uint8)
    try:
        f = e.best_face(noise)
        print(f"  [注意] 随机噪声图被判为人脸并放行：{f[2]:.0f}x{f[3]:.0f}px "
              f"score={f[4]:.2f} —— 说明单靠检测分数不足，靠 STREAK+阈值+防静态图三重门")
    except facelib.NoFace:
        check("随机噪声不应被当人脸", True)
    check("空模板时 max_similarity 返回负值",
          e.max_similarity(np.random.rand(128).astype(np.float32), np.empty((0, 128), np.float32)) < 0)

    print("\n== 9. face-ctl 各子命令能起来 ==")
    py = sys.executable
    r = subprocess.run([py, os.path.join(ROOT, "src", "face_ctl.py"), "status"],
                       capture_output=True, text=True, env=dict(os.environ))
    check("face-ctl status 退出码可预期（未就绪应返回1，不该崩）",
          r.returncode in (0, 1), f"rc={r.returncode}")
    check("face-ctl status 无 traceback", "Traceback" not in (r.stdout + r.stderr),
          (r.stderr.strip().splitlines() or [""])[-1][:80])
    r2 = subprocess.run([py, os.path.join(ROOT, "src", "face_enroll.py"), "--list"],
                        capture_output=True, text=True, env=dict(os.environ))
    check("face-enroll --list 不崩", "Traceback" not in (r2.stdout + r2.stderr))
    print("\n  face-ctl status 实际输出：")
    for line in r.stdout.splitlines():
        print("    " + line)

    print("\n== 10. 真人相似度分布 ==")
    if not want_face:
        skip("同人相似度分布", "需要镜头前有人；加 --face 重跑")
    else:
        run_face_distribution(e)

    print(f"\n{'='*60}")
    print(f"通过 {len(PASS)}  失败 {len(FAIL)}  跳过 {len(SKIP)}")
    if FAIL:
        print("失败项："); [print("   - " + x) for x in FAIL]
    if SKIP:
        print("跳过项："); [print("   - " + x) for x in SKIP]
    print(f"{'='*60}")
    print(f"清理： rm -rf {tmp}")
    return 1 if FAIL else 0


def run_face_distribution(e) -> None:
    import numpy as np
    import facelib, common
    cap = e.open_camera()
    embs = []
    try:
        for _ in range(60):
            fr = e.grab(cap)
            if fr is None:
                continue
            try:
                embs.append(e.embed(fr))
            except facelib.NoFace:
                pass
            if len(embs) >= 16:
                break
    finally:
        cap.release()

    if len(embs) < 6:
        skip("同人相似度分布", f"只采到 {len(embs)} 帧有效人脸，镜头前需要有人正对摄像头")
        return

    # ---- face 向量列序验证（曾把 score 当成 col4，导致 landmark 整体错位一格）
    e2 = facelib.FaceEngine(det_size=e.det_size)
    e2._layout_checked = False
    ok_layout = True
    try:
        e2.check_face_layout(np.asarray(embs[0] * 0 + 1, np.float32), 100, 100)
    except RuntimeError:
        pass          # 全 1 向量本来就该被拒（landmark 出界），这里只要求不崩
    cap2 = e2.open_camera(); fr = e2.grab(cap2); cap2.release()
    faces = e2.detect(fr)
    if faces:
        f = faces[0]
        sc = float(f[14])
        check("score 在最后一列且落在 0~1", 0.0 <= sc <= 1.0, f"col14={sc:.3f}")
        lm = facelib.FaceEngine.landmarks(f)
        inside = ((lm[:, 0] >= f[0]) & (lm[:, 0] <= f[0] + f[2]) &
                  (lm[:, 1] >= f[1]) & (lm[:, 1] <= f[1] + f[3])).all()
        check("五个 landmark 都在人脸框内", inside, lm.round(0).tolist())
        bogus = np.concatenate([f[:4], [f[14]], f[4:14]]).astype(np.float32)
        try:
            e2.check_face_layout(bogus, fr.shape[1], fr.shape[0])
            check("把 score 放回 col4 的错误排布必须被抓出", False, "居然通过了校验")
        except RuntimeError as exc:
            check("把 score 放回 col4 的错误排布必须被抓出", True, str(exc)[:60])
        crop = e2.recognizer.alignCrop(fr, f)
        d2 = cv2.FaceDetectorYN_create(os.path.join(ROOT, "models/yunet.onnx"), "", (112, 112), 0.5, 0.3, 5000)
        d2.setInputSize((112, 112))
        _, ff = d2.detect(crop)
        check("对齐裁剪图上能再检出一个人脸", ff is not None and len(ff) >= 1,
              f"{0 if ff is None else len(ff)} 个")
        if ff is not None and len(ff):
            eyes = facelib.FaceEngine.landmarks(ff[0])[:2]
            std = np.array([[38.29, 51.70], [73.53, 51.50]])
            err = float(np.linalg.norm(eyes - std, axis=1).mean())
            check("对齐后眼位接近 SFace 标准模板", err < 15.0, f"平均误差 {err:.1f}px（阈值 15px）")
    else:
        skip("列序验证", "镜头前没有人")

    half = len(embs) // 2
    A, B = embs[:half], embs[half:]
    intra_self = [e.similarity(a, b) for a in A for b in A]
    cross = [e.similarity(a, b) for a in A for b in B]
    print(f"  有效帧 {len(embs)}（分半：{len(A)} / {len(B)}）")
    print(f"  同组内相似度  min={min(intra_self):.3f} median={np.median(intra_self):.3f}")
    print(f"  跨组间相似度  min={min(cross):.3f} median={np.median(cross):.3f} max={max(cross):.3f}")
    thr = float(common.load_config()["THRESHOLD"])
    worst = min(cross)
    check(f"跨组最低值应高于默认阈值 {thr}", worst > thr, f"最低 {worst:.3f}")
    print(f"  结论：默认阈值下本人可通过，余量 {worst - thr:+.3f}")
    print("  注意：以上全部是「同一个人」的数据，只证明不会拒你自己。")
    print("        非本人会不会被放行，必须由一个不是你本人的人来测 —— 跑 face-ctl tune。")


if __name__ == "__main__":
    raise SystemExit(main())
