# face-unlock

用摄像头给 GNOME 锁屏做无感解锁的 Ubuntu 桌面工具（**仅 X11 会话**）。

开机自动登录进桌面 → 屏幕自动锁上 → 你坐到镜头前，认出你 → 屏幕自己解开，全程不碰键盘。
认不出来时正常输密码，密码通道没有被削弱。

同时提供命令行（`face-unlock-ctl`）和图形界面（`face-unlock-gui`：实时预览录脸、开关、
参数调节、日志查看）。

---

## 它是怎么工作的

```
开机 ──► GDM 自动登录 ──► GNOME 会话 ──► face-lock-at-boot.service
                                                    │ 8 秒后 org.gnome.ScreenSaver.Lock
                                                    ▼
                                          屏幕锁定（时钟页）
                                                    │
                              face-unlockd.service（用户级，常驻）
                                                    │ 每 0.3s 查一次 GetActive
                                                    ▼
                            锁着 + 屏幕亮 才开摄像头扫描；锁着但屏幕灭 -> 只轻量查 xset
                                                    │  （动一下鼠标，0.25s 内就开窗）
                                                    ▼
                     YuNet 检脸 → 5 点对齐 → SFace 出 128 维特征 → 与模板算余弦
                                                    │
                              连续 4 帧都 >= 阈值，且画面不是完全静止
                                                    ▼
                              再确认一次仍处于锁定 → xdotool 注入密码 → 桌面
```

扫描的触发条件是**「锁定 且 亮屏」两个门同时为真**，而不是「从锁屏那一刻起倒计时」。
早先的版本只按锁屏计时：扫 25 秒没人就关摄像头退避 60 秒，结果息屏一段时间后回来点鼠标，
约 71% 的概率正撞在那 61 秒的摄像头关闭空窗里（当天日志：462 次心跳中 451 次是
「画面里没有人脸」）。改成亮屏事件驱动后，死区从最多 85 秒变成「亮屏延迟 + 约 1 秒」，
实测反应延迟 150ms。

## 快速开始

### 方式一：自包含 .deb

```bash
./packaging/build-deb.sh --release jammy     # 22.04 / Python 3.10
./packaging/build-deb.sh --release noble     # 24.04 / Python 3.12
sudo apt install ./packaging/out/face-unlock_*_jammy_amd64.deb
face-unlock-selftest                         # 装完自检，不需要人坐镜头前
face-unlock-gui                              # 录脸、开开关、调参数
```

包内自带 venv 与 `opencv-python-headless` + `numpy` 的 wheel，postinst 用 `--no-index`
离线安装，**装的时候不联网，也完全不依赖发行版 OpenCV 的版本**。按 Ubuntu 版本分别出包，
因为 venv 与 Python 小版本绑定、wheel 与 cp3xx ABI 绑定。

### 方式二：手工装到用户目录（只有 apt 那一步需要 root）

```bash
bash install.sh --check      # 先体检，不装任何东西
bash install.sh
face-enroll                  # 录脸
face-ctl set-password        # 写入登录密码
```

装在 `~/.local/lib/face-unlock`，不碰 `/etc`。两种方式之间可以互相迁移，见「迁移」。

## 关键实现决定

这些是踩过之后写下来的，改动前建议先读：

