#!/usr/bin/env bash
# 构建自包含离线 .deb。
#
#   ./build-deb.sh --release jammy                    构建 22.04 包（Python 3.10）
#   ./build-deb.sh --release noble --pyver 312        构建 24.04 包（Python 3.12）
#   ./build-deb.sh --release jammy --fetch-only       只抓 wheel 和模型
#
# 为什么按 Ubuntu 版本分别出包：venv 与 Python 小版本绑定，而 wheel 与 cp3xx ABI 绑定。
# 自包含离线安装的代价就是这个矩阵；好处是装的时候不联网，也完全不依赖发行版 OpenCV 版本。
set -euo pipefail

RELEASE="jammy"; ARCH="amd64"; VERSION="1.0.0"
PYVER=""; PYVER_SET=0
CACHE="packaging/cache"
ROOT="packaging/pkgroot"
OUT="packaging/out"
FETCH_ONLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --release) RELEASE="$2"; shift 2 ;;
    --pyver)   PYVER="$2"; PYVER_SET=1; shift 2 ;;
    --arch)    ARCH="$2";    shift 2 ;;
    --version) VERSION="$2"; shift 2 ;;
    --fetch-only) FETCH_ONLY=1; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "未知参数 $1" >&2; exit 2 ;;
  esac
done

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SRC_DIR"
INSTALL_DIR="/opt/face-unlock"

ZOO="https://github.com/opencv/opencv_zoo/raw/main/models"
# 钉死版本：本项目的全部测试是在这两个版本上做的。不钉会拉到未验证的大版本
# （第一次构建就实际拉到了 opencv 5.0.0.93），而 FaceDetectorYN 的 API 在 5.x 未验证。
CV_PIN="opencv-python-headless==4.11.0.86"
NP_PIN="numpy==2.2.6"
step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
ok()   { printf '  [OK] %s\n' "$1"; }
bad()  { printf '  [FAIL] %s\n' "$1" >&2; }
die()  { printf '\n\033[31m中断：%s\033[0m\n' "$1" >&2; exit 1; }

# 目标 release 的 Python 版本。写死在这里是因为装错 wheel 的后果要到用户装包时才暴露。
case "$RELEASE" in
  jammy)  WANT_PY="3.10"; PYVER_DEFAULT="310" ;;
  noble)  WANT_PY="3.12"; PYVER_DEFAULT="312" ;;
  oracular) WANT_PY="3.12"; PYVER_DEFAULT="312" ;;
  resolute) WANT_PY="3.13"; PYVER_DEFAULT="313" ;;
  *) die "未知的 Ubuntu 代号「$RELEASE」。已知：jammy / noble / oracular / resolute。
      新增 release 前先确认它的 python3 版本，并同步这里的 PYVER 映射。" ;;
esac
if [ "$PYVER_SET" = 0 ]; then
  PYVER="$PYVER_DEFAULT"
elif [ "$PYVER" != "$PYVER_DEFAULT" ]; then
  echo "  注意：--release $RELEASE 期望 wheel ABI cp$PYVER_DEFAULT，你显式指定了 cp$PYVER" >&2
  echo "        装到目标机上 venv 自检会失败（cp3xx ABI 不匹配），除非那台机器的 Python 确实是 $PYVER" >&2
fi
ok "目标 $RELEASE (Ubuntu Python $WANT_PY) / wheel ABI cp$PYVER / arch $ARCH"

# ---------------------------------------------------------------- 1. 抓依赖
step "1/5 抓取离线依赖（wheel + ONNX 模型）"
mkdir -p "$CACHE/wheels" "$CACHE/models"

