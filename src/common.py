"""配置、路径、屏幕锁状态、X 环境发现等公共设施。

被 face_enroll / face_unlockd / face_ctl 共用，保证三个入口对同一份配置的理解一致。
"""

from __future__ import annotations

import glob
import os
import re
import subprocess
import time

APP = "face-unlock"

DEFAULTS: dict[str, str] = {
    # 摄像头
    "CAMERA_INDEX": "0",
    "CAPTURE_SIZE": "1280x720",
    # 检测：尺寸宽高都必须能被 32 整除，facelib 会静默拉齐
    "DET_SIZE": "640x352",
    "DET_SCORE": "0.6",
    "MIN_FACE_PX": "80",
    # 判定
    "THRESHOLD": "0.42",         # 余弦相似度 >= 此值算本人；见 face-ctl tune
    "STREAK": "4",               # 连续多少帧匹配才放行
    "STREAK_WINDOW_S": "2.5",    # 这些帧必须落在这个时间窗内（防止慢慢磨）
    "ANTI_SPOOF": "1",           # 要求确认窗内画面有轻微变化，挡静止图像
    # 节奏
    "LOCK_SETTLE_S": "1.2",       # 开始扫描前等多久（等锁屏动画/亮屏稳定）
    "SCAN_FPS": "12",
    # 扫描窗改由「屏幕亮灭」驱动，不再是锁屏起倒计时：
    #   熄屏 -> 完全不扫（没人看着屏幕，也省摄像头灯和 CPU）
    #   亮屏 -> 立刻开一个窗，连续扫到解锁 / 熄屏 / 窗到期
    # 旧配置里的 GIVEUP_S 已废弃，不再被读取。
    "SCAN_ONLY_WHEN_SCREEN_ON": "1",
    "WAKE_POLL_S": "0.25",        # 熄屏期间多久查一次亮屏（实测 xset q 耗时 <1ms）
    "SCAN_WINDOW_S": "600",       # 单次连续扫描的上限，到期后无缝重开，不算失败
    "IDLE_POLL_S": "0.3",
    # 解锁动作
    "MAX_ATTEMPTS": "3",         # 一个扫描窗内最多注入几次密码
    "BACKOFF_S": "60",           # 仅当「注入过密码但没解开」才退避；没看见人不退避
    "TYPE_DELAY_MS": "25",
    # 注入前先按这两下把 GNOME 锁屏从时钟页唤醒到密码框。
    # space + BackSpace 的净效果为空：即使落进密码框也不会留下字符。
    # 别设成会往焦点窗口塞内容的键（ctrl+a、Delete 之类），屏幕没锁时会毁掉你的编辑现场。
    "WAKE_KEYS": "space BackSpace",
    # 开关
    "ENABLED": "1",
}


def uid() -> int:
    return os.getuid()


def data_dir() -> str:
    d = os.environ.get("FACE_UNLOCK_DIR") or os.path.join(
        os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), APP)
    os.makedirs(d, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def config_path() -> str:
    return os.environ.get("FACE_UNLOCK_CONFIG") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
        APP, "config")


def template_path() -> str:
    return os.path.join(data_dir(), "faces.npz")


def secret_path() -> str:
    return os.path.join(data_dir(), "secret")


def disable_flag() -> str:
    """存在此文件即暂停扫描。文件内容可选填「到什么时候为止」的 unix 时间戳。"""
    return os.path.join(data_dir(), "disabled")


def pause_until() -> float | None:
    """返回暂停到期时间戳；永久暂停返回 inf；未暂停返回 None。"""
    p = disable_flag()
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            body = fh.read().strip()
    except OSError:
        return float("inf")
    if not body:
        return float("inf")
    try:
        return float(body)
    except ValueError:
        return float("inf")


def is_paused() -> bool:
    until = pause_until()
    if until is None:
        return False
    if until != float("inf") and time.time() >= until:
        try:
            os.unlink(disable_flag())
        except OSError:
            pass
        return False
    return True


def load_config() -> dict[str, str]:
    cfg = dict(DEFAULTS)
    path = config_path()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def cfg_float(cfg: dict, key: str) -> float:
    return float(cfg[key])


def cfg_int(cfg: dict, key: str) -> int:
    return int(float(cfg[key]))


def cfg_size(cfg: dict, key: str) -> tuple[int, int]:
    w, h = cfg[key].lower().split("x")
    return int(w), int(h)


