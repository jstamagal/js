"""Pixel-level browser probing with Playwright, screenshots, and WebGL."""

from __future__ import annotations

import functools
import http.server
import io
import json
import re
import socketserver
import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .core import Tool, ToolContext
from .descriptions import load_description
from .sanitize import int_or_default, text_or_default

CHROME_FALLBACK = Path("/opt/chrome-for-testing/chrome-linux64/chrome")
GL_ARGS = (
    "--use-gl=swiftshader",
    "--enable-unsafe-swiftshader",
    "--hide-scrollbars",
    "--mute-audio",
)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        pass


class _ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _start_local_server(path: Path) -> tuple[_ThreadingServer, str, Path]:
    root = path if path.is_dir() else path.parent
    index = "" if path.is_dir() else quote(path.name)
    handler = functools.partial(_QuietHandler, directory=str(root))
    server = _ThreadingServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/{index}", root


def _rgb_pixels(image: Any) -> Any:
    data = iter(image.tobytes())
    return zip(data, data, data, strict=True)


def _frame_metrics(png: bytes) -> tuple[dict[str, Any], Any]:
    from PIL import Image

    image = Image.open(io.BytesIO(png)).convert("RGB")
    counts = Counter(
        ((red >> 3) << 10) | ((green >> 3) << 5) | (blue >> 3)
        for red, green, blue in _rgb_pixels(image)
    )
    pixels = image.width * image.height
    return (
        {
            "dimensions": [image.width, image.height],
            "dominant_color_share": round(max(counts.values()) / pixels, 4),
            "unique_colors": len(counts),
        },
        image,
    )


def _changed_pct(before: Any, after: Any) -> float | None:
    if before is None or after is None or before.size != after.size:
        return None
    changed = 0
    total = before.width * before.height
    for left, right in zip(_rgb_pixels(before), _rgb_pixels(after), strict=True):
        if max(abs(left[channel] - right[channel]) for channel in range(3)) > 8:
            changed += 1
    return round(changed * 100.0 / total, 3)


def _largest_canvas(page: Any) -> Any | None:
    best = None
    best_area = 0.0
    for canvas in page.locator("canvas").all():
        try:
            if not canvas.is_visible():
                continue
            box = canvas.bounding_box() or {}
            area = float(box.get("width") or 0) * float(box.get("height") or 0)
            if area > best_area:
                best = canvas
                best_area = area
        except Exception:
            continue
    return best if best_area > 10_000 else None


def _safe_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:50] or "step"


def _launch_browser(playwright: Any) -> Any:
    try:
        return playwright.chromium.launch(args=list(GL_ARGS))
    except Exception as first:
        if not CHROME_FALLBACK.is_file():
            raise first
        return playwright.chromium.launch(
            executable_path=str(CHROME_FALLBACK), args=list(GL_ARGS)
        )


def _webgl_report(page: Any) -> dict[str, Any]:
    return page.evaluate(
        """
        () => {
          const result = {webgl: false, webgl2: false, renderer: null, version: null};
          for (const kind of ['webgl2', 'webgl', 'experimental-webgl']) {
            const canvas = document.createElement('canvas');
            const gl = canvas.getContext(kind);
            if (!gl) continue;
            if (kind === 'webgl2') result.webgl2 = true;
            else result.webgl = true;
            result.version ||= gl.getParameter(gl.VERSION);
            const ext = gl.getExtension('WEBGL_debug_renderer_info');
            if (ext) result.renderer ||= gl.getParameter(ext.UNMASKED_RENDERER_WEBGL);
          }
          return result;
        }
        """
    )


