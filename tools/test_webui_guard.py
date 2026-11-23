#!/usr/bin/env python3
"""check_webui() 的回归测试：守卫自己不能再「每更新一次上游就坏一次」。

为什么需要它
------------
2026-11 的两周里，面板预检连着坏了两次，都不是上游坏了，而是守卫把**某一版
的目录形状**当成了永久契约：

  · v3.4.1：i18n/combined/ 出现，旧写法「拿 i18n/ 下第一个子目录推域清单」
    把五种语言全部误报成缺词典；
  · v3.5.0：i18n/manifest.js 与 20 个域文件下线，UI_REQUIRED 里钉的两个路径
    直接 404，bump 卡在 [6/6]，白等一天。

修法不是再钉一遍路径，而是把判据换成「页面引用了什么，就要求什么」。但守卫
一旦改成动态推导，就更容易在下次修改时**悄悄弄坏某一种形态** —— 而那种坏法
要等下一次上游发版才暴露。所以这里用合成树把三种真实形态固定下来：

  3.3.x   manifest + 各域（无 combined）
  3.4.x   manifest + 各域 + combined
  3.5.x   无 manifest，每语言只剩 login/accounts + combined

再配负例（缺静态引用 / 缺 srcset 候选 / 缺合并词典 / 缺登录域 / 缺图标 /
未登记的根绝对路径），保证守卫不是「永远通过」。

跑法：python3 tools/test_webui_guard.py
无需网络、无需上游产物 —— 全部在临时目录里造。

注意：本文件不参与构建期文本卫生检查（tools/ 在 check_text_hygiene 的排除表里），
但它自己保持 LF / 无 BOM，和其它脚本一致。
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import build  # noqa: E402

LOCALES = ("en", "ja")


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def make_ui_tree(root, *, legacy_manifest, combined, locales=LOCALES):
    """在 root 下造一棵最小但完整的面板树（布局与 _upstream_ui_src 一致）。

    legacy_manifest=True  → 3.3/3.4 形态：i18n/manifest.js + 每语言全部域
    combined=True         → 3.4/3.5 形态：i18n/combined/<locale>.js
    """
    ui = os.path.join(root, "upstream-bundle-amd64", "ui")

    # UI_REQUIRED 里的静态文件：内容不重要，存在即可（引用闭合性会核它们）
    for rel in (
        "app.js", "icons.js", "i18n.js", "islands/ui.js",
        "css/tokens.css", "css/layout.css", "css/mobile.css",
        "manifest.webmanifest", "sw.js",
        "m/mobile.js", "m/mobile.css",
    ):
        _write(os.path.join(ui, *rel.split("/")), "/* fixture */\n")

    index_head = [
        '<!doctype html><html><head>',
        '<link rel="stylesheet" href="css/tokens.css">',
        '<link rel="stylesheet" href="css/layout.css">',
        '<script src="i18n.js"></script>',
    ]
    if combined:
        # 真实形态：运行时拼路径，静态解析必须跳过它（否则会去找一个字面量
        # 叫 `i18n/combined/' + locale + '.js` 的文件）
        index_head.append(
            "<script>document.write('<script src=\"i18n/combined/' + locale + '.js\">"
            "<\\/script>')</script>"
        )
    index_head.append("</head><body>")
    index_head.append('<script src="app.js"></script>')
    index_head.append('<script src="icons.js"></script>')
    index_head.append('<script src="islands/ui.js"></script>')
    index_head.append("</body></html>")
    _write(os.path.join(ui, "index.html"), "\n".join(index_head) + "\n")

    login = [
        '<!doctype html><html><head>',
        '<script src="i18n.js"></script>',
        "<script>document.write('<script src=\"i18n/' + locale + '/login.js\">"
        "<\\/script>')</script>",
        "<script>document.write('<script src=\"i18n/' + locale + '/accounts.js\">"
        "<\\/script>')</script>",
        "</head><body></body></html>",
    ]
    _write(os.path.join(ui, "login.html"), "\n".join(login) + "\n")

    mobile = [
        '<!doctype html><html><head>',
        '<link rel="stylesheet" href="m/mobile.css">',
        '<script src="i18n.js"></script>',
    ]
    if legacy_manifest:
        mobile.append('<script src="i18n/manifest.js"></script>')
    if combined:
        mobile.append(
            "<script>document.write('<script src=\"i18n/combined/' + locale + '.js\">"
            "<\\/script>')</script>"
        )
    mobile.append("</head><body>")
    mobile.append('<script src="m/mobile.js"></script>')
    mobile.append("</body></html>")
    _write(os.path.join(ui, "m.html"), "\n".join(mobile) + "\n")

    domains = ("common", "login", "accounts")
    if legacy_manifest:
        _write(
            os.path.join(ui, "i18n", "manifest.js"),
            "window.__WB_I18N_DOMAINS = ["
            + ", ".join('"%s"' % d for d in domains)
            + "];\n",
        )
    for loc in locales:
        per_locale = domains if legacy_manifest else ("login", "accounts")
        for dom in per_locale:
            _write(os.path.join(ui, "i18n", loc, dom + ".js"), "/* fixture */\n")
    if combined:
        for loc in locales:
            _write(os.path.join(ui, "i18n", "combined", loc + ".js"), "/* fixture */\n")

    _write(os.path.join(ui, "i18n.js"), '/* i18n runtime */\nvar t = function(k){return k};\n')
    if combined:
        # i18n.js 自带「页面没预注入就补合并词典」的兜底，所以这里出现该路径
        # 是真实形态，闭包检查必须能容忍它（它不是页面引用）
        with open(os.path.join(ui, "i18n.js"), "a", encoding="utf-8") as f:
            f.write("/* fallback: i18n/combined/<locale>.js */\n")

    # 模型图标：一张 png + 两个索引各引用一次
    _write(os.path.join(ui, "assets", "providers", "openai.png"), "png")
    _write(
        os.path.join(ui, "preset-providers.js"),
        "const PRESET = [{ id: 'openai', icon: 'openai.png' }];\n",
    )
    _write(
        os.path.join(ui, "islands", "ui.js"),
        'const PROVIDER_ICONS = { openai: "assets/providers/openai.png" };\n',
    )
    return ui


class WebuiGuardTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="webui-guard-")
        self._old_build_dir = build.BUILD_DIR
        build.BUILD_DIR = self.root

    def tearDown(self):
        build.BUILD_DIR = self._old_build_dir
        shutil.rmtree(self.root, ignore_errors=True)

    # --- 三种真实形态都必须通过 -------------------------------------------
    def _passes(self, label, **kw):
        make_ui_tree(self.root, **kw)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            build.check_webui()
        return out.getvalue()

    def test_350_shape_passes(self):
        log = self._passes("3.5", legacy_manifest=False, combined=True)
        self.assertIn("无域清单（合并词典模型）", log)
        self.assertIn("缺失 0 个", log)
        self.assertIn("被引用 1 个", log)  # 两个索引都解析到同一张图

    def test_341_shape_passes(self):
        log = self._passes("3.4", legacy_manifest=True, combined=True)
        self.assertIn("个词典域", log)

    def test_330_shape_passes(self):
        log = self._passes("3.3", legacy_manifest=True, combined=False)
        self.assertIn("个词典域", log)

    # --- 负例：每一种「页面要的东西不在」都必须失败 ------------------------
    def _fails(self, label, **kw):
        make_ui_tree(self.root, **kw)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit, msg=label):
                build.check_webui()
        return out.getvalue()

    def test_missing_static_reference_fails(self):
        make_ui_tree(self.root, legacy_manifest=False, combined=True)
        os.remove(os.path.join(self.root, "upstream-bundle-amd64", "ui", "css", "tokens.css"))
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                build.check_webui()

    def test_missing_srcset_candidate_fails(self):
        ui = make_ui_tree(self.root, legacy_manifest=False, combined=True)
        _write(os.path.join(ui, "assets", "a.png"), "png")
        # 往 index.html 里塞一个 srcset：a.png 在、missing.png 不在
        idx = os.path.join(ui, "index.html")
        with open(idx, "r", encoding="utf-8") as f:
            html = f.read()
        html = html.replace(
            "</body>",
            '<img src="assets/a.png" srcset="assets/a.png 1x, assets/missing.png 2x"></body>',
        )
        _write(idx, html)
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                build.check_webui()

    def test_missing_combined_dict_fails(self):
        make_ui_tree(self.root, legacy_manifest=False, combined=True)
        os.remove(os.path.join(self.root, "upstream-bundle-amd64", "ui", "i18n", "combined", "ja.js"))
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                build.check_webui()

    def test_missing_login_domain_fails(self):
        make_ui_tree(self.root, legacy_manifest=False, combined=True)
        os.remove(os.path.join(self.root, "upstream-bundle-amd64", "ui", "i18n", "en", "login.js"))
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                build.check_webui()

    def test_missing_icon_fails(self):
        ui = make_ui_tree(self.root, legacy_manifest=False, combined=True)
        _write(
            os.path.join(ui, "preset-providers.js"),
            "const PRESET = [{ id: 'ghost', icon: 'ghost.png' }];\n",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                build.check_webui()

    def test_unregistered_root_absolute_fails(self):
        ui = make_ui_tree(self.root, legacy_manifest=False, combined=True)
        idx = os.path.join(ui, "index.html")
        with open(idx, "r", encoding="utf-8") as f:
            html = f.read()
        _write(idx, html.replace("</head>", '<link rel="stylesheet" href="/cdn/x.css"></head>'))
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                build.check_webui()


if __name__ == "__main__":
    unittest.main(verbosity=2)

