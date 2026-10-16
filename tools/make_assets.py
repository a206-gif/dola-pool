#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成 Dola Pool 内置静态资源（可重复执行）：

1. static/bg_images/bg_01.jpg ... bg_15.jpg
   由 assets_src/bg_* 三张抽象底图裁剪 / 缩放 / 调色而来（可用任意自有图片替换）。
   前端每小时切换一张（见 app.py 的 changeBg()）。

2. static/demo/sample_9x16.mp4 与 static/demo/sample_16x9.mp4
   演示模式（DOLA_DEMO=1）下"生成"出来的示例成片，用于在没有真实
   Dola 账号 / 代理的情况下完整跑通流程。

依赖：pillow、imageio-ffmpeg（仅制作资源时需要，运行程序不需要）
    pip install pillow imageio-ffmpeg
用法：
    python tools/make_assets.py
"""
import math
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC_DIR = os.path.join(ROOT, "assets_src")
BG_DIR = os.path.join(ROOT, "static", "bg_images")
DEMO_DIR = os.path.join(ROOT, "static", "demo")

BG_COUNT = 15
BG_SIZE = (1600, 900)


def _require(mod, hint):
    try:
        return __import__(mod)
    except ImportError:
        print("[!] 缺少依赖 %s，请先安装：%s" % (mod, hint))
        sys.exit(1)


def make_backgrounds():
    """把 3 张源图变成 15 张不同取景的背景图。"""
    _require("PIL", "pip install pillow")
    from PIL import Image, ImageEnhance

    sources = sorted(
        os.path.join(SRC_DIR, f) for f in os.listdir(SRC_DIR)
        if f.lower().endswith((".png", ".jpg", ".jpeg"))
    )
    if not sources:
        print("[!] %s 下没有源图，跳过背景生成" % SRC_DIR)
        return
    os.makedirs(BG_DIR, exist_ok=True)

    made = 0
    for i in range(BG_COUNT):
        src = Image.open(sources[i % len(sources)]).convert("RGB")
        w, h = src.size
        # 每张不同：轻微放大 + 平移取景，做出"同一套视觉的不同切片"
        zoom = 1.02 + (i % 5) * 0.045
        ratio = BG_SIZE[0] / BG_SIZE[1]
        crop_w = min(w, int(h * ratio)) / zoom
        crop_h = crop_w / ratio
        ox = (w - crop_w) * ((i * 0.37) % 1.0)
        oy = (h - crop_h) * ((i * 0.61) % 1.0)
        img = src.crop((int(ox), int(oy), int(ox + crop_w), int(oy + crop_h)))
        img = img.resize(BG_SIZE, Image.LANCZOS)

        # 压暗 + 轻微降饱和，作为后台背景不抢内容
        img = ImageEnhance.Brightness(img).enhance(0.62 + (i % 3) * 0.05)
        img = ImageEnhance.Color(img).enhance(0.72)
        img = ImageEnhance.Contrast(img).enhance(1.06)

        out = os.path.join(BG_DIR, "bg_%02d.jpg" % (i + 1))
        img.save(out, "JPEG", quality=84, optimize=True, progressive=True)
        made += 1
    print("[+] 已生成 %d 张背景图 -> %s" % (made, BG_DIR))


def _frames_for(size, seconds, title, subtitle, out_dir):
    """用 PIL 画一段动画帧（渐变流动 + 光带 + 文字），交给 ffmpeg 编码。"""
    from PIL import Image, ImageDraw, ImageFont
    W, H = size
    fps = 24
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    small_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    title_font = ImageFont.truetype(font_path, max(24, W // 14)) if os.path.exists(font_path) else ImageFont.load_default()
    small_font = ImageFont.truetype(small_path, max(14, W // 34)) if os.path.exists(small_path) else ImageFont.load_default()

    paths = []
    total = int(seconds * fps)
    for f in range(total):
        t = f / total
        img = Image.new("RGB", (W, H), (8, 14, 16))
        px = img.load()
        # 垂直渐变 + 缓慢移动的青色光带
        for y in range(0, H, 3):
            wave = math.sin((y / H + t * 1.4) * math.pi * 2)
            base = int(14 + 26 * (1 - y / H) + 8 * wave)
            for x in range(0, W, 3):
                glow = math.exp(-(((x / W - (0.5 + 0.42 * math.sin(t * math.pi * 2))) ** 2) * 26))
                r = min(255, int(base * 0.55 + 40 * glow))
                g = min(255, int(base * 1.7 + 150 * glow))
                b = min(255, int(base * 1.6 + 140 * glow))
                for dy in range(3):
                    for dx in range(3):
                        if x + dx < W and y + dy < H:
                            px[x + dx, y + dy] = (r, g, b)
        d = ImageDraw.Draw(img, "RGBA")
        d.text((W * 0.08, H * 0.40), title, font=title_font, fill=(226, 245, 242, 255))
        d.text((W * 0.08, H * 0.40 + title_font.size * 1.35), subtitle, font=small_font, fill=(150, 200, 195, 255))
        # 进度条
        d.rectangle([W * 0.08, H * 0.86, W * 0.92, H * 0.86 + max(3, H // 240)], fill=(30, 60, 62, 255))
        d.rectangle([W * 0.08, H * 0.86, W * 0.08 + (W * 0.84) * t, H * 0.86 + max(3, H // 240)], fill=(12, 165, 140, 255))
        p = os.path.join(out_dir, "f%04d.png" % f)
        img.save(p)
        paths.append(p)
    return fps, paths


def make_demo_video(size, name, seconds=6, title="DOLA POOL", subtitle="演示成片 · DEMO RENDER"):
    _require("imageio_ffmpeg", "pip install imageio-ffmpeg")
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    os.makedirs(DEMO_DIR, exist_ok=True)
    out = os.path.join(DEMO_DIR, name)

    tmp = tempfile.mkdtemp(prefix="dola_frames_")
    try:
        fps, _ = _frames_for(size, seconds, title, subtitle, tmp)
        cmd = [
            ffmpeg, "-y", "-loglevel", "error",
            "-framerate", str(fps), "-i", os.path.join(tmp, "f%04d.png"),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", out,
        ]
        subprocess.run(cmd, check=True)
        print("[+] 已生成演示视频 %s (%.1f KB)" % (out, os.path.getsize(out) / 1024))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    make_backgrounds()
    make_demo_video((720, 1280), "sample_9x16.mp4")
    make_demo_video((1280, 720), "sample_16x9.mp4")
    print("[✓] 资源生成完成")


if __name__ == "__main__":
    main()
