"""源码、模块与安装后命令共用的本地启动入口。"""

import argparse

import uvicorn


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="启动 PDF Layout Diff 本地比较界面")
    parser.add_argument("--port", type=int, default=8765, help="本机端口（默认 8765）")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1 至 65535 之间。")
    print(f"\nPDF Layout Diff: http://127.0.0.1:{args.port}\n按 Ctrl+C 退出。\n")
    uvicorn.run("pdfdiff.app:app", host="127.0.0.1", port=args.port, workers=1)


if __name__ == "__main__":
    main()
