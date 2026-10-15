#!/usr/bin/env bash
# 人脸无感解锁 —— 安装脚本。幂等，可反复执行。
#   bash install.sh          全量安装
#   bash install.sh --check  只体检，不装任何东西
#
# 全部落在用户目录（~/.local/lib/face-unlock 及 ~/.config、~/.config/systemd/user），
# 不碰 /etc，只有 apt 装 xdotool 那一步需要 sudo。
set -uo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/lib/face-unlock}"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
PYPI_MIRROR="${PYPI_MIRROR:-https://pypi.tuna.tsinghua.edu.cn/simple}"
CAMERA_INDEX="${CAMERA_INDEX:-0}"
CV_PIN="opencv-python-headless==4.11.0.86"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

BOLD=$'\033[1m'; RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; RST=$'\033[0m'
step() { printf '\n%s== %s%s\n' "$BOLD" "$1" "$RST"; }
ok()   { printf '  %s[OK]%s   %s\n' "$GRN" "$RST" "$1"; }
bad()  { printf '  %s[FAIL]%s %s\n' "$RED" "$RST" "$1"; }
warn() { printf '  %s[警告]%s %s\n' "$YEL" "$RST" "$1"; }
die()  { printf '\n%s中断：%s%s\n' "$RED" "$1" "$RST"; exit 1; }
FAILS=0
need() { FAILS=$((FAILS+1)); bad "$1"; }

VPY=""   # 第 3 步之后指向安装目录里的 venv python

# ---------------------------------------------------------------- 1. 体检
step "1/6 环境体检"
. /etc/os-release 2>/dev/null || true
ok "发行版 ${PRETTY_NAME:-未知}"

case "${XDG_SESSION_TYPE:-}" in
  wayland) die "当前是 Wayland 会话。xdotool 无法向锁屏注入按键，这个方案在 Wayland 上不成立。
      修法：注销 -> 登录界面左下角齿轮选「Ubuntu on Xorg」重新登录，再跑本脚本。
      （你本机 /etc/gdm3/custom.conf 已写 WaylandEnable=false，正常应该落在 X11）" ;;
  x11)     ok "会话类型 X11，xdotool 注入可用" ;;
  *)       [ -e /tmp/.X11-unix/X0 ] && ok "无 XDG_SESSION_TYPE 但 X11 socket 在，按 X11 处理" \
                                  || need "确认不了 X11 会话。请在桌面终端里跑，不要在纯 SSH 里跑" ;;
esac

command -v dbus-send >/dev/null || need "缺 dbus-send：sudo apt install -y dbus-x11"
command -v loginctl  >/dev/null || need "缺 loginctl（systemd 自带，异常了检查 systemd）"
command -v curl      >/dev/null || need "缺 curl：sudo apt install -y curl"
command -v python3   >/dev/null || need "缺 python3"
python3 -m venv --help >/dev/null 2>&1 || need "缺 venv 模块：sudo apt install -y python3-venv"
command -v xdotool   >/dev/null || warn "缺 xdotool，第 2 步会装（需要 sudo）"

[ -e "/dev/video$CAMERA_INDEX" ] || need "/dev/video$CAMERA_INDEX 不存在，接好摄像头再来"
[ -r "/dev/video$CAMERA_INDEX" ] || need "/dev/video$CAMERA_INDEX 当前用户不可读。
      先校验： getfacl -p /dev/video$CAMERA_INDEX
      临时修： sudo setfacl -m u:$USER:rw /dev/video$CAMERA_INDEX      # 重启失效
      永久修： sudo usermod -aG video $USER 然后注销重新登录"

if [ "$CHECK_ONLY" = 1 ]; then
  if [ "$FAILS" -gt 0 ]; then printf '\n体检未通过 %d 项\n' "$FAILS"; exit 1; fi
  step "体检结束"; printf '  没问题。去掉 --check 正式安装。\n'; exit 0
fi
[ "$FAILS" -gt 0 ] && die "体检有 $FAILS 项未通过，先按提示修好再继续"