def write_default_config(force: bool = False) -> str:
    path = config_path()
    if os.path.exists(path) and not force:
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# 人脸无感解锁配置。改完执行 face-ctl restart 生效。\n")
        fh.write("# THRESHOLD 越高越严（越容易拒你自己），越低越松（越容易放行别人）。\n")
        for k, v in DEFAULTS.items():
            fh.write(f"{k}={v}\n")
    os.chmod(path, 0o600)
    return path


# ---------- 日志 ----------

_t0 = time.time()


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def warn(msg: str) -> None:
    log(f"!! {msg}")


# ---------- 屏幕锁状态 ----------

_SCREENSAVER_DEST = "org.gnome.ScreenSaver"
_SCREENSAVER_PATH = "/org/gnome/ScreenSaver"


def _dbus_session_call(method: str, timeout: float = 3.0) -> str:
    env = dict(os.environ)
    env.setdefault("DBUS_SESSION_BUS_ADDRESS",
                   f"unix:path=/run/user/{uid()}/bus")
    try:
        out = subprocess.run(
            ["dbus-send", "--session", "--print-reply",
             "--dest=" + _SCREENSAVER_DEST, _SCREENSAVER_PATH,
             "org.gnome.ScreenSaver." + method],
            capture_output=True, text=True, timeout=timeout, env=env)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise RuntimeError(f"dbus 调用失败（{method}）：{exc}") from exc
    if out.returncode != 0:
        raise RuntimeError(f"dbus 调用 {method} 返回 {out.returncode}: {out.stderr.strip()[:120]}")
    return out.stdout


def is_screen_locked() -> bool:
    """GNOME 会话锁的权威状态。实测在 ubuntu:GNOME / X11 下可用。"""
    reply = _dbus_session_call("GetActive")
    return "boolean true" in reply


def lock_screen() -> None:
    _dbus_session_call("Lock")


# ---------- X 环境发现 ----------

def discover_display() -> str:
    """/tmp/.X11-unix/X<n> 存在即说明该编号有 X server 在跑。"""
    socks = sorted(glob.glob("/tmp/.X11-unix/X*"))
    if not socks:
        raise RuntimeError("没发现任何 X socket，本机似乎不在 X11 会话下")
    return ":" + os.path.basename(socks[0])[1:]


def _xauthority_from_xorg_cmdline(display: str) -> str | None:
    """从正在运行的 Xorg 进程参数里抠出 -auth 的路径。

    GDM 走 -displayfd 传递编号，命令行里没有显式的 :0，所以编号只能靠 socket 发现。
    """
    for stat in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            with open(stat, "rb") as fh:
                args = fh.read().split(b"\0")
        except OSError:
            continue
        argv = [a.decode("utf-8", "replace") for a in args if a]
        if not argv or "Xorg" not in os.path.basename(argv[0]):
            continue
        for i, a in enumerate(argv):
            if a == "-auth" and i + 1 < len(argv):
                if os.path.exists(argv[i + 1]):
                    return argv[i + 1]
    return None


def x_environment(cfg: dict) -> dict[str, str]:
    """给 xdotool 用的环境。systemd user service 不会继承 DISPLAY，必须自己拼。"""
    env = dict(os.environ)
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid()}/bus")

    display = cfg.get("DISPLAY") or env.get("DISPLAY")
    if not display:
        display = discover_display()
    env["DISPLAY"] = display

    xauth = cfg.get("XAUTHORITY") or env.get("XAUTHORITY")
    if not xauth or not os.path.exists(xauth):
        candidates = [_xauthority_from_xorg_cmdline(display),
                      f"/run/user/{uid()}/gdm/Xauthority",
                      os.path.expanduser("~/.Xauthority")]
        xauth = next((c for c in candidates if c and os.path.exists(c)), None)
    if xauth:
        env["XAUTHORITY"] = xauth
    return env


def describe_x_env(cfg: dict) -> str:
    env = x_environment(cfg)
    return f"DISPLAY={env.get('DISPLAY')} XAUTHORITY={env.get('XAUTHORITY', '(未找到!)')}"


_MONITOR_RE = re.compile(r"Monitor is\s+(\w+)")
_DPMS_RE = re.compile(r"DPMS is\s+(\w+)")


