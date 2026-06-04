# 3GPP-Everything Logo — 源母版与再生成

品牌标：`3` 与其水平镜像 `E` 组成的同源 monogram，中间连字符 `-`；
水平蓝→青渐变（`#3B82F6` → `#2DD4BF`）。APP 图标黑底，Web 用透明纯标。

## 母版 SVG（本目录，唯一源）

| 文件 | 用途 |
|---|---|
| `mark.svg` | 透明纯标。Web favicon / `Icon-192/512(any)` / 文档水印 |
| `icon-legacy.svg` | 黑底 squircle + 标。Android 旧版 mipmap `ic_launcher.png` |
| `foreground.svg` | 透明、标缩进安全区（scale 0.85）。Android 自适应前景层 |
| `monochrome.svg` | 透明纯白标。Android 13 主题图标 monochrome 层 |
| `maskable.svg` | 黑底满铺 + 标。Web PWA `Icon-maskable-192/512` |

自适应背景层 = 纯色 `@color/ic_launcher_background`（`#000000`），见
`android/.../res/values/ic_launcher_background.xml`，无需 PNG。

## 再生成全部位图（cairosvg）

```bash
cd assets/logo/final
RES=../../../frontend/android/app/src/main/res
WEB=../../../frontend/web
python3.11 - "$RES" "$WEB" <<'PY'
import sys, cairosvg
RES, WEB = sys.argv[1], sys.argv[2]
r = lambda svg,w,out: cairosvg.svg2png(url=svg, write_to=out, output_width=w, output_height=w)
LEG={"mdpi":48,"hdpi":72,"xhdpi":96,"xxhdpi":144,"xxxhdpi":192}
FG ={"mdpi":108,"hdpi":162,"xhdpi":216,"xxhdpi":324,"xxxhdpi":432}
for d in LEG:
    r("icon-legacy.svg", LEG[d], f"{RES}/mipmap-{d}/ic_launcher.png")
    r("foreground.svg",  FG[d],  f"{RES}/mipmap-{d}/ic_launcher_foreground.png")
    r("monochrome.svg",  FG[d],  f"{RES}/mipmap-{d}/ic_launcher_monochrome.png")
for n,w in [("Icon-192",192),("Icon-512",512)]:
    r("mark.svg", w, f"{WEB}/icons/{n}.png")
for n,w in [("Icon-maskable-192",192),("Icon-maskable-512",512)]:
    r("maskable.svg", w, f"{WEB}/icons/{n}.png")
r("mark.svg", 32, f"{WEB}/favicon.png")
PY
cp mark.svg "$WEB/favicon.svg"
```

> 改色/改字形只需改这 5 个 SVG（或只改 `mark.svg` 的渐变/路径，其余 4 个复用同一组 path）后重跑上面脚本。
> 用 `cairosvg`（非 headless Chrome）——Chrome 截图在本机有异步竞态/底边裁切问题。