# ---------------------------------------------------------------- 2. apt 依赖
step "2/6 系统依赖"
if command -v xdotool >/dev/null; then
  ok "xdotool 已就绪"
else
  printf '  执行： sudo apt-get install -y xdotool\n'
  sudo apt-get install -y xdotool || die "apt 安装 xdotool 失败"
  command -v xdotool >/dev/null || die "装完仍找不到 xdotool"
  ok "xdotool 就绪"
fi

# ---------------------------------------------------------------- 3. venv + OpenCV
# Ubuntu 22.04 自带的 python3-opencv 是 4.5.4，它的 FaceDetectorYN::detect 依赖模型里
# 名为 width/height 的动态维度：YuNet 2022mar 过不了 4.5.4 的 onnx 导入器（-210），
# 2023mar 能在建立网络时通过但 detect() 报 -204 Layer with requested id=-1 not found。
# 两个版本都不可用，所以必须独立装 OpenCV >= 4.6。
# 用 venv 而不是 pip --user：你平时用系统 cv2 做点云/ROS，不能污染它。
step "3/6 Python 环境（隔离在 $INSTALL_DIR/.venv）"
mkdir -p "$INSTALL_DIR" || die "创建 $INSTALL_DIR 失败"
if [ ! -x "$INSTALL_DIR/.venv/bin/python" ]; then
  printf '  创建 venv…\n'
  python3 -m venv "$INSTALL_DIR/.venv" || die "python3 -m venv 失败"
fi
VPY="$INSTALL_DIR/.venv/bin/python"
[ -x "$VPY" ] || die "venv python 不存在：$VPY"