| 决定 | 原因 |
|---|---|
| 识别脚本完全 headless | 普通用户能直接读 `/dev/video0`（有 `user:$USER:rw-` 的 ACL），抓帧不需要 X。只有注入密码那一下才碰 X，所以不需要 root、也不需要解析 GDM 的 X 授权 |
| 必须用 OpenCV ≥ 4.6，且用 venv 隔离 | Ubuntu 22.04 自带的 4.5.4 **加载不了 YuNet 的任何版本导出**：2022mar 过不了它的 onnx 导入器（`-210`），2023mar 能建网络但 `detect()` 报 `-204 Layer with requested id=-1 not found`。用 venv 是为了不污染系统里给点云/ROS 用的 cv2 |
| 检测输入宽高必须 32 对齐 | 720 拉不进 32 的倍数时，YuNet 的 FPN 融合层会 shape 断言失败（`[1 64 45 80]` vs `[1 64 44 80]`）。`facelib.align32()` 负责拉齐，别绕过它 |
| face 向量列序是 `x,y,w,h + 5×landmark + score` | 和 OpenCV 文档写的「score 在第 5 个」**不一致**。实测证据：按文档排布时第 5 个 landmark 的 y 坐标是 0.916；而且按文档排布喂给 `alignCrop`，裁出来的图里连人脸都检不出。`check_face_layout()` 会在首次检测时把这类错位变成显式异常 |
| 亮屏门控**必须 fail-open** | 读不到 DPMS 状态、或 DPMS 未启用时，一律当作「亮着」照常扫。读不到就不扫会让门禁静默失效，比死区严重得多 |
| 亮屏状态用 `xset q` 轮询 | 实测单次耗时 <1ms，`xset dpms force off` 能读到 `Monitor is Off`、唤醒后回 `On`。鼠标一动 DPMS 就翻转，这就是「用户回来了」的可靠信号，不需要 root，也不需要 `xprintidle` |
| X 环境靠发现，不靠继承 | systemd 用户服务不继承 `DISPLAY`。代码从 `/tmp/.X11-unix/X*` 取编号、从运行中的 Xorg 命令行取 `-auth` 路径（GDM 走 `-displayfd`，命令行里没有显式的 `:0`） |
| 锁状态查 `org.gnome.ScreenSaver.GetActive` | 实测在 GNOME / X11 下可用，返回布尔。不需要 root，也不需要猜 session id |
| venv 必须 `--system-site-packages` | PyGObject 的 `gi` 不是 pip 包，普通 venv 看不见它，GUI 会起不来。postinst 自检里显式 `import gi` 验证这一点 |
| 实时预览用 `GdkPixbuf.Pixbuf.new_from_bytes` | 直接吃 cv2 的帧，所以 headless 版 OpenCV 够用，不需要 GUI 构建 |

## 安全边界

**这是便利设施，不是安全边界。** 两点必须清楚：

**一、登录密码会以明文存在 `~/.local/share/face-unlock/secret`（0600）。** 自动解锁必须由
脚本替你敲密码，这一步绕不过去。如果磁盘没有全盘加密，任何能物理接触机器、愿意插 U 盘
启动的人都能读走它 —— 和读走 home 目录下任何文件一样容易。要挡这类攻击，正确做法是先上
LUKS 全盘加密，而不是换更强的人脸模型。

**二、2D 人脸识别不是活体检测。** 代码里的「连续 4 帧 + 要求画面有轻微变化」只能挡
「把一张照片完全不动地贴在镜头前」，挡不住手机里播放的视频，也挡不住举着照片微微晃动。

所以它的定位是：**挡陌生人、挡同事随手翻你的电脑、挡自己忘了锁屏**。

实现层面还有几条硬约束被刻意保住：

- 屏幕没锁时**绝不发送任何按键**（否则按键会落进正在使用的窗口，等于往编辑器里打字）。
  发键前会二次确认 `GetActive==true`。
- 不使用 `ctrl+a` / `Delete` 这类会清空焦点窗口内容的按键；重试依赖 GNOME 在密码错误后
  自行清空输入框。
- 单窗内最多注入 `MAX_ATTEMPTS` 次，且**只有「真的注入过密码却没解开」才退避**；
  「没看见人」「熄屏」「窗口到期」都不算失败。
- 提权助手只做一件事（改 GDM 自动登录那两行），受益用户取自 `PKEXEC_UID` 而不是
  命令行自称 —— 否则任何本地进程都能拿它给别的账号开自动登录。

## 配置

`~/.config/face-unlock/config`。`ENABLED` 是热加载的（2 秒内生效，`face-unlock-ctl on/off`
不需要重启服务）；其它键在守护进程启动时绑定，改完要 `face-unlock-ctl restart`。

