import argparse
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Install MemWeave and configure one existing Agent workspace.")
    parser.add_argument("--client", choices=("cursor", "codex", "devin"), required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--home", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args()
    report = {"status": "failed", "steps": [], "required_actions": []}
    try:
        if sys.version_info < (3, 12):
            raise ValueError("需要 Python 3.12+，请安装后重新执行。")
        source = Path(__file__).resolve().parents[1]
        metadata = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
        if metadata.get("project", {}).get("name") != "memweave":
            raise ValueError("请在完整 MemWeave 克隆中运行 scripts/bootstrap.py。")
        root = arguments.root.expanduser()
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("--root 必须是已存在的项目绝对目录。")
        root = root.resolve()
        venv = source / ".venv"
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if venv.is_symlink() or venv.is_junction():
            raise ValueError("拒绝使用链接或 junction .venv；请先自行检查。")
        if venv.exists() and (not (venv / "pyvenv.cfg").is_file() or not python.is_file()):
            raise ValueError("已有 .venv 不完整，请保留所需内容后修复；脚本不会删除它。")
        if venv.exists():
            for component in (python.parent, python):
                if component.is_junction() or (os.name == "nt" and component.is_symlink()):
                    raise ValueError("拒绝使用重定向到其他目录的 Windows .venv 解释器。")
        common = ["--client", arguments.client, "--root", str(root)]
        if arguments.home is not None:
            common += ["--home", str(arguments.home.expanduser().resolve())]
        commands = []
        if not venv.exists():
            commands.append([sys.executable, "-m", "venv", str(venv)])
        commands += [
            [str(python), "-m", "pip", "install", "--disable-pip-version-check", str(source)],
            [str(python), "-m", "memweave", "setup", *common, "--json"],
            [str(python), "-m", "memweave", "check", *common, "--probe", "--json"],
        ]
        report.update(source=str(source), root=str(root), client=arguments.client, commands=commands)
        if arguments.dry_run:
            report["status"] = "dry_run"
        else:
            for command in commands:
                completed = subprocess.run(
                    command, cwd=source, stdin=subprocess.DEVNULL, capture_output=True,
                    encoding="utf-8", errors="replace", timeout=900,
                    env={**os.environ, "PYTHONUTF8": "1"},
                )
                step = {"command": command, "returncode": completed.returncode}
                if "memweave" in command:
                    try:
                        step["result"] = json.loads(completed.stdout or completed.stderr)
                        report["required_actions"].extend(
                            step["result"].get("data", {}).get("required_actions", [])
                        )
                    except ValueError:
                        step["error"] = "接入命令没有返回有效 JSON。"
                if completed.returncode:
                    step["error"] = step.get("error", "命令失败，请检查解释器、网络或接入冲突。")
                report["steps"].append(step)
                if completed.returncode:
                    break
            else:
                report["status"] = "installed"
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        report["error"] = type(error).__name__ + ": " + str(error)
    report["required_actions"] = list(dict.fromkeys(report["required_actions"]))
    if arguments.json or arguments.dry_run:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("织忆：" + {"installed": "安装及协议检查完成", "failed": "安装失败"}[report["status"]])
        if report.get("error"):
            print(report["error"])
        for step in report["steps"]:
            if step.get("error"):
                print(step["error"])
        for action in report["required_actions"]:
            print("- " + action)
    return 0 if report["status"] != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