# 本机 pip 只用来下载目标 ABI 的 wheel，不安装
PIP="${PIP:-python3 -m pip}"
fetch_wheels() {
  local spec="$1" pkg="${1%%=*}" ver=""
  case "$spec" in *==*) ver="${spec#*==}" ;; esac
  # 匹配条件放宽：opencv 这类发布的是 cp37-abi3 通用 wheel，文件名里没有 cp$PYVER，
  # 平台标记也是 manylinux 而不是 amd64 —— 用严格正则会产生假阴性。
  local stem; stem=$(echo "$pkg" | tr 'A-Z-' 'a-z_')
  if ls "$CACHE/wheels" 2>/dev/null | grep -qi "^${stem}_.*\.whl$"; then
    if [ -z "$ver" ] || ls "$CACHE/wheels" | grep -q "${stem}-${ver}-"; then
      ok "$pkg wheel 已在缓存（$(ls "$CACHE/wheels" | grep "${stem}-" | head -1)）"
      return 0
    fi
    echo "  缓存里 $pkg 版本不对，重新拉 $ver"; rm -f "$CACHE/wheels"/${stem}-*
  fi
  # shellcheck disable=SC2086
  $PIP download --only-binary=:all: --no-deps --dest "$CACHE/wheels" \
      --python-version "$PYVER" --implementation cp --abi "cp$PYVER" \
      --platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64 \
      "$spec" >/dev/null 2>&1 \
    || { echo "  直连失败，改用清华镜像"; $PIP download --only-binary=:all: --no-deps \
          -i https://pypi.tuna.tsinghua.edu.cn/simple --dest "$CACHE/wheels" \
          --python-version "$PYVER" --implementation cp --abi "cp$PYVER" \
          --platform manylinux2014_x86_64 "$spec" >/dev/null; }
  got=$(ls "$CACHE/wheels" 2>/dev/null | grep "${stem}-" | head -1)
  [ -n "$got" ] || { bad "$spec 没抓到可用 wheel"; return 1; }
  if [ -n "$ver" ] && ! echo "$got" | grep -q "${stem}-${ver}-"; then
    bad "$spec 实际拿到 $got —— 版本不符就停止，不能拿未验证的库出包"; return 1
  fi
  ok "$spec -> $got"
}
fetch_wheels "$CV_PIN"
fetch_wheels "$NP_PIN"

get_model() {  # 文件名 最小字节数
  local f="$CACHE/models/$1"
  if [ -f "$f" ] && [ "$(stat -c%s "$f")" -ge "$2" ]; then ok "$1 已缓存"; return 0; fi
  local url="$ZOO/$3"
  for u in "$url" "https://ghfast.top/$url"; do
    if curl -fsSL --retry 2 -m 600 -o "$f" "$u" && [ "$(stat -c%s "$f")" -ge "$2" ]; then
      ok "$1 $(stat -c%s "$f") 字节"; return 0
    fi
    rm -f "$f"
  done
  bad "$1 下载失败：$url"; return 1
}
get_model yunet.onnx 200000 "face_detection_yunet/face_detection_yunet_2023mar.onnx"
get_model sface.onnx 30000000 "face_recognition_sface/face_recognition_sface_2021dec.onnx"

FAILS=0
ls "$CACHE/wheels" | grep -q opencv || { bad "缺 opencv wheel"; FAILS=1; }
ls "$CACHE/wheels" | grep -q numpy  || { bad "缺 numpy wheel";  FAILS=1; }
[ -s "$CACHE/models/sface.onnx" ]   || { bad "缺 sface.onnx";   FAILS=1; }
[ "$FAILS" = 0 ] || die "离线依赖不齐，继续打包只会做出一个装不上的 deb"
du -sh "$CACHE" | sed 's/^/  缓存体积: /'
[ "$FETCH_ONLY" = 1 ] && { step "只抓取依赖，结束"; exit 0; }

# ---------------------------------------------------------------- 2. 组装文件树
step "2/5 组装安装树"
rm -rf "$ROOT"; mkdir -p \
  "$ROOT$INSTALL_DIR"/{src,gui,priv,tests,models,wheels} \
  "$ROOT/usr/bin" \
  "$ROOT/usr/lib/systemd/user" \
  "$ROOT/usr/share/applications" \
  "$ROOT/usr/share/polkit-1/actions" \
  "$ROOT/usr/share/doc/face-unlock" \
  "$ROOT/DEBIAN"