if ! "$VPY" -c 'import cv2' >/dev/null 2>&1; then
  "$VPY" -m pip install --quiet --upgrade pip 2>/dev/null
  printf '  pip 安装 %s（直连 PyPI）…\n' "$CV_PIN"
  "$VPY" -m pip install --quiet "$CV_PIN" \
    || { printf '  直连失败，改用镜像 %s\n' "$PYPI_MIRROR"
         "$VPY" -m pip install --quiet -i "$PYPI_MIRROR" "$CV_PIN" \
           || die "$CV_PIN 安装失败：直连和镜像都试过。手动执行：
              $VPY -m pip install $CV_PIN" ; }
fi
CV_VER=$("$VPY" -c 'import cv2;print(cv2.__version__)') || die "venv 里 cv2 导入失败"
"$VPY" -c 'import cv2,sys;v=tuple(int(x) for x in cv2.__version__.split(".")[:2]);sys.exit(0 if v>=(4,6) else 1)' \
  || die "cv2 $CV_VER 太旧，需要 >= 4.6"
"$VPY" -c 'import cv2,sys;sys.exit(0 if hasattr(cv2,"FaceDetectorYN") and hasattr(cv2,"FaceRecognizerSF") else 1)' \
  || die "这个 cv2 构建缺 FaceDetectorYN/FaceRecognizerSF（装的是 opencv-python-headless 吗）"
ok "venv cv2 $CV_VER，YuNet/SFace 接口齐全"
ok "系统 cv2 未被改动：$(python3 -c 'import cv2;print(cv2.__version__)' 2>/dev/null || echo '（系统未装）')"

# ---------------------------------------------------------------- 4. 模型
step "4/6 识别模型"
MDIR="$INSTALL_DIR/models"; mkdir -p "$MDIR"
ZOO="https://github.com/opencv/opencv_zoo/raw/main/models"
# 兜底镜像经本机实测：ghfast.top 回 200 且字节数与源站一致（38696353）；
# gh-proxy.com 回 403，mirror.ghproxy.com 连接失败，gitclone.com 回 500 —— 别用后三个。
MIRROR_PREFIX="${MIRROR_PREFIX:-https://ghfast.top/}"
fetch_model() {  # 文件名 主地址 备用地址 最小字节数
  local f="$MDIR/$1" url
  # 源目录里已有合格的就复用，省一次下载
  if [ "$SRC_DIR" != "$INSTALL_DIR" ] \
     && [ -f "$SRC_DIR/models/$1" ] && [ "$(stat -c%s "$SRC_DIR/models/$1")" -ge "$4" ]; then
    cp -f "$SRC_DIR/models/$1" "$f"; ok "$1 复用源目录已有文件（$(stat -c%s "$f") 字节）"; return 0
  fi
  if [ -f "$f" ] && [ "$(stat -c%s "$f")" -ge "$4" ]; then
    ok "$1 已存在（$(stat -c%s "$f") 字节）"; return 0
  fi
  for url in "$2" "$MIRROR_PREFIX$2" "${3:-}"; do
    [ -n "$url" ] || continue
    printf '  下载 %s ← %s\n' "$1" "${url:0:78}"
    if curl -fsSL --retry 2 -m 300 -o "$f" "$url" && [ "$(stat -c%s "$f")" -ge "$4" ]; then
      ok "$1 $(stat -c%s "$f") 字节"; return 0
    fi
    rm -f "$f"
  done
  need "$1 下载失败（GitHub 源站和 ghfast.top 镜像都试过）。手动下载后放到 $f ，再重跑本脚本：
        $2"
}
fetch_model yunet.onnx "$ZOO/face_detection_yunet/face_detection_yunet_2023mar.onnx" \
                       "$ZOO/face_detection_yunet/face_detection_yunet_2022mar.onnx" 200000
fetch_model sface.onnx "$ZOO/face_recognition_sface/face_recognition_sface_2021dec.onnx" \
                       "" 30000000
[ "$FAILS" -gt 0 ] && die "模型没备齐，不继续"

# 文件存在不代表能推理，真跑一遍
"$VPY" - "$MDIR" <<'PY' || die "模型推理自检失败 —— 十有八九是 cv2 版本或模型文件不完整"
import cv2, numpy as np, sys
m = sys.argv[1]
d = cv2.FaceDetectorYN_create(f"{m}/yunet.onnx", "", (320, 320), 0.6, 0.3, 5000)
r = cv2.FaceRecognizerSF_create(f"{m}/sface.onnx", "")
img = (np.random.rand(320, 320, 3) * 255).astype(np.uint8)
d.detect(img)
face = np.array([[20, 40, 120, 160, 55, 90, 95, 90, 75, 115, 58, 140, 92, 140, 0.9]],
                dtype=np.float32)
f1 = r.feature(r.alignCrop(img, face))
assert f1.shape[1] == 128, f1.shape
assert abs(r.match(f1, f1, cv2.FaceRecognizerSF_FR_COSINE) - 1.0) < 1e-4
print("  推理自检通过：检测→对齐→128维特征→比对 全链路可用")
PY

# ---------------------------------------------------------------- 5. 代码 + 入口
step "5/6 安装代码"
if [ "$SRC_DIR" != "$INSTALL_DIR" ]; then
  mkdir -p "$INSTALL_DIR/src" "$INSTALL_DIR/systemd"
  cp -a "$SRC_DIR/src/."      "$INSTALL_DIR/src/"
  cp -a "$SRC_DIR/systemd/."  "$INSTALL_DIR/systemd/"
  [ -f "$SRC_DIR/README.md" ] && cp -a "$SRC_DIR/README.md" "$INSTALL_DIR/"
  ok "已复制到 $INSTALL_DIR"
else
  ok "就地安装（INSTALL_DIR == 源目录）"
fi
chmod +x "$INSTALL_DIR/src/lock-at-boot.sh"

UNIT_DIR="$HOME/.config/systemd/user"; mkdir -p "$UNIT_DIR"
for u in face-unlockd face-lock-at-boot; do
  src="$INSTALL_DIR/systemd/$u.service.in"
  [ -f "$src" ] || die "缺少单元模板 $src"
  sed "s|__INSTALL_DIR__|$INSTALL_DIR|g" "$src" > "$UNIT_DIR/$u.service"
  grep -q "__INSTALL_DIR__" "$UNIT_DIR/$u.service" && die "$u.service 占位符没替换干净"
  ok "生成 $UNIT_DIR/$u.service"
done

mkdir -p "$BIN_DIR"
printf '#!/bin/sh\nexec "%s/.venv/bin/python" "%s/src/face_ctl.py" "$@"\n' \
       "$INSTALL_DIR" "$INSTALL_DIR" > "$BIN_DIR/face-ctl"
printf '#!/bin/sh\nexec "%s/.venv/bin/python" "%s/src/face_enroll.py" "$@"\n' \
       "$INSTALL_DIR" "$INSTALL_DIR" > "$BIN_DIR/face-enroll"
chmod +x "$BIN_DIR/face-ctl" "$BIN_DIR/face-enroll"
ok "命令入口 $BIN_DIR/face-ctl · $BIN_DIR/face-enroll"
case ":$PATH:" in *":$BIN_DIR:"*) ok "$BIN_DIR 在 PATH 里" ;;
  *) warn "$BIN_DIR 不在 PATH。执行：
      echo 'export PATH=\$HOME/.local/bin:\$PATH' >> ~/.bashrc && source ~/.bashrc" ;;
