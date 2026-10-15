"""桌面 GUI 与命令行共用的操作层。

放进这个文件的能力都满足两个条件：① GUI 需要；② CLI 也需要（或迟早需要）。
GUI 里不写业务逻辑，只做控件与渲染，避免出现两套会各自漂移的判断。

会话类型判定是新增的关键一环：Wayland 下 xdotool/XTEST 注入按键和 xset 读 DPMS
都不成立，解锁功能必须显式拒绝，而不是装上后静默不工作。
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
import facelib

PKG_NAME = "face-unlock"
UNIT = "face-unlockd.service"
LOCK_UNIT = "face-lock-at-boot.service"
POLKIT_ACTION = "com.qoder.faceunlock.setautologin"

# 界面上允许改的参数：键 -> (中文名, 说明, 类型, 取值范围)
PARAM_FIELDS: dict[str, tuple[str, str, str, tuple[float, float]]] = {
    "THRESHOLD": ("识别严格度", "余弦相似度门槛。调低更容易拒你自己，调高更容易放行别人",
                  "float", (0.20, 0.90)),
    "STREAK": ("连续确认帧数", "连续多少帧匹配才放行。越大越稳但越慢", "int", (1, 20)),
    "STREAK_WINDOW_S": ("确认时间窗(秒)", "这些帧必须落在此窗口内，防止慢慢凑够", "float", (0.5, 10)),
    "MIN_FACE_PX": ("最小人脸像素", "人脸框短边最小像素；调小允许坐更远，但精度下降", "int", (40, 400)),
    "SCAN_WINDOW_S": ("单窗扫描上限(秒)", "连续扫描多久重开一个窗；到期不算失败", "int", (10, 7200)),
    "BACKOFF_S": ("错密后冷却(秒)", "只在注入过密码却没解开时才罚的这一段时间", "int", (0, 600)),
    "MAX_ATTEMPTS": ("单窗最大注入次数", "一个窗内最多替你敲几次密码", "int", (1, 10)),
    "LOCK_SETTLE_S": ("开窗前等待(秒)", "等锁屏/亮屏动画稳定再开始扫", "float", (0.0, 5.0)),
    "WAKE_POLL_S": ("亮屏轮询间隔(秒)", "熄屏期间查亮屏的间隔，即鼠标动完到开始扫的延迟", "float", (0.05, 5)),
    "SCAN_FPS": ("扫描帧率", "越高越跟手，越吃 CPU", "int", (2, 30)),
}

BOOL_FIELDS: dict[str, tuple[str, str]] = {
    "ENABLED": ("启用人脸解锁", "关掉后不再扫摄像头；屏幕仍会照锁，只能靠密码"),
    "SCAN_ONLY_WHEN_SCREEN_ON": ("熄屏时暂停扫描", "开着更省摄像头和 CPU；关掉则锁屏期间不分亮灭一直扫"),
    "ANTI_SPOOF": ("要求画面有变化", "挡完全静止的照片贴镜头；挡不住视频和晃动的照片"),
}

def run(args: list[str], timeout: float = 10.0, env: dict | None = None) -> tuple[int, str]:
    """统一的外部命令封装：拿返回码和合并后的输出，绝不抛。"""
    try:
        p = subprocess.run(args, capture_output=True, text=True,
                           timeout=timeout, env=env or dict(os.environ))
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, f"找不到命令 {args[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"{args[0]} 超时"
    except Exception as exc:
        return 1, f"{args[0]} 调用失败：{exc}"


# ---------------------------------------------------------------- 会话类型

def session_type() -> tuple[str, str]:
    """返回 (类型, 依据)。类型 x11 / wayland / unknown。

    顺序：环境变量 -> loginctl 会话 -> socket 存在性兜底。
    用户级 systemd 服务里能读到 systemd --user 环境（实测含 XDG_SESSION_TYPE=x11），
    所以第一条在守护进程上下文也成立。
    """
    st = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
    if st in ("x11", "wayland"):
        return st, "环境变量 XDG_SESSION_TYPE"

    if shutil.which("loginctl"):
        uid = common.uid()
        rc, out = run(["loginctl", "list-sessions", "--no-legend"], timeout=5)
        if rc == 0:
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[1] == str(uid):
                    rc2, out2 = run(["loginctl", "show-session", parts[0],
                                     "-p", "Type", "--value"], timeout=5)
                    t = out2.strip().lower()
                    if t in ("x11", "wayland"):
                        return t, f"loginctl 会话 {parts[0]}"

    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland", "WAYLAND_DISPLAY 存在"
    if glob.glob(f"/run/user/{common.uid()}/wayland-*"):
        return "wayland", "运行时目录里有 wayland socket"
    if glob.glob("/tmp/.X11-unix/X*"):
        return "x11", "X11 socket 存在"
    return "unknown", "无法判定"


def unlock_supported() -> tuple[bool, str]:
    """解锁功能在当前会话下是否成立。

    Wayland 的失败不是「实现得不好」而是设计使然：合成器不允许别的进程注入按键，
    所以这里拒绝并说明，而不是装上后静默不工作。
    """
    t, why = session_type()
    if t == "x11":
        return True, f"X11 会话（{why}），按键注入与 DPMS 读取可用"
    if t == "wayland":
        return False, (f"当前是 Wayland 会话（{why}）。GNOME 在 Wayland 下的锁屏不接受其它进程"
                       f"注入按键，xset 也读不到显示器电源状态，自动解锁无法工作。"
                       f"注销后在登录页右下角齿轮选「Ubuntu on Xorg」再登录即可使用。")
    return False, f"无法判定会话类型（{why}）。请在桌面终端里运行本程序。"


# ---------------------------------------------------------------- 服务

def systemctl_user(*args: str) -> tuple[int, str]:
    return run(["systemctl", "--user", *args], timeout=25)


def service_state() -> dict:
    rc, cur = systemctl_user("is-active", UNIT)
    rc2, en = systemctl_user("is-enabled", UNIT, LOCK_UNIT)
    return {
        "active": cur.strip() or "unknown",
        "enabled": en.strip().replace("\n", " / ") or "unknown",
        "supported": unlock_supported()[0],
    }


def enable_service(start: bool = True) -> tuple[bool, str]:
    args = ["enable", UNIT, LOCK_UNIT] + (["--now"] if start else [])
    rc, out = systemctl_user(*args)
    return rc == 0, out or f"exit={rc}"


def restart_service() -> tuple[bool, str]:
    rc, out = systemctl_user("daemon-reload")
    rc2, out2 = systemctl_user("restart", UNIT)
    return rc2 == 0, "\n".join(x for x in (out, out2) if x)


def stop_service() -> tuple[bool, str]:
    rc, out = systemctl_user("stop", UNIT, LOCK_UNIT)
    return rc == 0, out or f"exit={rc}"


# ---------------------------------------------------------------- 参数

def get_params() -> dict[str, str]:
    return common.load_config()


def set_params(values: dict[str, str]) -> tuple[bool, str]:
    """校验后写配置。非法值直接拒绝而不是夹带写进去——静默写坏一个参数比报错难查得多。"""
    path = common.config_path()
    if not os.path.exists(path):
        common.write_default_config()
    errs = []
    clean: dict[str, str] = {}
    for k, v in values.items():
        if k in BOOL_FIELDS:
            if v not in ("0", "1"):
                errs.append(f"{k} 只能是 0 或 1")
                continue
            clean[k] = v
        elif k in PARAM_FIELDS:
            name, _, kind, (lo, hi) = PARAM_FIELDS[k]
            try:
                num = float(v)
            except ValueError:
                errs.append(f"{name} 的值「{v}」不是数字")
                continue
            if kind == "int":
                num = int(round(num))
            if not (lo <= num <= hi):
                errs.append(f"{name} 要在 {lo}~{hi} 之间（当前 {v}）")
                continue
            clean[k] = str(num) if kind == "float" else str(int(num))
        else:
            errs.append(f"未知参数 {k}")
    if errs:
        return False, "；".join(errs)

    existing = {l.split("=", 1)[0].strip(): i
                for i, l in enumerate(open(path, encoding="utf-8"))
                if "=" in l and not l.strip().startswith("#")}
    lines = open(path, encoding="utf-8").read().splitlines()
    for k, v in clean.items():
        if k in existing:
            lines[existing[k]] = f"{k}={v}"
        else:
            lines.append(f"{k}={v}")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return True, "已写入配置"


# ---------------------------------------------------------------- 自动登录（需 root）

_CUSTOM_CONF = "/etc/gdm3/custom.conf"


def autologin_state() -> tuple[str | None, str]:
    """读取自动登录设置。custom.conf 通常 0644，普通用户能读。"""
    if not os.path.exists(_CUSTOM_CONF):
        return None, f"找不到 {_CUSTOM_CONF}（这台机器可能不是 GDM）"
    txt = open(_CUSTOM_CONF, encoding="utf-8", errors="replace").read()
    enable = re.search(r"^\s*AutomaticLoginEnable\s*=\s*(\S+)", txt, re.M)
    who = re.search(r"^\s*AutomaticLogin\s*=\s*(\S+)", txt, re.M)
    if not enable:
        return False, "未配置 AutomaticLoginEnable"
    val = enable.group(1).lower() in ("true", "1", "yes")
    return val, (who.group(1) if who else "(未指定用户)")


def set_autologin(enable: bool, user: str | None = None) -> tuple[bool, str]:
    """经 pkexec 调提权助手。这里不自己写 /etc，全部交给那个只做一件事的脚本。"""
    if not shutil.which("pkexec"):
        return False, "系统里没有 pkexec，无法安全提权"
    helper = helper_path()
    if not os.path.exists(helper):
        return False, f"提权助手缺失：{helper}"
    # 只传 on|off。受益用户由助手自己从 PKEXEC_UID 取，不在这里自称——
    # 传用户名等于让调用方决定给谁开自动登录，那是不该存在的口子。
    args = [POLKIT_ACTION, helper, "on" if enable else "off"]
    rc, out = run(["pkexec"] + args, timeout=120)
    ok = rc == 0
    if rc == 126:
        return False, "认证被取消或超时"
    return ok, out or ("已修改" if ok else f"exit={rc}")


def helper_path() -> str:
    """提权助手位置：打包态在 /opt，开发态在仓库里。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for cand in ("/opt/face-unlock/priv/set_autologin.py",
                 os.path.join(root, "priv", "set_autologin.py")):
        if os.path.exists(cand):
            return cand
    return "/opt/face-unlock/priv/set_autologin.py"