cp -a src/. "$ROOT$INSTALL_DIR/src/"
cp -a gui/. "$ROOT$INSTALL_DIR/gui/"
cp -a priv/. "$ROOT$INSTALL_DIR/priv/"
cp -a tests/. "$ROOT$INSTALL_DIR/tests/"
cp -a "$CACHE/models/." "$ROOT$INSTALL_DIR/models/"
cp -a "$CACHE/wheels/." "$ROOT$INSTALL_DIR/wheels/"
rm -rf "$ROOT$INSTALL_DIR"/**/__pycache__
find "$ROOT" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

# systemd 用户单元：安装路径固定，构建时就把占位符替换掉
for u in face-unlockd face-lock-at-boot; do
  sed "s|__INSTALL_DIR__|$INSTALL_DIR|g" systemd/$u.service.in > "$ROOT/usr/lib/systemd/user/$u.service"
  grep -q "__INSTALL_DIR__" "$ROOT/usr/lib/systemd/user/$u.service" && die "$u.service 占位符未替换"
  ok "单元 usr/lib/systemd/user/$u.service"
done

cat > "$ROOT/usr/bin/face-unlock-gui" <<EOF
#!/bin/sh
# 用自带 venv 的 python：它有 opencv 4.x，同时以 --system-site-packages 看得见发行版的 gi
exec "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/gui/face_unlock_gui.py" "\$@"
EOF
cat > "$ROOT/usr/bin/face-unlock-ctl" <<EOF
#!/bin/sh
exec "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/src/face_ctl.py" "\$@"
EOF
cat > "$ROOT/usr/bin/face-unlock-enroll" <<EOF
#!/bin/sh
exec "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/src/face_enroll.py" "\$@"
EOF
cat > "$ROOT/usr/bin/face-unlock-selftest" <<EOF
#!/bin/sh
# 装完自己验一遍：判定逻辑与机制层都不需要人坐在镜头前
PY="$INSTALL_DIR/.venv/bin/python"
echo "== 判定逻辑 =="; "\$PY" "$INSTALL_DIR/tests/logic_test.py" | tail -3
echo "== 机制自测 =="; "\$PY" "$INSTALL_DIR/tests/selftest.py" | grep -E "^通过"
echo "== 自动登录改写逻辑 =="; "\$PY" "$INSTALL_DIR/tests/autologin_test.py" | tail -3
echo
echo "真人相关（录脸相似度、非本人拒绝）请打开 face-unlock-gui 的「录入人脸」与「测一次相似度」"
EOF
cat > "$ROOT/usr/bin/face-unlock-migrate" <<EOF
#!/bin/sh
# 把老的「手工安装」留下的用户态文件让位给 deb：单元改由 /usr/lib 提供，代码改由 /opt 提供。
# 配置、人脸模板、登录密码一律不动。
set -u
echo "将清理你账号下的手工安装残留："
for p in \$HOME/.config/systemd/user/face-unlockd.service \
         \$HOME/.config/systemd/user/face-lock-at-boot.service; do
  [ -e "\$p" ] && { rm -f "\$p"; echo "  删除 \$p"; }
done
systemctl --user daemon-reload

# 只 enable，绝不加 --now：face-lock-at-boot 是 oneshot，它的作用是「锁屏」，
# --now 会让它立刻执行 —— 也就是迁移过程中当场把用户的屏幕锁上。
systemctl --user enable face-unlockd.service face-lock-at-boot.service 2>&1 | sed 's/^/  /'

# 守护进程是常驻单元，enable 对已在运行的实例不生效，必须显式 restart 才会切到新代码。
systemctl --user restart face-unlockd.service
sleep 1
echo "  守护进程现在跑的是："
ps -o args= -p "\$(systemctl --user show face-unlockd.service -p MainPID --value)" 2>/dev/null | sed 's/^/    /'

[ -d \$HOME/.local/lib/face-unlock ] && cat <<'EOT'
  老代码仍在 \$HOME/.local/lib/face-unlock，确认新版可用后可删：
    rm -rf ~/.local/lib/face-unlock ~/.local/bin/face-ctl ~/.local/bin/face-enroll
