#!/usr/bin/env bash
# 人脸无感解锁 —— 卸载。装了什么就拆什么，全部在用户目录，不碰 /etc。
#   bash uninstall.sh              拆服务 + 删代码 + 删密码和人脸模板（默认，安全优先）
#   bash uninstall.sh --keep-data  只拆服务和代码，保留 ~/.local/share/face-unlock
set -uo pipefail

INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/lib/face-unlock}"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
UNIT_DIR="$HOME/.config/systemd/user"
DATA_DIR="${FACE_UNLOCK_DIR:-$HOME/.local/share/face-unlock}"
CFG_DIR="$HOME/.config/face-unlock"
KEEP_DATA=0
[ "${1:-}" = "--keep-data" ] && KEEP_DATA=1

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
ok()   { printf '  [x] %s\n' "$1"; }
skip() { printf '  [ ] %s\n' "$1"; }

step "1/4 停掉并注销服务"
for u in face-unlockd.service face-lock-at-boot.service; do
  if systemctl --user is-active "$u" >/dev/null 2>&1; then
    systemctl --user stop "$u" && ok "已停止 $u"
  else
    skip "$u 未在运行"
  fi
  systemctl --user disable "$u" >/dev/null 2>&1 && ok "已注销自启 $u"
done
# 屏幕锁策略是 GNOME 自己的设置，本项目没改过，所以这里也不需要还原。
# 唯一例外：如果你之前为了测试手动执行过 gsettings，那要你自己决定还原不还原。

step "2/4 删除单元文件和命令入口"
for f in "$UNIT_DIR/face-unlockd.service" "$UNIT_DIR/face-lock-at-boot.service" \
         "$BIN_DIR/face-ctl" "$BIN_DIR/face-enroll"; do
  if [ -e "$f" ]; then rm -f "$f" && ok "删除 $f"; else skip "$f 不存在"; fi
done
systemctl --user daemon-reload 2>/dev/null && ok "systemd 用户配置已重载"

step "3/4 敏感文件"
if [ "$KEEP_DATA" = 1 ]; then
  skip "按 --keep-data 保留了 $DATA_DIR（里面有明文登录密码和人脸模板，别忘了手动删）"
else
  if [ -e "$DATA_DIR/secret" ]; then
    # 先截断再删除，降低同盘恢复的可行性（磁盘未加密时这只是聊胜于无）
    : > "$DATA_DIR/secret" 2>/dev/null || true
    shred -uzf "$DATA_DIR/secret" 2>/dev/null || rm -f "$DATA_DIR/secret"
    ok "已清除登录密码文件"
  else
    skip "没有密码文件（说明你还没执行过 face-ctl set-password）"
  fi
  [ -d "$DATA_DIR" ] && rm -rf "$DATA_DIR" && ok "已删除 $DATA_DIR（人脸模板等）"
  [ -d "$CFG_DIR" ] && rm -rf "$CFG_DIR" && ok "已删除 $CFG_DIR"
fi

step "4/4 程序本体"
if [ -d "$INSTALL_DIR" ]; then
  rm -rf "$INSTALL_DIR" && ok "已删除 $INSTALL_DIR（含 venv 和模型）"
else
  skip "$INSTALL_DIR 不存在"
fi
skip "源目录 $(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd) 保留着，确认不再需要可自行删除"

cat <<'TXT'

--------------------------------------------------------------------------
卸载完成。校验一下有没有残留：

  systemctl --user list-units --all | grep -i face          # 应无输出
  systemctl --user list-unit-files | grep -i face           # 应无输出
  ls ~/.local/share/face-unlock ~/.config/face-unlock        # 应提示不存在
  command -v face-ctl face-enroll                            # 应无输出
  getfacl -p /dev/video0 | grep -v '^#'                      # 摄像头权限应回到原样

系统包 xdotool 属于公共依赖，卸载脚本不动它。要清：sudo apt remove -y xdotool
系统 python3-opencv 从头到尾没被改动过，你原来用 cv2 做的一切照旧。

现在开机行为：自动登录后会立即锁屏 —— 如果你把 face-lock-at-boot.service 删掉了，
这个行为也随之消失，回到「开机直接进桌面」。
--------------------------------------------------------------------------
TXT
