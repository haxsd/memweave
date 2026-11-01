import argparse
import asyncio
import json
import sys
from pathlib import Path

from memweave import __version__
from memweave.diagnostics import Diagnostics, DiagnosticError
from memweave.integrations import client_configuration


def emit_error(error: DiagnosticError) -> None:
    print(json.dumps({
        "status": "error",
        "error": {"code": error.code, "message": str(error)},
    }, ensure_ascii=False), file=sys.stderr)


def emit_installation(data: dict) -> None:
    state = data["state"]
    labels = {"installed": "接入已更新", "up_to_date": "接入已是当前版本",
              "ready": "本机接入检查通过", "needs_attention": "本机接入需要修复"}
    print(f"织忆 {data.get('version', __version__)} · {labels[state]}")
    print(f"项目：{data['root']}")
    if data.get("home"):
        print(f"记忆库：{data['home']}")
    if data.get("toolset"):
        print(f"工具：{data['tool_count']} 个（通用）")
    for check in data.get("checks", []):
        if check["state"] != "ok":
            print(f"- {check['name']}: {check['message']}")
            print(f"  修复：{check['fix']}")
    if data.get("backup_directory"):
        print(f"原配置备份：{data['backup_directory']}")
    if data.get("protocol_verification") == "sdk_stdio":
        print("协议自检：通过；桌面加载和自动触发仍须在客户端核对。")
    elif state in ("installed", "up_to_date"):
        print("在已有客户端中重新加载 MemWeave 后核对工具列表；本命令不会打开窗口。")
    for action in data.get("required_actions", []):
        print(f"- 待处理：{action}")


def emit_client_diagnosis(data: dict) -> None:
    labels = {
        "ready": "正常",
        "repaired": "已修复，需重载客户端",
        "needs_attention": "需要处理",
        "not_installed": "未安装",
    }
    print(f"织忆 {data['version']} · 客户端巡检：{labels[data['state']]}")
    print(f"记忆库：{data['home']}")
    summary = data["summary"]
    print(
        f"扫描：{data['roots_scanned']} 个项目根目录，{data['targets_scanned']} 个接入目标；"
        f"正常 {summary['ready']}，已修复 {summary['repaired']}，待处理 {summary['needs_attention']}。"
    )
    for target in data["targets"]:
        scope = "全局" if target["scope"] == "global" else "项目"
        location = target["root"] or target["config_path"]
        print(f"[{labels[target['state']]}] {target['client']} {scope} · {location}")
        for issue in target.get("issues", []):
            fixable = "，可修复" if issue["fixable"] else "，需人工处理"
            print(f"- {issue['code']}：{issue['message']}（{fixable}）")
        repair = target.get("repair")
        if repair:
            if repair.get("backup_directory"):
                print(f"  原配置备份：{repair['backup_directory']}")
            if repair.get("message"):
                print(f"  修复说明：{repair['message']}")
        for action in target.get("required_actions", []):
            print(f"- 待处理：{action}")
    if data["state"] == "needs_attention":
        print(f'项目级可修复项：memweave clients doctor --home "{data["home"]}" --fix')