EOT
echo "完成。跑 face-unlock-ctl status 复查。"
EOF
chmod 755 "$ROOT/usr/bin/"*
ok "命令入口: $(ls "$ROOT/usr/bin" | tr '\n' ' ')"
cp -a packaging/data/usr/share/applications/. "$ROOT/usr/share/applications/"
mkdir -p "$ROOT/usr/share/icons/hicolor/256x256/apps"
cp -a packaging/data/usr/share/icons/hicolor/256x256/apps/. "$ROOT/usr/share/icons/hicolor/256x256/apps/"
cp packaging/data/usr/share/polkit-1/actions/com.qoder.faceunlock.policy \
   "$ROOT/usr/share/polkit-1/actions/"
cp README.md "$ROOT/usr/share/doc/face-unlock/README"
gzip -9n "$ROOT/usr/share/doc/face-unlock/README" 2>/dev/null || true
SIZE=$(du -sx --block-size=1 "$ROOT" | cut -f1)
ok "文件树就绪，未压缩体积 $SIZE 字节"

# ---------------------------------------------------------------- 3. control
step "3/5 生成维护者脚本"
# polkit 在 24.04 起改名为 polkitd，policykit-1 变成 transitional；两个都写进依赖或装不上
cat > "$ROOT/DEBIAN/control" <<EOF
Package: face-unlock
Version: $VERSION
Section: utils
Priority: optional
Architecture: $ARCH
Installed-Size: $((SIZE / 1024))
Depends: python3 (>= 3.10~), python3-venv, python3-gi, gir1.2-gtk-3.0,
 xdotool, dbus-x11, x11-xserver-utils, policykit-1 | polkitd
Recommends: adwaita-icon-theme
Maintainer: face-unlock local build <noreply@localhost>
Description: Camera face unlock for the GNOME lock screen (X11 sessions)
  Boots into the desktop, locks the screen, then unlocks it by recognizing the
  enrolled face through the webcam. Self-contained: ships its own OpenCV and
  ONNX models, so it does not depend on the distribution OpenCV version.
 .
 Requires an X11 session. On a Wayland session the unlock part is refused by
 design, because GNOME's Wayland lock screen does not accept injected keystrokes.
 .
 Face recognition from a 2D webcam is a convenience gate, not a security boundary:
 it can be fooled by a video of you, and the login password is stored on disk.
EOF

cat > "$ROOT/DEBIAN/postinst" <<'EOF'
#!/bin/sh
# postinst 以 root 运行。这里只做一件事：用随包分发的 wheel 离线建 venv。
# 全程 --no-index，绝不联网；建不出来就明确失败，不能留一个"装好了但跑不了"的状态。
set -e
INSTALL_DIR=/opt/face-unlock
PY=$(command -v python3)
if [ ! -x "$PY" ]; then echo "postinst: 没有 python3" >&2; exit 1; fi

if [ ! -x "$INSTALL_DIR/.venv/bin/python" ] || \
   ! "$INSTALL_DIR/.venv/bin/python" -c 'import cv2' >/dev/null 2>&1; then
  rm -rf "$INSTALL_DIR/.venv"
  echo "postinst: 创建 venv（--system-site-packages，为了看得见发行版的 gi/PyGObject）"
  "$PY" -m venv --system-site-packages "$INSTALL_DIR/.venv"
  "$INSTALL_DIR/.venv/bin/python" -m pip install --quiet --no-index \
    --find-links "$INSTALL_DIR/wheels" opencv-python-headless numpy
fi

"$INSTALL_DIR/.venv/bin/python" - <<'PYCHK'
import sys, cv2
v = tuple(int(x) for x in cv2.__version__.split(".")[:2])
# 只接受验证过的区间：>=4.6 才认 YuNet，5.x 没测过就明确拒绝而不是蒙着装
assert (4, 6) <= v < (5, 0), (
    f"venv cv2 {cv2.__version__} 不在已验证区间 [4.6, 5.0)；"
    f"太旧加载不了 YuNet，太新未经测试")
assert hasattr(cv2, "FaceDetectorYN") and hasattr(cv2, "FaceRecognizerSF")
import gi; gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GdkPixbuf   # 验证 venv 能看见系统 gi
print(f"postinst 自检通过：cv2 {cv2.__version__}，GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}")
PYCHK

