"""人脸识别内核：YuNet 检测 + SFace 特征比对。

设计约束（都在本机实测得出，不是猜的）：
  * OpenCV 必须 >= 4.6。系统自带的 4.5.4 加载不了 YuNet 的任何版本导出，
    原因见 README（4.5.4 找不到模型里名为 width/height 的动态维度）。
  * 检测输入尺寸的宽高都必须能被 32 整除，否则 FPN 融合层 shape 断言失败。
    _align32() 负责把任意尺寸拉齐，不要绕过它。
  * 全链路不需要 X display，纯 headless 可用。
"""

from __future__ import annotations

import os
import time

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_DIR = os.path.join(ROOT, "models")

# SFace 官方给出的两个推荐阈值，任选其一代数量纲
THRESH_COSINE = 0.363      # >= 视为同一人
THRESH_NORMED_L2 = 1.128   # <= 视为同一人

EMB_DIM = 128


def align32(value: int) -> int:
    """向下取到 32 的倍数，且不低于 32。"""
    v = (int(value) // 32) * 32
    return max(32, v)


def align_size(w: int, h: int) -> tuple[int, int]:
    return align32(w), align32(h)


class NoFace(Exception):
    """画面里没有人脸，或人脸不合格（太小/太模糊/多人）。"""


class FaceEngine:
    """把一帧 BGR 图像变成一个可比较的 128 维人脸特征。"""

    def __init__(self,
                 det_size: tuple[int, int] = (640, 352),
                 score_threshold: float = 0.6,
                 min_face_px: int = 80,
                 camera_index: int = 0,
                 capture_size: tuple[int, int] = (1280, 720),
                 model_dir: str = MODEL_DIR):
        # 静默拉齐，避免调用方传进 720 这种尺寸直接崩
        self.det_size = align_size(*det_size)
        self.capture_size = capture_size
        self.score_threshold = score_threshold
        self.min_face_px = min_face_px
        self.camera_index = camera_index

        det_path = os.path.join(model_dir, "yunet.onnx")
        rec_path = os.path.join(model_dir, "sface.onnx")
        for p in (det_path, rec_path):
            if not os.path.exists(p):
                raise FileNotFoundError(
                    f"缺少模型文件 {p}；请先执行 install.sh 的模型下载步骤")

        self.detector = cv2.FaceDetectorYN_create(
            det_path, "", self.det_size, score_threshold, 0.3, 5000)
        self.recognizer = cv2.FaceRecognizerSF_create(rec_path, "")
        self._layout_checked = False

    # ---------- 采集 ----------

    def open_camera(self) -> cv2.VideoCapture:
        cap = cv2.VideoCapture(self.camera_index, cv2.CAP_V4L2)
        if not cap.isOpened():
            raise RuntimeError(
                f"打不开 /dev/video{self.camera_index}。"
                f"检查摄像头是否存在、当前用户有没有读取权限")
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.capture_size[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.capture_size[1])
        # 让自动曝光跑几帧，否则头几张会偏暗导致特征质量差
        for _ in range(8):
            cap.read()
        return cap

    def grab(self, cap: cv2.VideoCapture):
        """返回一帧原始尺寸图像，读不到返回 None。"""
        ok, frame = cap.read()
        return frame if ok else None

    # ---------- 检测与特征 ----------

    def _scale_face(self, face: np.ndarray, sx: float, sy: float) -> np.ndarray:
        """把缩小图上的 face 向量映射回原图坐标。

        实测列布局（本机 OpenCV 4.11 + yunet_2023mar，用 alignCrop 反查确认）：
            [0..3]  x, y, w, h
            [4..13] 5 个 landmark 的 x,y 交替：左眼 右眼 鼻尖 左嘴角 右嘴角
            [14]    score，0~1
        注意：这和 OpenCV 文档写的「score 在第 5 个」不一致。文档那种排布会让
        alignCrop 裁出来的图连人脸都检不出来，所以别照文档改这里。
        """
        f = face.astype(np.float32).copy()
        f[0] *= sx
        f[1] *= sy
        f[2] *= sx
        f[3] *= sy
        f[4:14:2] *= sx   # landmark x: 4,6,8,10,12
        f[5:14:2] *= sy   # landmark y: 5,7,9,11,13
        # f[14] 是 score，不能缩放
        return f

    @staticmethod
    def score(face: np.ndarray) -> float:
        return float(face[14])

    @staticmethod
    def landmarks(face: np.ndarray) -> np.ndarray:
        """(5,2) 的五点，顺序 左眼/右眼/鼻/左嘴角/右嘴角。"""
        return np.stack([face[4:14:2], face[5:14:2]], axis=1)

    @staticmethod
    def check_face_layout(face: np.ndarray, img_w: int, img_h: int) -> None:
        """列序如果被换错，症状是「score 不在 0~1」或「landmark 跑到框外」。
        启动时跑一次，避免以后静默降级精度。"""
        s = float(face[14])
        if not 0.0 <= s <= 1.0:
            raise RuntimeError(f"face 向量列序可疑：col14 的值 {s:.3f} 不在 0~1，"
                               f"说明 score 不在最后一列 —— 检查 _scale_face 和检测器版本")
        lm = FaceEngine.landmarks(face)
        x, y, w, h = face[0], face[1], face[2], face[3]
        if (lm[:, 0] < x - 0.5 * w).any() or (lm[:, 0] > x + 1.5 * w).any() or \
           (lm[:, 1] < y - 0.5 * h).any() or (lm[:, 1] > y + 1.5 * h).any():
            raise RuntimeError(f"landmark 落在人脸框外太多，列序可能被搞错了：{lm.round(1).tolist()}")
        if (lm[:, 0] < 0).any() or (lm[:, 1] < 0).any() or \
           (lm[:, 0] > img_w).any() or (lm[:, 1] > img_h).any():
            raise RuntimeError("landmark 超出图像边界，坐标缩放有误")


    def detect(self, frame: np.ndarray) -> list[np.ndarray]:
        """返回原图坐标系下的人脸列表，按面积从大到小。"""
        h, w = frame.shape[:2]
        dw, dh = self.det_size
        small = cv2.resize(frame, (dw, dh), interpolation=cv2.INTER_AREA)
        self.detector.setInputSize((dw, dh))
        _, faces = self.detector.detect(small)
        if faces is None or len(faces) == 0:
            return []
        sx, sy = w / dw, h / dh
        out = [self._scale_face(f, sx, sy) for f in faces]
        out.sort(key=lambda f: f[2] * f[3], reverse=True)
        if not self._layout_checked:
            # 只在第一次跑，把列序/缩放错误变成显式异常而不是静默降精度
            self.check_face_layout(out[0], w, h)
            self._layout_checked = True
        return out

    def best_face(self, frame: np.ndarray, require_single: bool = True) -> np.ndarray:
        """取质量最好的一张脸；不合格就抛 NoFace，让调用方决定怎么提示。"""
        faces = self.detect(frame)
        if not faces:
            raise NoFace("画面里没有人脸")
        if require_single and len(faces) > 1:
            raise NoFace(f"画面里有 {len(faces)} 张脸，无法判定解锁给谁")
        f = faces[0]
        if min(f[2], f[3]) < self.min_face_px:
            raise NoFace(f"人脸太小（{int(f[2])}x{int(f[3])}px），请靠近一点")
        return f

    def embedding(self, frame: np.ndarray, face: np.ndarray) -> np.ndarray:
        """对齐裁剪 -> 128 维单位化特征。"""
        crop = self.recognizer.alignCrop(frame, face)
        emb = self.recognizer.feature(crop).flatten().astype(np.float32)
        if emb.shape[0] != EMB_DIM:
            raise RuntimeError(f"特征维度异常：期望 {EMB_DIM}，实际 {emb.shape[0]}")
        return emb

    def embed(self, frame: np.ndarray, require_single: bool = True) -> np.ndarray:
        return self.embedding(frame, self.best_face(frame, require_single))

    # ---------- 比对 ----------

    def similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        """余弦相似度，越大越像。输入可以是 (128,) 或 (1,128)。"""
        a = np.asarray(a, dtype=np.float32).reshape(1, EMB_DIM)
        b = np.asarray(b, dtype=np.float32).reshape(1, EMB_DIM)
        return float(self.recognizer.match(a, b, cv2.FaceRecognizerSF_FR_COSINE))

    def max_similarity(self, probe: np.ndarray, templates: np.ndarray) -> float:
        """probe 与一组模板里最像的那一个的相似度。"""
        if len(templates) == 0:
            return -1.0
        return max(self.similarity(probe, t) for t in templates)

    # ---------- 防静态图 ----------

    @staticmethod
    def frame_diff(a: np.ndarray, b: np.ndarray) -> float:
        """两张裁剪脸的平均绝对差，用来筛掉完全静止的画面（比如贴着不动的照片）。"""
        ga = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY).astype(np.float32)
        gb = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY).astype(np.float32)
        if ga.shape != gb.shape:
            gb = cv2.resize(gb, (ga.shape[1], ga.shape[0]))
        return float(np.mean(np.abs(ga - gb)))


def load_templates(path: str) -> tuple[np.ndarray, dict]:
    """读模板文件。返回 (N,128) 数组和元信息 dict。"""
    if not os.path.exists(path):
        return np.empty((0, EMB_DIM), dtype=np.float32), {}
    with np.load(path, allow_pickle=False) as z:
        embs = np.asarray(z["embeddings"], dtype=np.float32)
        meta = {k: str(z[k]) for k in z.files if k != "embeddings"}
    return embs, meta


def save_templates(path: str, embeddings: np.ndarray, meta: dict) -> None:
    """原子写 + 0600。模板就是人脸骨架，泄露等于可被用来合成解锁图像。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        np.savez(fh, embeddings=np.asarray(embeddings, dtype=np.float32), **meta)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
