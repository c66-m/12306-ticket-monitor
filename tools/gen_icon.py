from PIL import Image, ImageDraw, ImageFilter
import os

SRC = r"C:\Users\Admini\.dsh\attachments\v1\objects\32\3201cefa709b4b13ab1561e46c82747ecfd0c0a368d9c8d05de5bd665af0939a"
DEST = r"C:\develep\产物\12306\electron"

im = Image.open(SRC).convert("RGBA")
N = 1024
r = int(N * 0.215)
mask = Image.new("L", (N, N), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, N - 1, N - 1], radius=r, fill=255)
mask = mask.filter(ImageFilter.GaussianBlur(1.2))
im.putalpha(mask)

# 主基准 512（保留细节），各尺寸独立重采样；小尺寸轻微锐化提升可辨识度
master = im.resize((512, 512), Image.LANCZOS)

def at(s):
    t = im.resize((s, s), Image.LANCZOS)
    if s <= 48:
        t = t.filter(ImageFilter.UnsharpMask(radius=1.0, percent=90, threshold=2))
    return t

sizes = [16, 24, 32, 48, 64, 128, 256]
imgs = [at(s) for s in sizes]
out = os.path.join(DEST, "icon.ico")
imgs[-1].save(out, format="ICO", sizes=[(i.width, i.height) for i in imgs], append_images=imgs[:-1])
print("ico", os.path.getsize(out), "bytes", sorted(Image.open(out).ico.sizes()))

# 预览：上排白底、下排深底，含 4x 放大的小尺寸
sheet = Image.new("RGBA", (700, 380), (255, 255, 255, 255))
ImageDraw.Draw(sheet).rectangle([0, 190, 700, 380], fill=(32, 34, 42, 255))
for row, (bg, off) in enumerate([((255,255,255), 0), ((32,34,42), 190)]):
    x = 20
    for s in (128, 64, 48, 32, 24, 16):
        t = at(s)
        box = t if s > 32 else t.resize((s * 3, s * 3), Image.LANCZOS)
        sheet.alpha_composite(box, (x, off + 20 + (128 - box.height) // 2))
        x += box.width + 16
sheet.save(r"C:\develep\产物\12306\_icon_preview.png")
print("preview ok")
