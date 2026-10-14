"""설치 프로그램과 exe 에 쓸 아이콘(app.ico)을 그린다. (빌드할 때 자동 실행)"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw


def draw(size):
    s = size * 4  # 크게 그린 뒤 줄여서 테두리를 부드럽게
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    pad = s // 16
    d.rounded_rectangle([pad, pad, s - pad, s - pad], radius=s // 5, fill=(42, 120, 214, 255))
    # 겹친 사진 두 장 (중복)
    w, h = int(s * 0.46), int(s * 0.36)
    for dx, dy, col in ((-0.08, -0.08, (190, 214, 245, 255)), (0.06, 0.06, (255, 255, 255, 255))):
        x0, y0 = int(s * (0.5 + dx) - w / 2), int(s * (0.5 + dy) - h / 2)
        d.rounded_rectangle([x0, y0, x0 + w, y0 + h], radius=s // 28, fill=col)
    # 앞 사진 속 산과 해
    x0, y0 = int(s * 0.56 - w / 2), int(s * 0.56 - h / 2)
    d.polygon([(x0 + w * 0.08, y0 + h * 0.88), (x0 + w * 0.40, y0 + h * 0.40),
               (x0 + w * 0.62, y0 + h * 0.70), (x0 + w * 0.75, y0 + h * 0.55),
               (x0 + w * 0.92, y0 + h * 0.88)], fill=(42, 120, 214, 255))
    r = w * 0.08
    cx, cy = x0 + w * 0.76, y0 + h * 0.28
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(237, 161, 0, 255))
    return im.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).with_name("app.ico"))
    sizes = [16, 24, 32, 48, 64, 128, 256]
    big = draw(256)
    big.save(out, format="ICO", sizes=[(n, n) for n in sizes],
             append_images=[draw(n) for n in sizes[:-1]])
    big.save(out.with_suffix(".png"))
    print("icon:", out)