# ---------------------------------------------------------------- 状态汇总

def overview() -> dict:
    cfg = common.load_config()
    deps = common.check_deps(cfg)
    al, al_detail = autologin_state()
    sup, why = unlock_supported()
    stype, _ = session_type()
    rc, uptime = run(["systemctl", "--user", "show", UNIT, "-p", "NRestarts", "-p", "ExecMainStartTimestamp"])
    return {
        "deps": deps,
        "session_type": stype,
        "supported": sup,
        "support_reason": why,
        "autologin": al,
        "autologin_detail": al_detail,
        "service": service_state(),
        "service_detail": uptime,
        "config_path": common.config_path(),
        "data_path": common.data_dir(),
        "paused": common.is_paused(),
        "screen_locked": safe_locked(),
        "monitor": common.describe_monitor(common.x_environment(cfg)),
        "templates": len(facelib.load_templates(common.template_path())[0]),
    }


def safe_locked() -> bool | None:
    try:
        return common.is_screen_locked()
    except Exception:
        return None


def logs(lines: int = 80) -> str:
    rc, out = run(["journalctl", "--user", "-u", UNIT, "-n", str(lines), "--no-pager"], timeout=20)
    return out or f"(无输出 exit={rc})"


# ---------------------------------------------------------------- 录脸（GUI 驱动，无子线程）