def monitor_power(env: dict) -> tuple[str | None, bool]:
    """返回 (显示器电源状态, DPMS 是否启用)。

    本机实测：`xset q` 单次耗时 <1ms，`xset dpms force off` 能读到 "Monitor is Off"，
    唤醒后回到 "On"。这是「用户回来了」的可靠信号——鼠标动一下就会让 DPMS 从 Off 翻回 On。
    读不到时返回 (None, False)，调用方必须降级成「照常扫描」，不能因为读不到就不扫，
    否则门禁会静默失效。
    """
    try:
        p = subprocess.run(["xset", "q"], capture_output=True, text=True,
                           timeout=2, env=env)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None, False
    if p.returncode != 0:
        return None, False
    m = _MONITOR_RE.search(p.stdout)
    d = _DPMS_RE.search(p.stdout)
    enabled = bool(d) and d.group(1).lower() == "enabled"
    return (m.group(1).lower() if m else None), enabled


def describe_monitor(env: dict) -> str:
    state, enabled = monitor_power(env)
    if state is None:
        return "xset 读不到显示器电源状态（降级为持续扫描）"
    return f"Monitor={state} DPMS={'启用' if enabled else '未启用'}"


# ---------- 密码 ----------

def read_secret() -> str:
    path = secret_path()
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"缺少 {path}。执行 face-ctl set-password 写入登录密码")
    mode = os.stat(path).st_mode & 0o777
    if mode & 0o077:
        warn(f"{path} 权限是 {oct(mode)}，其他用户可读；执行 chmod 600 {path}")
    with open(path, encoding="utf-8") as fh:
        return fh.read().rstrip("\n")


def write_secret(password: str) -> None:
    path = secret_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(password + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


# ---------- 依赖体检 ----------

def check_deps(cfg: dict, need_secret: bool = True) -> list[tuple[str, bool, str]]:
    """返回 [(名称, 是否 OK, 说明)]，供 face-ctl status 和 install.sh 复用。"""
    import facelib

    items: list[tuple[str, bool, str]] = []

    def add(name, ok, detail):
        items.append((name, bool(ok), detail))

    try:
        import cv2
        add("opencv", True, f"{cv2.__version__} @ {cv2.__file__}")
        ver = tuple(int(x) for x in cv2.__version__.split(".")[:2])
        if ver < (4, 6):
            items[-1] = ("opencv", False,
                         f"{cv2.__version__} 太旧，加载不了 YuNet；必须 >= 4.6（用 venv 里的）")
    except Exception as exc:
        add("opencv", False, str(exc))
        return items

    for f in ("yunet.onnx", "sface.onnx"):
        p = os.path.join(facelib.MODEL_DIR, f)
        add(f"模型 {f}", os.path.exists(p), p if os.path.exists(p) else f"未下载：{p}")

    import shutil
    xd = shutil.which("xdotool")
    if xd:
        try:
            ver = subprocess.run([xd, "--version"], capture_output=True, text=True,
                                 timeout=3).stdout.strip().splitlines()[-1]
        except Exception:
            ver = xd
        add("xdotool", True, ver)
    else:
        add("xdotool", False, "找不到 -> sudo apt install -y xdotool")

    idx = cfg.get("CAMERA_INDEX", "0")
    dev = f"/dev/video{idx}"
    readable = os.path.exists(dev) and os.access(dev, os.R_OK)
    add("摄像头", readable,
        f"{dev} 可读" if readable else f"{dev} 不存在或无读取权限（用户需在 video 组，或有 ACL）")

    try:
        add("D-Bus 锁状态", True, f"GetActive={is_screen_locked()}")
    except Exception as exc:
        add("D-Bus 锁状态", False, str(exc))

    try:
        add("X 环境", True, describe_x_env(cfg))
    except Exception as exc:
        add("X 环境", False, str(exc))

    try:
        env = x_environment(cfg)
        state, enabled = monitor_power(env)
        gate = cfg.get("SCAN_ONLY_WHEN_SCREEN_ON", "1") == "1"
        detail = describe_monitor(env)
        if state is None:
            add("亮屏门控", not gate, detail)
        elif gate and not enabled:
            add("亮屏门控", not gate, detail + "  <-- DPMS 没启用，亮屏门控无从判断")
        else:
            add("亮屏门控", True, detail)
    except Exception as exc:
        add("亮屏门控", False, str(exc))

    embs, meta = facelib.load_templates(template_path())
    add("人脸模板", len(embs) > 0,
        f"{len(embs)} 条特征（{meta.get('enrolled_at', '未录入')}）")

    if need_secret:
        try:
            s = read_secret()
            add("登录密码", len(s) > 0, f"{len(s)} 个字符 @ {secret_path()}")
        except Exception as exc:
            add("登录密码", False, str(exc))

    return items
