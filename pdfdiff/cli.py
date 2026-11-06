"""源码、模块与安装后命令共用的本地启动入口。"""

import argparse
from pathlib import Path

import uvicorn


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="启动 PDF Layout Diff 本地比较界面")
    parser.add_argument("--port", type=int, default=8765, help="本机端口（默认 8765）")
    parser.add_argument("--allow-root", action="append", help="允许 API 读取的目录，可重复指定，默认当前目录")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1 至 65535 之间。")
    roots = []
    for value in args.allow_root or [str(Path.cwd())]:
        try:
            root = Path(value).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            parser.error("可读目录不存在或无法访问。")
        if not root.is_dir() or str(root).startswith(("\\\\", "//")):
            parser.error("可读目录必须是本机已存在的目录。")
        roots.append(root)
    from .app import app

    app.state.configured_roots = tuple(dict.fromkeys(roots))
    print(f"\nPDF Layout Diff: http://127.0.0.1:{args.port}\n按 Ctrl+C 退出。\n")
    uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1)


if __name__ == "__main__":
    main()