| 键 | 默认 | 说明 |
|---|---|---|
| `THRESHOLD` | 0.42 | 余弦相似度门槛。SFace 官方推荐值是 0.363，这里提高是为了少放人 |
| `STREAK` / `STREAK_WINDOW_S` | 4 / 2.5 | 需要连续几帧匹配、且必须落在这个时间窗内 |
| `ANTI_SPOOF` | 1 | 要求确认窗内画面有轻微变化 |
| `MIN_FACE_PX` | 80 | 人脸框短边最小像素。调小允许坐更远，但特征质量下降 |
| `SCAN_ONLY_WHEN_SCREEN_ON` | 1 | 熄屏时不扫描。设 0 则锁屏期间不分亮灭一直扫 |
| `WAKE_POLL_S` | 0.25 | 熄屏期间查亮屏的间隔，即「鼠标动完到开始扫」的延迟 |
| `SCAN_WINDOW_S` | 600 | 单次连续扫描窗上限，到期**无缝重开、不算失败** |
| `MAX_ATTEMPTS` / `BACKOFF_S` | 3 / 60 | 单窗最多注入几次；只有注入失败才退避 |
| `LOCK_SETTLE_S` | 1.2 | 开窗前等待，避免抓到锁屏/亮屏瞬间的残影 |

GNOME 侧的两个事实需要知道：**录脸时画面里只能有一张脸**（身后的海报、手机屏幕、镜子里的
人像会让程序拒绝判定归属，实测出现过「40 帧全 1 张脸」和「某几帧突然 2 张脸」两种情况）；
录完的样本拼图在 `~/.local/share/face-unlock/enroll_preview.jpg`，打开确认抓到的是正脸。

调到什么值合适只有本人的数据能回答，用 `face-unlock-ctl tune`（需要两个目标：本人，以及
一个不是本人的人或手机里的一张本人照片）。

## 验证

```bash
face-unlock-selftest                   # 包内自检（判定逻辑 + 机制 + 提权改写）
.venv/bin/python tests/gui_test.py     # GUI：结构可达性、启动不得写系统、逐页非空
.venv/bin/python tests/flow_test.py    # 真人端到端（需要镜头前有人）
```

| 测试 | 条数 | 覆盖 |
|---|---|---|
| `logic_test.py` | 43 | 屏幕没锁时零按键、注入前二次确认、多人画面不放行、低于阈值不放行、完全静止画面不放行、错密次数封顶、摄像头异常不崩、`pause` 止血、慢速凑帧不计入窗、**熄屏不开摄像头、扫描中途熄屏收窗、读不到 DPMS 时 fail-open、关掉亮屏门控后熄屏也扫** |
| `selftest.py` | 31 | 尺寸 32 对齐、模板与密码文件权限、face 向量列序（含「把 score 放回 col4 必须被抓出」）、X 环境发现、锁状态通道、无脸降级 |
| `gui_test.py` | 16 | 启动期间零系统写操作、页面可达性与非零分配尺寸、手动切换仍生效、逐页像素方差非空 |
| `autologin_test.py` | 19 | `[daemon]` 段改写的幂等、作用域、段末边界、注释语义（纯函数，不需要 root） |

三条值得单说的验证经验：

- **回归测试要证明自己抓得住 bug。** 修 GtkStack 那个问题时，把修复临时撤掉确认测试报错、
  再恢复。否则测试通过说明不了什么。
- **GUI 那条「像素方差」检查有局限**：它只能发现「整窗全空」。若某个容器没挂进布局而标签栏
  还在，方差依然很高 —— 那种情况由「可达性 + 分配尺寸」那条负责。这段局限写在测试注释里。
- **测试不能依赖测试机的物理状态。** 亮屏判定默认打桩成「永远亮着」，否则屏幕一灭就有 9 条
  断言一起失败，看起来像产品坏了。要测真实路径必须显式传 `monitor="real"`。

## 已实测 / 未实测

**已实测**：jammy 包构建；解到临时根后离线执行 postinst 建 venv（`cv2 4.11.0` + `import gi`）；
包内代码跑全部自动化测试；GUI 四页离屏渲染；包内运行时开摄像头取帧与检测；真实
`xset dpms force off/on` 的灭→亮被 150ms 内捕获；锁屏→认脸→注入密码的完整链路在真机上
跑过数十次。

**未实测**：noble / oracular / resolute 的包（需要对应 `cp312` / `cp313` 的 wheel 与真机）；
pkexec 认证对话框的实际弹出；Wayland 会话下的拒绝路径；以及最重要的一条 —— **非本人会不会
被放行**。最后这条只有真人能测：自动化数据里所有样本都是同一个人，只能证明「不会拒你自己」。