class Enroller:
    """一次一步的录脸器。

    故意不开线程：GUI 用 GLib.timeout_add 每帧调用 step()，画面、进度、异常全在同一
    条时间线上，省掉跨线程回调这一类难查的问题。命令行也能复用同一个 step()。
    """

    def __init__(self, target: int = 12, diversity: float = 0.12,
                 pause_service: bool = True, append: bool = False):
        self.target = max(3, int(target))
        self.diversity = diversity
        self.cfg = common.load_config()
        self.engine = facelib.FaceEngine(
            det_size=common.cfg_size(self.cfg, "DET_SIZE"),
            score_threshold=common.cfg_float(self.cfg, "DET_SCORE"),
            min_face_px=common.cfg_int(self.cfg, "MIN_FACE_PX"),
            camera_index=common.cfg_int(self.cfg, "CAMERA_INDEX"),
            capture_size=common.cfg_size(self.cfg, "CAPTURE_SIZE"))
        self.append = append
        self.cap = None
        self.embs: list = []
        self.crops: list = []
        self.done = False
        self.message = "正在打开摄像头…"
        self._rejected = 0
        self._last_hint = 0.0
        if pause_service:
            # 录脸时让守护进程别来抢摄像头。它只在锁屏时开摄像头，正常情况本不冲突，
            # 但如果用户是「锁屏状态下用远程方式录入」就会撞上。
            with open(common.disable_flag(), "w", encoding="utf-8") as fh:
                fh.write(str(time.time() + 1800))
            self._paused_by_me = True
        else:
            self._paused_by_me = False

    def start(self) -> None:
        try:
            self.cap = self.engine.open_camera()
            self.message = "请把正脸放在画面中央，慢慢转头"
        except Exception as exc:
            self.done = True
            self.message = f"摄像头打不开：{exc}"

    def step(self):
        """处理一帧。返回 (frame_bgr|None, message, n, target, finished)。"""
        import numpy as np
        if self.cap is None or self.done:
            return None, self.message, len(self.embs), self.target, True
        frame = self.engine.grab(self.cap)
        if frame is None:
            self.message = "读帧失败，摄像头可能被其它程序占用"
            self._rejected += 1
            if self._rejected > 60:
                self.finish()
            return None, self.message, len(self.embs), self.target, self.done
        try:
            face = self.engine.best_face(frame)
            crop = self.engine.recognizer.alignCrop(frame, face)
            emb = self.engine.embedding(frame, face)
            worst = min((self.engine.similarity(emb, e) for e in self.embs), default=0.0)
            if self.embs and worst > 1.0 - self.diversity:
                raise facelib.NoFace(f"角度重复（相似 {worst:.2f}），请缓缓换个角度")
        except facelib.NoFace as exc:
            if time.monotonic() - self._last_hint > 0.4:
                self.message = str(exc)
                self._last_hint = time.monotonic()
            self._rejected += 1
            if self._rejected > 1500:
                self.message = "连续失败太多，已停止；请检查光线与位置"
                self.finish()
            return frame, self.message, len(self.embs), self.target, self.done

        self.embs.append(emb)
        self.crops.append(crop)
        self.message = f"已采集 {len(self.embs)}/{self.target}"
        if len(self.embs) >= self.target:
            self.finish(commit=True)
        return frame, self.message, len(self.embs), self.target, self.done

    def finish(self, commit: bool = False) -> None:
        import numpy as np
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None
        if self._paused_by_me:
            try:
                os.unlink(common.disable_flag())
            except OSError:
                pass
            self._paused_by_me = False
        if commit and self.embs:
            new = np.stack(self.embs).astype(np.float32)
            old, _ = facelib.load_templates(common.template_path())
            allv = np.concatenate([old, new], axis=0) if (self.append and len(old)) else new
            facelib.save_templates(common.template_path(), allv, {
                "enrolled_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "camera": f"/dev/video{self.cfg['CAMERA_INDEX']}",
                "detector_size": "x".join(map(str, self.engine.det_size)),
                "count": str(len(allv)),
            })
            self.message = f"录入完成，共 {len(new)} 条特征"
        elif self.embs:
            self.message = f"已放弃本次采集（{len(self.embs)} 帧未提交）"
        self.done = True

    def preview_rgb(self, frame):
        """BGR -> 连续 RGB 字节，供 GtkPixbuf 直接消费。"""
        if frame is None:
            return None, 0, 0
        h, w = frame.shape[:2]
        scale = min(1.0, 640.0 / w)
        if scale < 1.0:
            import cv2
            frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
            h, w = frame.shape[:2]
        import numpy as np
        rgb = np.ascontiguousarray(frame[:, :, ::-1])
        return rgb.tobytes(), w, h


def clear_templates() -> tuple[bool, str]:
    p = common.template_path()
    if not os.path.exists(p):
        return True, "本来就没有模板"
    bak = p + f".bak-{int(time.time())}"
    os.replace(p, bak)
    return True, f"已清空，原文件备份为 {os.path.basename(bak)}"