def run_hook(arguments: argparse.Namespace, diagnostics: Diagnostics) -> int:
    response = {}
    hooks = None
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise DiagnosticError("RECORD_INVALID", "Hook input exceeds 65536 bytes.")
        try:
            payload = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise DiagnosticError("RECORD_INVALID", "Hook input must be UTF-8 JSON.") from error
        diagnostics.record_hook(arguments.client, arguments.event, payload)
        from memweave.services import Service
        service = Service(diagnostics.home)
        if arguments.client == "codex":
            from memweave.codex_hooks import CodexHooks
            hooks = CodexHooks(diagnostics, arguments.root, service=service)
        else:
            from memweave.hooks import CursorHooks
            hooks = CursorHooks(diagnostics, service=service)
        response = hooks.handle(arguments.event, payload)
    except DiagnosticError as error:
        emit_error(error)
    except OSError:
        emit_error(DiagnosticError("STORAGE_ERROR", "Hook storage unavailable; continue the host task."))
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        emit_error(DiagnosticError("RECORD_INVALID", "Invalid hook event; continue the host task."))
    except Exception:
        emit_error(DiagnosticError("STORAGE_ERROR", "Hook runtime unavailable; continue the host task."))
    finally:
        if hooks is not None:
            try:
                hooks.close()
            except Exception:
                pass
    print(json.dumps(response, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memweave")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("init", "doctor", "serve", "config", "hook", "probe", "call", "ui", "recover", "rebuild", "demo", "setup", "check"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--home", required=command not in ("setup", "check"), type=Path)
        if command in ("serve", "config", "setup"):
            subparser.add_argument("--toolset", choices=("core", "full", "universal"), default=None,
                                   help=argparse.SUPPRESS)
        if command == "serve":
            subparser.add_argument("--diagnostic", action="store_true", help=argparse.SUPPRESS)
        if command == "config":
            subparser.add_argument("--client", required=True, choices=("cursor", "codex"))
            subparser.add_argument("--kind", choices=("mcp", "hooks"), default="mcp")
            subparser.add_argument("--root", type=Path)
        elif command == "hook":
            subparser.add_argument("--client", required=True, choices=("cursor", "codex"))
            subparser.add_argument("--event", required=True)
            subparser.add_argument("--root", type=Path)
        elif command == "call":
            subparser.add_argument("action")
            subparser.add_argument("--input")
        elif command == "ui":
            subparser.add_argument("--port", type=int, default=8765)
            subparser.add_argument("--no-browser", action="store_true")
            subparser.add_argument("--root", type=Path, default=Path.cwd())
        elif command == "demo":
            subparser.add_argument("--root", type=Path, default=Path.cwd())
        elif command in ("setup", "check"):
            subparser.add_argument("--root", type=Path, default=Path.cwd())
            subparser.add_argument("--json", action="store_true")
            subparser.add_argument("--client", choices=("cursor", "codex"), default="cursor")
            if command == "check":
                subparser.add_argument("--probe", action="store_true")
    clients_parser = subparsers.add_parser("clients")
    clients_subparsers = clients_parser.add_subparsers(dest="clients_command", required=True)
    clients_doctor = clients_subparsers.add_parser("doctor")
    clients_doctor.add_argument("--home", type=Path, default=Path.home() / ".memweave")
    clients_doctor.add_argument("--root", action="append", type=Path)
    clients_doctor.add_argument("--client", choices=("all", "cursor", "codex"), default="all")
    clients_doctor.add_argument("--fix", action="store_true")
    clients_doctor.add_argument("--json", action="store_true")
    arguments = parser.parse_args(argv)
    diagnostics = Diagnostics(arguments.home) if arguments.home is not None else None
    if arguments.command == "hook":
        return run_hook(arguments, diagnostics)
    try:
        if arguments.command == "config":
            print(client_configuration(arguments.client, arguments.kind, arguments.home, arguments.toolset, root=arguments.root))
            return 0
        if arguments.command == "clients":
            from memweave.client_doctor import CLIENTS, doctor_clients
            from memweave.diagnostics import result
            selected_clients = CLIENTS if arguments.client == "all" else (arguments.client,)
            output = result(doctor_clients(
                arguments.home, roots=arguments.root, clients=selected_clients,
                fix=arguments.fix,
            ))
            if arguments.json:
                print(json.dumps(output, ensure_ascii=False, indent=2))
            else:
                emit_client_diagnosis(output["data"])
            return 1 if output["data"]["state"] == "needs_attention" else 0
        if arguments.command in ("setup", "check"):
            from memweave.installation import check_installation, setup_cursor
            from memweave.diagnostics import result
            if arguments.client == "codex":
                from memweave.codex_installation import check_codex, setup_codex
                setup_cursor, check_installation = setup_codex, check_codex
            if arguments.command == "setup":
                output = result(setup_cursor(arguments.root, arguments.home, arguments.toolset))
            else:
                output = result(check_installation(arguments.root, arguments.home, arguments.probe))
            if arguments.json:
                print(json.dumps(output, ensure_ascii=False, indent=2))
            else:
                emit_installation(output["data"])
            return 1 if output["data"]["state"] == "needs_attention" else 0
        elif arguments.command == "init":
            from memweave.services import Service
            output = Service(arguments.home).initialize()
            output = {"status": "ok", "data": output}
        elif arguments.command == "doctor":
            output = diagnostics.status()
        elif arguments.command == "serve":
            if not diagnostics.status()["data"]["initialized"]:
                raise DiagnosticError("NOT_INITIALIZED", "Run memweave init before starting the server.")
            from memweave.server import create_server
            create_server(
                diagnostics, arguments.toolset, diagnostic=arguments.diagnostic,
            ).run(transport="stdio")
            return 0
        elif arguments.command in ("call", "ui", "recover", "rebuild"):
            from memweave.services import Service
            service = Service(arguments.home)
            if arguments.command == "ui":
                from memweave.web import serve_ui
                with service:
                    serve_ui(
                        service, port=arguments.port, open_browser=not arguments.no_browser,
                        preferred_root=arguments.root,
                    )
                return 0
            if arguments.command == "call":
                raw = arguments.input if arguments.input is not None else sys.stdin.buffer.read(262145).decode("utf-8-sig")
                if len(raw.encode("utf-8")) > 262144:
                    raise DiagnosticError("RECORD_INVALID", "JSON input exceeds 256 KiB.")
                try:
                    parameters = json.loads(raw)
                except (ValueError, RecursionError) as error:
                    raise DiagnosticError("RECORD_INVALID", "Input must be a JSON object.") from error
                with service:
                    output = service.dispatch(arguments.action, parameters)
            else:
                with service:
                    output = service.dispatch(arguments.command)
        elif arguments.command == "demo":
            from memweave.demo import run_demo
            output = asyncio.run(run_demo(arguments.home, arguments.root))
        else:
            from memweave.probe import run_probe

            output = asyncio.run(run_probe(arguments.home))
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except DiagnosticError as error:
        emit_error(error)
        return 1
    except OSError:
        emit_error(DiagnosticError("STORAGE_ERROR", "Cannot access the configured local path."))
        return 1
    except TimeoutError:
        emit_error(DiagnosticError("PROBE_TIMEOUT", "The stdio probe exceeded its time limit."))
        return 1
    except UnicodeError:
        emit_error(DiagnosticError("RECORD_INVALID", "Input must be UTF-8."))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