esac

CFG="$HOME/.config/face-unlock/config"
"$VPY" -c "import sys;sys.path.insert(0,'$INSTALL_DIR/src');import common;print('  配置 '+common.write_default_config())" \
  || die "写配置失败"

# ---------------------------------------------------------------- 6. 服务
step "6/6 systemd 用户服务"
systemctl --user daemon-reload || die "systemctl --user daemon-reload 失败"
systemctl --user enable face-unlockd.service face-lock-at-boot.service 2>/dev/null \
  || warn "enable 失败（不影响本次启动，只影响下次登录自启）"
ok "已 enable：下次登录自动起守护进程"
systemctl --user restart face-unlockd.service || warn "守护进程这次没起来（还没录脸属正常，见 face-ctl status）"

# ---------------------------------------------------------------- 收尾校验
step "最终校验"
"$VPY" - "$INSTALL_DIR" <<'PY' || warn "自检未通过，见上面报错"
import sys
sys.path.insert(0, sys.argv[1] + "/src")
import common, facelib
cfg = common.load_config()
eng = facelib.FaceEngine(det_size=common.cfg_size(cfg, "DET_SIZE"),
                         camera_index=common.cfg_int(cfg, "CAMERA_INDEX"))
print(f"  检测尺寸 : {eng.det_size}（宽高已 32 对齐）")
eng.open_camera().release()
print("  摄像头   : 打开并取流成功（自动曝光预热 8 帧）")
try:
    print(f"  锁状态   : {'已锁' if common.is_screen_locked() else '未锁'} —— D-Bus 通道可用")
except Exception as exc:
    print(f"  锁状态   : 查询失败 {exc}")
try:
    print(f"  X 环境   : {common.describe_x_env(cfg)}")
except Exception as exc:
    print(f"  X 环境   : 发现失败 {exc}")
PY

cat <<TXT

--------------------------------------------------------------------------
装完了，但门禁还没生效 —— 剩下两步必须你本人做：

  1) face-enroll                  录你的脸（约 30 秒，会提示你转头）
  2) face-ctl set-password        写入登录密码（自动解锁必须靠它）

然后验证：
  face-ctl status                 体检，全 OK 才算就绪
  face-ctl test                   只看相似度，不注入密码
  face-ctl lock                   手动锁屏，看它几秒内自己解开
  face-ctl logs                   守护进程在想什么
  journalctl --user -u face-unlockd.service -f

日常开关：
  face-ctl pause 300              5 分钟内不许摄像头工作
  face-ctl resume                 立即恢复
  face-ctl off                    停用扫描（密码文件仍在，按需 rm）

彻底回退：
  bash $SRC_DIR/uninstall.sh
--------------------------------------------------------------------------
TXT
