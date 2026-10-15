#!/usr/bin/env python3
"""人脸录入：face-enroll [--count 12] [--append] [--clear] [--list]

注意 venv 里装的是 opencv-python-**headless**，没有 GUI，所以这里用终端引导 +
事后留一张预览图的方式，效果一样，且远程 SSH 也能用。
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
import facelib


def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def cmd_list() -> int:
    embs, meta = facelib.load_templates(common.template_path())
    if len(embs) == 0:
        print("还没录入任何人脸。运行 face-enroll")
        return 1
    print(f"模板文件: {common.template_path()}")
    print(f"特征条数: {len(embs)}")
    for k in ("enrolled_at", "appended_at", "camera", "detector_size", "threshold_at_enroll"):
        if k in meta:
            print(f"  {k}: {meta[k]}")
    return 0


def cmd_clear() -> int:
    p = common.template_path()
    if os.path.exists(p):
        os.replace(p, p + f".bak-{int(time.time())}")
        print(f"已清空（原文件备份为 {os.path.basename(p)}.bak-*）")
    else:
        print("本来就没有模板")
    return 0


def enroll(engine: facelib.FaceEngine, count: int, append: bool,
           diversity: float, show_debug: bool) -> int:
    embs_existing, _ = facelib.load_templates(common.template_path())
    collected: list[np.ndarray] = []
    crops: list[np.ndarray] = []

    print(f"目标：采集 {count} 张有效人脸。")
    print("请正对摄像头，保持脸部在画面中央、约占画面一半高。")
    print("环境光线要能照到正脸，别背光。")
    print()
    for i, tip in enumerate(["① 正面不动 2 秒", "② 头缓缓左转约 20°",
                             "③ 头缓缓右转约 20°", "④ 微微抬头再低头",
                             "⑤ 自然表情来回微调"], start=0):
        print(f"   提示{i+1}. {tip}")
    print()

    cap = engine.open_camera()
    last_hint = 0.0
    reject_streak = 0
    try:
        while len(collected) < count:
            frame = engine.grab(cap)
            if frame is None:
                print("读帧失败，摄像头可能被别的程序占用了")
                return 2
            try:
                face = engine.best_face(frame)
                crop = engine.recognizer.alignCrop(frame, face)
                emb = engine.embedding(frame, face)
                # 姿态多样性：和已采集的太像就不要，逼着用户动一动
                worst = min((engine.similarity(emb, e) for e in collected), default=0.0)
                if collected and worst > 1.0 - diversity:
                    raise facelib.NoFace(f"姿态和已有样本重复（相似 {worst:.2f}），请换个角度")
            except facelib.NoFace as exc:
                if time.time() - last_hint > 0.8:
                    print(f"   · {exc}")
                    last_hint = time.time()
                    reject_streak += 1
                    if reject_streak > 400:
                        print("连续失败太多次，中止。请检查光线和位置。")
                        return 3
                continue
            reject_streak = 0

            collected.append(emb)
            crops.append(cv2.resize(crop, (112, 112)))
            have = len(collected)
            bar = "#" * have + "." * max(0, count - have)
            print(f"\r   [{bar}] {have}/{count}  已采集          ", end="", flush=True)
            if show_debug:
                cv2.imwrite(f"/tmp/face-enroll-{have}.jpg", frame)

        print()
    finally:
        cap.release()

    new = np.stack(collected).astype(np.float32)
    final = np.concatenate([embs_existing, new], axis=0) if append else new

    meta = {
        "enrolled_at": now_stamp() if not append else str(meta_prev(append)),
        "camera": f"/dev/video{common.cfg_int(common.load_config(), 'CAMERA_INDEX')}",
        "detector_size": "x".join(map(str, engine.det_size)),
        "threshold_at_enroll": common.load_config().get("THRESHOLD", ""),
        "count": str(len(final)),
    }
    if append:
        meta["appended_at"] = now_stamp()
    facelib.save_templates(common.template_path(), final, meta)

    prev = os.path.join(common.data_dir(), "enroll_preview.jpg")
    if crops:
        cols = min(6, len(crops))
        rows = int(np.ceil(len(crops) / cols))
        W = H = 112
        sheet = np.full((rows * H, cols * W, 3), 30, np.uint8)
        for i, c in enumerate(crops):
            r, cc = divmod(i, cols)
            sheet[r * H:(r + 1) * H, cc * W:(cc + 1) * W] = c
        cv2.imwrite(prev, sheet, [cv2.IMWRITE_JPEG_QUALITY, 92])
        os.chmod(prev, 0o600)

    print(f"\n录入完成：共 {len(final)} 条特征（本次新增 {len(new)}）")
    print(f"模板：{common.template_path()} (0600)")
    print(f"预览：{prev}  <- 打开确认抓到的是你的正脸")
    print("下一步：face-ctl test 验证通过率，再 face-ctl install-service")
    return 0


def meta_prev(append: bool) -> str:
    _, meta = facelib.load_templates(common.template_path())
    return meta.get("enrolled_at", "unknown")


def main() -> int:
    ap = argparse.ArgumentParser(description="人脸录入")
    ap.add_argument("--count", type=int, default=12, help="采集多少张有效人脸（默认 12）")
    ap.add_argument("--append", action="store_true", help="追加到已有模板，不覆盖")
    ap.add_argument("--clear", action="store_true", help="清空模板")
    ap.add_argument("--list", action="store_true", help="查看已录入情况")
    ap.add_argument("--diversity", type=float, default=0.12,
                    help="样本间最小差异（相似度上限=1-diversity），默认 0.12")
    ap.add_argument("--debug", action="store_true", help="每张原始帧另存 /tmp")
    args = ap.parse_args()

    if args.clear:
        return cmd_clear()
    if args.list:
        return cmd_list()

    cfg = common.load_config()
    engine = facelib.FaceEngine(
        det_size=common.cfg_size(cfg, "DET_SIZE"),
        score_threshold=common.cfg_float(cfg, "DET_SCORE"),
        min_face_px=common.cfg_int(cfg, "MIN_FACE_PX"),
        camera_index=common.cfg_int(cfg, "CAMERA_INDEX"),
        capture_size=common.cfg_size(cfg, "CAPTURE_SIZE"),
    )
    print(f"检测输入尺寸已被拉齐为 {engine.det_size}（必须 32 的倍数）")
    return enroll(engine, args.count, args.append, args.diversity, args.debug)


if __name__ == "__main__":
    raise SystemExit(main())