## 已知限制

- **只在 X11 下成立。** Wayland 会话里 GNOME 的锁屏不接受其它进程注入按键，`xset` 也读不到
  DPMS 状态。程序会显式拒绝解锁功能并在界面顶部说明原因，**不会**去改 `WaylandEnable` ——
  那会牵连同机的其它用户和别的软件。要换到 X11：注销后在登录页右下角齿轮选
  「Ubuntu on Xorg」。
- **依赖 GDM 自动登录开着。** 守护进程是用户级服务，只有图形会话建立之后才存在。关掉自动
  登录后，开机停在登录页那一刻没有任何组件在跑，必须先用密码登录，人脸只从之后的锁屏开始
  接管。（让登录页也走人脸是另一套做法，不在本项目范围内。）
- **只管 GNOME 锁屏**，不管 GDM 登录页、BIOS/UEFI 和 GRUB。
- 摄像头被别的程序占用时扫不了，会明确报「读帧失败」并退避，不会静默假装成功。
- 戴厚帽子、口罩、大角度侧脸、逆光都可能被拒 —— 兜底是密码，而不是把阈值调低。
- 锁屏且屏幕亮着时摄像头灯会长亮、约 18% 单核。熄屏即停扫，所以自然离开时负担很短；但手动
  锁屏后走开、GNOME 又还没熄屏的那段会一直扫。

## 迁移（手工安装 ↔ deb）

装过手工版再装 deb 时，用户目录的单元文件优先级高于包安装的 `/usr/lib/systemd/user/`，
需要让位：

```bash
face-unlock-migrate     # enable 两个单元 + 显式 restart 守护进程
rm -rf ~/.local/lib/face-unlock ~/.local/bin/face-ctl ~/.local/bin/face-enroll
```

注意 `face-unlock-migrate` **不加 `--now`** —— `face-lock-at-boot` 是 oneshot、作用是锁屏，
`--now` 会让它在迁移过程中当场把屏幕锁上。

迁移后命令改名：`face-ctl` → `face-unlock-ctl`，`face-enroll` → `face-unlock-enroll`，
GUI 为 `face-unlock-gui`。配置路径、人脸模板、密码文件都不变。

卸载：`sudo apt remove face-unlock`（或手工版 `bash uninstall.sh`）。两者都不会自动删除
`~/.local/share/face-unlock/` 下的模板与密码，需要自己清。

## 文件结构

```
src/facelib.py           检测/对齐/特征/比对 + 尺寸与列序校验
src/common.py            配置、路径、锁状态、X 环境发现、DPMS 读取、权限
src/backend.py           GUI 与 CLI 共用的操作层（会话判定、参数校验、提权调用、录脸器）
src/face_unlockd.py      守护进程：亮屏事件驱动扫描 + 注入
src/face_ctl.py          命令行：status / test / tune / lock / pause / logs
src/face_enroll.py       命令行录脸
src/lock-at-boot.sh      开机后把屏幕锁上
gui/face_unlock_gui.py   GTK3 界面（含 --render-test 离屏渲染自检）
priv/set_autologin.py    pkexec 提权助手：只改 [daemon] 段那两行
systemd/*.in             两个用户级单元模板
packaging/build-deb.sh   按 release 构建自包含离线 deb
tests/                   四套自动化测试，见「验证」
```

## 第三方与许可

本项目代码以 **MIT** 发布，见 [`LICENSE`](LICENSE)。

运行时依赖（由 apt 安装）：`xdotool`、`dbus-x11`、`x11-xserver-utils`、`python3-gi` +
`gir1.2-gtk-3.0`、`policykit-1`（或 24.04 起的 `polkitd`）。

自包含包内附带的开源组件见 [`NOTICE`](NOTICE)：

- `opencv-python-headless` 4.11.0.86（Apache-2.0）
- `numpy` 2.2.6（BSD-3-Clause）
- 两个 ONNX 模型来自 [opencv_zoo](https://github.com/opencv/opencv_zoo)（Apache-2.0），
  **不提交进仓库**，由构建脚本按需下载：
  - `face_detection_yunet_2023mar.onnx`（YuNet 人脸检测）
  - `face_recognition_sface_2021dec.onnx`（SFace 人脸特征）