chmod 700 "$INSTALL_DIR/priv/set_autologin.py" 2>/dev/null || true
chmod 755 "$INSTALL_DIR/priv" 2>/dev/null || true
echo "postinst: 完成。用户侧还需在桌面里运行 face-unlock-gui 完成录脸与启用服务。"
exit 0
EOF

cat > "$ROOT/DEBIAN/prerm" <<'EOF'
#!/bin/sh
# 卸载前没法替每个用户停服务（用户级服务归各自 session 管），只能提示。
echo "提示：每个用过本软件的用户请各自执行："
echo "  systemctl --user disable --now face-unlockd.service face-lock-at-boot.service"
echo "人脸模板与登录密码在 ~/.local/share/face-unlock/，卸载不会自动删除，请自行清理："
echo "  rm -rf ~/.local/share/face-unlock"
exit 0
EOF

cat > "$ROOT/DEBIAN/postrm" <<'EOF'
#!/bin/sh
if [ "$1" = "purge" ] || [ "$1" = "remove" ]; then
  rm -rf /opt/face-unlock/.venv 2>/dev/null || true
fi
exit 0
EOF
chmod 755 "$ROOT/DEBIAN/postinst" "$ROOT/DEBIAN/prerm" "$ROOT/DEBIAN/postrm"
ok "control / postinst / prerm / postrm"

# ---------------------------------------------------------------- 4. 打包
step "4/5 打包"
mkdir -p "$OUT"
NAME="face-unlock_${VERSION}_${RELEASE}_${ARCH}.deb"
dpkg-deb --root-owner-group -Zxz --build "$ROOT" "$OUT/$NAME" >/dev/null
[ -f "$OUT/$NAME" ] || die "dpkg-deb 没产出文件"
ok "$OUT/$NAME  $(du -h "$OUT/$NAME" | cut -f1)"

# ---------------------------------------------------------------- 5. 校验
step "5/5 校验"
dpkg-deb -I "$OUT/$NAME" >/tmp/deb-meta.txt 2>&1 && ok "元数据可读" || bad "元数据有问题"
grep -E "^[[:space:]]*(Package|Version|Architecture|Installed-Size):" /tmp/deb-meta.txt | sed 's/^/    /' || true   # dpkg-deb -I 的字段行可能带前导空格
echo "    维护脚本: $(grep -oE "(postinst|prerm|postrm)" /tmp/deb-meta.txt | sort -u | tr '\n' ' ')"
dpkg-deb -c "$OUT/$NAME" > /tmp/deb-files.txt || bad "无法列出包内容"
for want in "$INSTALL_DIR/models/sface.onnx" "$INSTALL_DIR/src/facelib.py" \
            "$INSTALL_DIR/gui/face_unlock_gui.py" "$INSTALL_DIR/priv/set_autologin.py" \
            "usr/lib/systemd/user/face-unlockd.service" \
            "usr/share/polkit-1/actions/com.qoder.faceunlock.policy" \
            "usr/share/applications/face-unlock.desktop" "usr/bin/face-unlock-gui"; do
  # 包内路径是 ./opt/... ，want 不能再带前导 /，否则拼成 .//opt/... 永远查不到
  pat=$(echo "$want" | sed 's|^/*||')
  grep -q "\./$pat" /tmp/deb-files.txt && ok "含 $pat" || bad "缺 $pat"
done
grep -q "wheels/opencv" /tmp/deb-files.txt && ok "含离线 wheel" || bad "缺 wheel —— 装的时候建不出 venv"
if grep -q "opencv_python_headless-5\." /tmp/deb-files.txt; then
  bad "包里是未验证的 OpenCV 5.x —— 必须是钉住的 4.11.0.86"; FAILS=1
fi
echo "  文件总数: $(wc -l < /tmp/deb-files.txt)"
printf '\n%s\n' "构建完成：$OUT/$NAME"
echo "安装（需要 root，装时不联网）：  sudo apt install ./packaging/out/$NAME"
echo "卸载：                          sudo apt remove face-unlock"
