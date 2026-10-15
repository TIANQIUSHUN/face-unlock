#!/usr/bin/env python3
"""priv/set_autologin.py 里 edit() 纯函数的单测。

不需要 root、不碰真文件——edit() 只接受字符串并返回字符串，所以全部行为都能在这里钉死。
重点覆盖：幂等、关闭时清掉 AutomaticLogin、段缺失时补建、以及**只改 [daemon] 段**这个
作用域边界（尤其 AutomaticLogin 恰好是段末最后一行时，插入新键后 end 没跟着走会写出重复行）。
"""

import configparser
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "priv"))
import set_autologin as sa

FAILS = []
PASS = []


def ck(name, cond, extra=""):
    if cond:
        PASS.append(name)
        print(f"  通过  {name}")
    else:
        FAILS.append(name)
        print(f"  失败  {name}   -> {extra}")


def daemon(out):
    cp = configparser.ConfigParser()
    cp.read_string(out)
    return dict(cp.items("daemon")) if cp.has_section("daemon") else {}


def live_lines(out, key):
    """未被注释掉的 key=... 行（大小写不敏感）。"""
    n = 0
    for l in out.splitlines():
        s = l.strip()
        if s.startswith("#"):
            continue
        if s.lower().startswith(key.lower() + "="):
            n += 1
    return n


BASE = """# comment
[daemon]
WaylandEnable=false
AutomaticLoginEnable=true
AutomaticLogin=alice
[security]
[xdmcp]
[chooser]
"""

print("== 幂等 ==")
o, c = sa.edit(BASE, True, "alice")
ck("已开启时再开启不产生变更", c == [], c)
ck("幂等不重复插行", live_lines(o, "AutomaticLogin") == 1, o)

print("\n== 关闭 ==")
o, c = sa.edit(BASE, False, "alice")
d = daemon(o)
ck("AutomaticLoginEnable 变 false", d.get("automaticloginenable") == "false", d)
ck("AutomaticLogin 被注释而非删除", "# AutomaticLogin=alice" in o, o)
ck("关闭后不再有生效的 AutomaticLogin 行", live_lines(o, "AutomaticLogin") == 0, o)
ck("WaylandEnable 原样保留（边界：不碰它）", "WaylandEnable=false" in o, o)
ck("[security]/[xdmcp]/[chooser] 段都还在",
   all(s in o for s in ("[security]", "[xdmcp]", "[chooser]")), o)

print("\n== 原本没有这两行 ==")
o, c = sa.edit("[daemon]\nWaylandEnable=false\n", True, "bob")
d = daemon(o)
ck("新增 Enable=true", d.get("automaticloginenable") == "true", o)
ck("新增 Login=bob", d.get("automaticlogin") == "bob", o)
ck("插入位置在 [daemon] 段内", o.index("AutomaticLoginEnable") < o.index("[daemon]") + 200, o)

print("\n== [daemon] 段整个缺失 ==")
o, c = sa.edit("[security]\n[xdmcp]\n", True, "bob")
d = daemon(o)
ck("自动补建 [daemon]", o.lstrip().startswith("[daemon]") or "[daemon]" in o, o)
ck("补建后写入成功", d.get("automaticlogin") == "bob", o)
ck("原有段没丢", "[security]" in o and "[xdmcp]" in o, o)

print("\n== 段作用域边界 ==")
src = "[daemon]\nAutomaticLoginEnable=true\nAutomaticLogin=alice\n\n[security]\nAutomaticLogin=HACKER\n"
o, c = sa.edit(src, False, "alice")
ck("[security] 段里的同名行不被误改", "AutomaticLogin=HACKER" in o, o)
ck("[daemon] 段里的 Enable 被改", daemon(o).get("automaticloginenable") == "false", o)

# 这是修 end 边界那个 bug 的回归用例：AutomaticLogin 正好是段末最后一行
src2 = "[other]\nx=1\n\n[daemon]\nAutomaticLoginEnable=true\nAutomaticLogin=alice\n"
o, c = sa.edit(src2, True, "alice")
ck("段末最后一行也能被命中（不写出重复键）", live_lines(o, "AutomaticLogin") == 1, o)
ck("且确实改成了新用户", daemon(o).get("automaticlogin") == "alice", o)

print("\n== 其它段在前时 [daemon] 定位 ==")
o, c = sa.edit("[chooser]\nfoo=bar\n[daemon]\nAutomaticLoginEnable=false\n", True, "zoe")
ck("定位到第二个 [daemon] 而非开头", daemon(o).get("automaticlogin") == "zoe", o)
ck("[chooser] 内容未受影响", "foo=bar" in o, o)

print("\n" + "=" * 54)
print(f"通过 {len(PASS)}  失败 {len(FAILS)}")
if FAILS:
    for f in FAILS:
        print("   - " + f)
print("=" * 54)
raise SystemExit(1 if FAILS else 0)
