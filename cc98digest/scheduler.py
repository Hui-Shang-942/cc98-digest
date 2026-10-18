"""Windows 计划任务与开机自启：注册 / 卸载。

- 定时抓取用 New-ScheduledTask* 系列 cmdlet（无需管理员权限，注册在当前用户名下）；
- 开机自启 = 在"启动"文件夹放一个 pythonw 快捷方式，指向 `cc98-digest autostart`；
- 所有 PowerShell 调用都通过脚本文件 + 输出重定向完成，避免命名管道捕获输出在沙箱/受限
  环境下失败的问题。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import DEFAULT_TASK_NAME, PROJECT_ROOT, AppConfig
from .logging_setup import get_logger, setup_logging

log = get_logger("scheduler")


def no_window_flags() -> int:
    """Windows 下让子进程不弹控制台窗口。

    pythonw.exe 起的进程是没有控制台的，这时再拉 powershell.exe 会**新建一个控制台窗口**，
    表现为"闪一下空白终端"。所有子进程调用都必须带上这个标志。
    """
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
RUN_BAT = "run-scheduled.bat"
RUN_ONCE_BAT = "run-once.bat"


def validate_time(value: str) -> str:
    m = TIME_RE.match((value or "").strip())
    if not m:
        raise ValueError(f"--time 需要 HH:MM（24 小时制），收到：{value!r}")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def python_for_task() -> str:
    """优先用无控制台的 pythonw.exe，避免每天弹黑框。"""
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    if candidate.exists():
        return str(candidate)
    if exe.exists():
        return str(exe)
    return "python"  # 极端情况：交给 PATH


def _project_root() -> Path:
    """每次调用都从 config 模块取当前值（测试里会打补丁）。"""
    from . import config as _config

    return _config.PROJECT_ROOT


def write_run_bat() -> Path:
    """写/更新 run-scheduled.bat（内容固定，幂等覆盖）。

    坑：`%DATE%` 带 `/`、`-`、`星期X` 等字符不能直接做文件名，所以用 PowerShell 取
    yyyyMMdd（仅数字）。PowerShell 在 cmd.exe 里执行时接收的是普通的双引号字符串，
    因此命令里必须用**单引号**格式串。
    """
    path = _project_root() / RUN_BAT
    content = "\r\n".join(
        [
            "@echo off",
            "rem 由 cc98-digest 自动生成：计划任务入口。",
            "chcp 65001 >nul",
            'cd /d "%~dp0"',
            "set PYTHONUTF8=1",
            "set PYTHONIOENCODING=utf-8",
            f'set "PY={python_for_task()}"',
            'if not exist "logs" mkdir "logs"',
            "for /f %%i in ('powershell -NoProfile -Command Get-Date -Format yyyyMMdd') do set LOGDATE=%%i",
            'if "%LOGDATE%"=="" set LOGDATE=unknown',
            '"%PY%" -m cc98digest run >> "logs\\scheduler-%LOGDATE%.log" 2>&1',
            "exit /b %ERRORLEVEL%",
            "",
        ]
    )
    path.write_text(content, encoding="utf-8-sig")
    log.info("已生成 %s", path)
    return path


def write_once_bat() -> Path:
    """调试用：前台运行一次并把输出打屏 + 落日志。"""
    path = _project_root() / RUN_ONCE_BAT
    content = "\r\n".join(
        [
            "@echo off",
            "rem 由 cc98-digest 自动生成：手动跑一次（前台可见输出）。",
            "chcp 65001 >nul",
            'cd /d "%~dp0"',
            "set PYTHONUTF8=1",
            "set PYTHONIOENCODING=utf-8",
            f'set "PY={python_for_task()}"',
            'if not exist "logs" mkdir "logs"',
            '%PY% -m cc98digest once --print %*',
            "exit /b %ERRORLEVEL%",
            "",
        ]
    )
    path.write_text(content, encoding="utf-8-sig")
    log.info("已生成 %s", path)
    return path


def _powershell_script(commands: list[str], working_dir: Path) -> str:
    lines = [
        "$ErrorActionPreference = 'Stop'",
        f"Set-Location -LiteralPath '{working_dir}'",
        *commands,
        "exit 0",
    ]
    return "\r\n".join(lines)


# ---------------------------------------------------------------------------
# 开机自启：在"启动"文件夹放一个指向 pythonw + `cc98-digest autostart` 的快捷方式
# ---------------------------------------------------------------------------

AUTOSTART_SHORTCUT = "cc98-digest 界面服务.lnk"
AUTOSTART_HOST = "127.0.0.1"
AUTOSTART_PORT = 8848


def startup_dir() -> Path:
    """当前用户的"启动"文件夹（不需要管理员权限）。"""
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidate = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        if candidate.exists():
            return candidate
    return Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def autostart_shortcut_path() -> Path:
    return startup_dir() / AUTOSTART_SHORTCUT


def port_in_use(host: str = AUTOSTART_HOST, port: int = AUTOSTART_PORT, timeout: float = 0.35) -> bool:
    """端口是否已被占用（用来避免开机时起出第二个服务）。"""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def _pythonw_for_task() -> str:
    """优先 pythonw.exe（无控制台窗口），退回 python.exe。"""
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    if candidate.exists():
        return str(candidate)
    return str(exe)


def _make_shortcut_commands(shortcut: Path, project_root: Path) -> list[str]:
    target = _pythonw_for_task()
    return [
        "$ws = New-Object -ComObject WScript.Shell",
        f"$sc = $ws.CreateShortcut('{shortcut}')",
        f"$sc.TargetPath = '{target}'",
        "$sc.Arguments = '-m cc98digest autostart'",
        f"$sc.WorkingDirectory = '{project_root}'",
        "$sc.WindowStyle = 7",  # 7 = 最小化，配合 pythonw 等于完全无窗口
        "$sc.Description = 'CC98 主题词日报：开机后台运行界面服务（浏览器打开 http://127.0.0.1:8848/）'",
        "$sc.IconLocation = '" + str(project_root / "cc98digest" / "static" / "favicon.svg") + "'",
        "$sc.Save()",
        f"if (Test-Path '{shortcut}') {{ 'CREATED' }} else {{ 'MISSING' }}",
    ]


def install_autostart(cfg: AppConfig) -> int:
    """把"开机自启界面服务"写进启动文件夹。"""
    shortcut = autostart_shortcut_path()
    if not startup_dir().exists():
        print(f"[失败] 找不到启动文件夹：{startup_dir()}", file=sys.stderr)
        return 1
    code, output = _run_powershell(_make_shortcut_commands(shortcut, PROJECT_ROOT), PROJECT_ROOT)
    if code != 0 or "CREATED" not in output:
        print(f"[失败] 创建快捷方式失败（退出码 {code}）", file=sys.stderr)
        print(output, file=sys.stderr)
        log.error("开机自启创建失败：%s", output)
        return 1
    print("已设置开机自启。")
    print(f"  快捷方式: {shortcut}")
    print(f"  启动命令: {_pythonw_for_task()} -m cc98digest autostart")
    print(f"  界面地址: http://{AUTOSTART_HOST}:{AUTOSTART_PORT}/")
    print("  说明    : 开机后在后台静默运行（无窗口、不开浏览器）；重复启动会被自动跳过。")
    print("  取消    : python -m cc98digest autostart --remove")
    log.info("已设置开机自启：%s", shortcut)
    return 0


def uninstall_autostart() -> int:
    shortcut = autostart_shortcut_path()
    if not shortcut.exists():
        print("没有设置开机自启，无需取消。")
        return 0
    try:
        shortcut.unlink()
    except OSError as exc:
        print(f"[失败] 删除快捷方式失败：{exc}", file=sys.stderr)
        return 1
    print("已取消开机自启。")
    log.info("已取消开机自启：%s", shortcut)
    return 0


def autostart_entry(cfg: AppConfig, host: str = AUTOSTART_HOST, port: int = AUTOSTART_PORT) -> int:
    """开机自启调用的入口：端口已占用就静默退出，不弹任何错误。"""
    log_dir = cfg.log_dir
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        setup_logging(log_dir, level=cfg.runtime.log_level, to_console=False, filename="autostart.log")
    except Exception as exc:  # 日志不可用也不能挡住服务启动
        print(f"[warn] 自启日志初始化失败：{exc}", file=sys.stderr)
    logger = get_logger("autostart")
    logger.info("开机自启触发：检查 %s:%s", host, port)
    if port_in_use(host, port):
        logger.info("端口已被占用（服务大概已经在跑），本次跳过。")
        return 0
    logger.info("端口空闲，开始启动界面服务。")
    from .gui import serve

    return serve(cfg, host=host, port=port, open_browser=False, quiet=True)


def _run_powershell(commands: list[str], working_dir: Path) -> tuple[int, str]:
    """把脚本写进临时文件再执行。

    注意：这里直接用进程参数调用 PowerShell（不经 cmd.exe），并用文件句柄重定向输出。
    早先在 cmd.exe /c 里拼带引号的路径会被 cmd 的引号规则破坏（报"文件名语法不正确"），
    而且管道捕获输出在受限环境里也不可靠，所以采用本方案。
    """
    exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"
    with tempfile.TemporaryDirectory(prefix="cc98-digest-") as tmp:
        tmp_dir = Path(tmp)
        script = tmp_dir / "task.ps1"
        out_file = tmp_dir / "out.txt"
        err_file = tmp_dir / "err.txt"
        # PowerShell 5.1 读取 UTF-8 需要 BOM，否则中文描述会乱码
        script.write_text(_powershell_script(commands, working_dir), encoding="utf-8-sig")
        with out_file.open("w", encoding="utf-8") as fout, err_file.open(
            "w", encoding="utf-8"
        ) as ferr:
            completed = subprocess.run(
                [
                    exe,
                    "-NoProfile",
                    "-NonInteractive",
                    "-WindowStyle",
                    "Hidden",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script),
                ],
                stdout=fout,
                stderr=ferr,
                cwd=str(working_dir),
                check=False,
                creationflags=no_window_flags(),
            )
        out = out_file.read_text(encoding="utf-8", errors="replace") if out_file.exists() else ""
        err = err_file.read_text(encoding="utf-8", errors="replace") if err_file.exists() else ""
    return completed.returncode, (out + err).strip()


def install_task(cfg: AppConfig, at: str = "23:00", name: str = DEFAULT_TASK_NAME) -> int:
    try:
        at_time = validate_time(at)
    except ValueError as exc:
        print(f"[参数错误] {exc}", file=sys.stderr)
        return 2

    write_run_bat()
    write_once_bat()

    bat = _project_root() / RUN_BAT
    root = _project_root()
    action = (
        f"$action = New-ScheduledTaskAction -Execute 'cmd.exe' "
        f"-Argument '/c \"{bat}\"' -WorkingDirectory '{root}'"
    )
    trigger = f"$trigger = New-ScheduledTaskTrigger -Daily -At '{at_time}'"
    # -RestartCount/-RestartInterval：失败后每 10 分钟重试，最多 3 次。
    # 实测踩过：23:00 运行时电脑刚开机还没连上网，DNS 解析失败直接退出码 2，
    # 那一整天的报告就丢了。有了重试，等网络起来后还能补上。
    settings = (
        "$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable "
        "-MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30) "
        "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 10)"
    )
    register = (
        f"Register-ScheduledTask -TaskName '{name}' -Action $action -Trigger $trigger "
        f"-Settings $settings -Description "
        f"'CC98 主题词日报（cc98-digest）：每天 {at_time} 抓取最近 24 小时内命中主题词的主题帖' "
        "-Force | Out-Null"
    )
    query = (
        f"$t = Get-ScheduledTask -TaskName '{name}'; "
        "$i = $t | Get-ScheduledTaskInfo; "
        '"STATE=$($t.State) NEXT=$($i.NextRunTime) LAST=$($i.LastTaskResult)"'
    )

    code, output = _run_powershell([action, trigger, settings, register, query], root)
    if code != 0:
        print(f"[任务注册失败] 退出码 {code}", file=sys.stderr)
        print(output, file=sys.stderr)
        log.error("计划任务注册失败：%s", output)
        return 1

    print(f"已注册计划任务：{name}（每天 {at_time}）")
    for line in output.splitlines():
        if line.strip():
            print("  " + line.strip())
    print(f"  入口脚本  : {bat}")
    print(f"  运行日志  : {cfg.log_dir / 'scheduler-<日期>.log'}")
    print(f"  修改时间  : python -m cc98digest install-task --time 09:30")
    print(f"  删除任务  : python -m cc98digest uninstall-task")
    print("提示：任务以当前用户身份运行；当天电脑关机会在开机后自动补跑（StartWhenAvailable）。")
    log.info("计划任务 %s 注册成功（%s）。", name, at_time)
    return 0


def uninstall_task(name: str = DEFAULT_TASK_NAME) -> int:
    code, output = _run_powershell(
        [
            f"$t = Get-ScheduledTask -TaskName '{name}' -ErrorAction SilentlyContinue",
            "if ($null -eq $t) { Write-Output 'NOT_FOUND'; exit 0 }",
            f"Unregister-ScheduledTask -TaskName '{name}' -Confirm:$false",
            "Write-Output 'REMOVED'",
        ],
        _project_root(),
    )
    if code != 0:
        print(f"[任务删除失败] 退出码 {code}", file=sys.stderr)
        print(output, file=sys.stderr)
        return 1
    if "NOT_FOUND" in output:
        print(f"计划任务 {name} 不存在，无需删除。")
        return 0
    print(f"已删除计划任务 {name}。")
    log.info("计划任务 %s 已删除。", name)
    return 0


if __name__ == "__main__":  # pragma: no cover
    print("这是 cc98-digest 的内部模块，请通过 python -m cc98digest 调用。")