def browser_probe(
    target: str,
    click: str | None = "",
    press: str | None = "",
    output_dir: str | None = "",
    settle_ms: int | None = 1200,
    hold_ms: int | None = 1600,
    viewport_width: int | None = 1280,
    viewport_height: int | None = 800,
    context: ToolContext | None = None,
) -> str:
    """Render and interact with a page, preserving screenshots and visual metrics."""
    if context is None:
        return "ERROR: missing ToolContext"
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return (
            "ERROR: browser_probe requires the optional Playwright backend; "
            "install js[browser] (Playwright does not publish musllinux wheels)"
        )

    target = text_or_default(target).strip()
    if not target:
        return "ERROR: target is required"
    click = text_or_default(click)
    press = text_or_default(press)
    settle = min(int_or_default(settle_ms, 1200, minimum=0), 30_000)
    hold = min(int_or_default(hold_ms, 1600, minimum=0), 30_000)
    width = min(int_or_default(viewport_width, 1280, minimum=100), 3840)
    height = min(int_or_default(viewport_height, 800, minimum=100), 2160)

    server: _ThreadingServer | None = None
    root: Path | None = None
    url = target
    if not re.match(r"^https?://", target, flags=re.IGNORECASE):
        path = context.resolve_path(target)
        if not path.exists():
            return f"ERROR: no such local target: {path}"
        try:
            server, url, root = _start_local_server(path)
        except OSError as exc:
            return f"ERROR: could not serve local target: {type(exc).__name__}: {exc}"

    base = context.resolve_path(output_dir) if output_dir else (root or context.cwd) / "browser-probes"
    run_dir = base / f"probe-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    context.snapshot(run_dir)
    run_dir.mkdir(parents=True, exist_ok=False)

    report: dict[str, Any] = {
        "url": url,
        "output_dir": str(run_dir),
        "frames": [],
        "webgl": {},
        "clicked": [],
        "console_errors": [],
        "page_errors": [],
    }
    previous_image = None
    browser = None

    try:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page(viewport={"width": width, "height": height})
            page.on(
                "console",
                lambda message: report["console_errors"].append(message.text[:500])
                if message.type == "error"
                else None,
            )
            page.on(
                "pageerror", lambda error: report["page_errors"].append(str(error)[:500])
            )
            page.goto(url, wait_until="load", timeout=30_000)
            page.wait_for_timeout(settle)
            report["webgl"] = _webgl_report(page)

            def capture(label: str) -> None:
                nonlocal previous_image
                surface = _largest_canvas(page)
                png = surface.screenshot() if surface is not None else page.screenshot()
                path = run_dir / f"{len(report['frames']) + 1:02d}-{_safe_label(label)}.png"
                path.write_bytes(png)
                metrics, image = _frame_metrics(png)
                metrics.update(
                    {
                        "label": label,
                        "region": "canvas" if surface is not None else "page",
                        "path": str(path),
                        "changed_pct_from_previous": _changed_pct(previous_image, image),
                    }
                )
                report["frames"].append(metrics)
                previous_image = image

            capture("landing")

            for pattern in [part.strip() for part in click.split(">") if part.strip()]:
                try:
                    matcher = re.compile(pattern, re.IGNORECASE)
                except re.error as exc:
                    report["clicked"].append(
                        {"pattern": pattern, "error": f"invalid regex: {exc}"}
                    )
                    break
                hit = None
                hit_text = ""
                for element in (
                    page.get_by_role("button").all() + page.get_by_role("link").all()
                ):
                    try:
                        if not element.is_visible():
                            continue
                        text = (element.inner_text() or element.text_content() or "").strip()
                        if matcher.search(text):
                            hit = element
                            hit_text = text[:100]
                            break
                    except Exception:
                        continue
                if hit is None:
                    report["clicked"].append(
                        {"pattern": pattern, "error": "matched no visible button or link"}
                    )
                    break
                hit.click()
                page.wait_for_timeout(settle)
                report["clicked"].append({"pattern": pattern, "text": hit_text})
                capture(f"after click {pattern}")

            if press:
                page.keyboard.down(press)
                page.wait_for_timeout(hold)
                page.keyboard.up(press)
                page.wait_for_timeout(120)
                report["pressed"] = press
                capture(f"after key {press}")

            browser.close()
            browser = None
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if server is not None:
            server.shutdown()
            server.server_close()

    changed = [
        frame["changed_pct_from_previous"]
        for frame in report["frames"]
        if frame["changed_pct_from_previous"] is not None
    ]
    report["changed_pixel_percentages"] = changed
    report["reading"] = (
        "Frame paths are PNGs; use read on them when available. "
        "dominant_color_share and unique_colors describe each frame. "
        "changed_pct_from_previous is the percent of pixels moving by more than "
        "eight RGB levels after each interaction. These are measurements, not verdicts."
    )
    text = json.dumps(report, indent=2)
    if len(text.encode("utf-8")) > context.max_tool_result_bytes:
        return text.encode("utf-8")[: context.max_tool_result_bytes].decode(
            "utf-8", errors="ignore"
        ) + "\n[truncated]"
    return text


def tools() -> tuple[Tool, ...]:
    return (
        Tool(
            "browser_probe",
            load_description("browser_probe"),
            browser_probe,
            {
                "target": {"type": "string"},
                "click": {"type": "string"},
                "press": {"type": "string"},
                "output_dir": {"type": "string"},
                "settle_ms": {"type": "integer", "default": 1200},
                "hold_ms": {"type": "integer", "default": 1600},
                "viewport_width": {"type": "integer", "default": 1280},
                "viewport_height": {"type": "integer", "default": 800},
            },
            required=("target",),
        ),
    )
