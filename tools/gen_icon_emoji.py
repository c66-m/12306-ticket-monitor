from PIL import Image, ImageDraw, ImageFont

S = 512
grad = Image.new("RGB", (S, S))
d = ImageDraw.Draw(grad)
for y in range(S):
    t = y / (S - 1)
    r = int(76 + (31 - 76) * t); g = int(139 + (85 - 139) * t); b = int(255 + (224 - 255) * t)
    d.line([(0, y), (S, y)], fill=(r, g, b))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.235), fill=255)
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
img.paste(grad, (0, 0), mask)
hl = Image.new("L", (S, S), 0)
ImageDraw.Draw(hl).rounded_rectangle([int(S*0.055), int(S*0.04), int(S*0.945), int(S*0.44)], radius=int(S*0.2), fill=64)
img.paste(Image.new("RGBA", (S, S), (255, 255, 255, 255)), (0, 0), hl)
f = ImageFont.truetype("C:/Windows/Fonts/seguiemj.ttf", int(S * 0.60))
em = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(em).text((S/2, S/2 + int(S*0.035)), "\U0001F684", font=f, anchor="mm", embedded_color=True)
img = Image.alpha_composite(img, em)
img.save(r"C:\develep\产物\12306\electron\icon.ico", sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
img.resize((256,256), Image.LANCZOS).save(r"C:\develep\产物\12306\_icon_preview.png")
print("ok", img.size)
