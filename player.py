# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2025-2026 thiliapr <thiliapr@tutanota.com>

import argparse
import pathlib
import subprocess
import threading
import shutil
import time
from collections.abc import Callable
from typing import Any, Optional

# 过滤器参数类型
FilterWithArgs = Callable[[pathlib.Path], bool]
FILTER_ARG_TYPES = {
    "str": str,
    "int": int,  # 不知道什么时候会用，但先写了再说，凑个整
    "float": float,  # 也是凑整类型
    "bool": lambda x: x.lower() == "true"  # 防止 bool("false") 返回 True
}
FILTERS = {
    "Suffix": lambda file, *suffixies, reverse=False: reverse ^ (file.suffix.lower() in suffixies),
    "Prefix": lambda file, *prefixies, reverse=False: reverse ^ any(file.name.startswith(prefix) for prefix in prefixies),
    "Substring": lambda file, *substrings, reverse=False: reverse ^ any(substring in file.name for substring in substrings),
    "Path": lambda file, *paths, reverse=False: reverse ^ any(file.as_posix().startswith(path) for path in paths),
}


class MusicPlayerController:
    """
    音乐播放控制器，提供线程安全的播放进程和任务管理。

    该类封装了播放进程和当前播放任务的状态管理，确保多线程环境下的安全访问。
    通过锁机制保护内部状态，避免竞态条件。

    Attributes:
        lock: 用于同步访问的内部锁
        process: 当前播放进程
        current_task: 当前应用的短暂过滤器
    """

    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()
        self._process = None
        self._current_task = None
        self._was_user_interrupted = False

    @property
    def process(self) -> subprocess.Popen:
        with self.lock:
            return self._process

    @process.setter
    def process(self, process: subprocess.Popen):
        with self.lock:
            self._process = process

    @property
    def current_task(self) -> FilterWithArgs:
        with self.lock:
            return self._current_task

    @current_task.setter
    def current_task(self, task: FilterWithArgs):
        with self.lock:
            self._current_task = task

    @property
    def was_user_interrupted(self) -> bool:
        with self.lock:
            return self._was_user_interrupted

    @was_user_interrupted.setter
    def was_user_interrupted(self, b: bool):
        with self.lock:
            self._was_user_interrupted = b


def dfs_file_sort_key(file: pathlib.Path) -> tuple[list[tuple[bool, str]], int]:
    return (
        [
            (
                i != len(file.parts),  # 先判断目录还是文件，不是文件则为 True，文件优先级更高（因为 sorted 会把 False 排在前面）
                part  # 按字典序排序
            )
            for i, part in enumerate(file.parts, start=1)
        ],
        len(file.parts)  # 目录深度，越浅的目录优先级越高
    )
    


def _playback_worker(directory: pathlib.Path, player_controller: MusicPlayerController, filters: list[FilterWithArgs]):
    """
    音乐文件播放线程函数，循环遍历目录并播放符合条件的音频文件。

    该函数会持续扫描指定目录及其子目录，查找可播放的音频文件。
    播放行为受控制器中的当前任务状态影响。
    此函数应作为后台线程运行，通过控制器与主线程通信。

    Args:
        directory: 要扫描的音乐文件根目录
        player_controller: 音乐播放控制器实例
    """
    # 寻找 ffplay 执行程序
    ffplay_path = shutil.which("ffplay")
    if not ffplay_path:
        raise RuntimeError("未找到 ffplay 执行程序。请确保 PATH 环境变量中包含 ffplay。")

    while True:
        for audio_file in sorted(directory.rglob("*"), key=dfs_file_sort_key):
            if not audio_file.is_file() or not all(func(audio_file.relative_to(directory)) for func in filters):
                continue

            # 检查短暂过滤条件
            current_task = player_controller.current_task
            if current_task and not current_task(audio_file.relative_to(directory)):
                continue

            # 找到后清除过滤条件
            player_controller.current_task = None

            # 显示正在播放的文件名（相对路径）
            print(audio_file.relative_to(directory).as_posix(), end="", flush=True)

            # 启动播放进程
            player_controller.process = subprocess.Popen([ffplay_path, "-nodisp", "-autoexit", audio_file], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)

            # 等待播放结束或收到停止信号
            while player_controller.process.poll() is None:
                time.sleep(0.1)

            # 如果用户输入了指令，就不换行，因为用户已经回车了
            if not player_controller.was_user_interrupted:
                print()

            # 清除用户指令状态
            player_controller.was_user_interrupted = False

        # 一轮扫描完成后重置任务
        player_controller.current_task = None


def parse_filter(filter_str: str) -> tuple[FilterWithArgs, tuple[str, list[Any], dict[str, Any]]]:
    # 初始化状态变量
    state = {"filter_name": "", "current_key": "", "current_value": "", "arg_type": ""}
    char_to_state = {":": "current_key", "#": "arg_type", "=": "current_value"}
    filter_args = []  # 位置参数列表
    filter_kwargs = {}  # 关键字参数字典
    current_state = "filter_name"  # 当前解析状态
    is_escaping = False  # 转义状态标志

    # 遍历输入字符串的每个字符
    for char in (filter_str + ";"):
        # 处理转义字符或普通字符
        if is_escaping or char not in ":#;=\\":
            is_escaping = False  # 重置转义状态
            state[current_state] += char  # 添加字符到当前状态变量
        # 处理特殊字符
        elif char == "\\":
            is_escaping = True  # 设置转义状态
        elif char in char_to_state:
            # 切换解析状态
            current_state = char_to_state[char]
        elif char == ";":
            # 完成一个参数的解析，检查类型
            type_converter = FILTER_ARG_TYPES.get(state["arg_type"] or "str")
            if type_converter is None:
                raise ValueError(f"解析 `{filter_str}` 时发现了未知的参数类型: {state['arg_type']}")

            # 如果存在 value 则为关键字参数，否则为位置参数
            current_key = state["current_key"]
            current_value = state["current_value"]
            if current_value:
                filter_kwargs[current_key] = type_converter(current_value)
            else:
                filter_args.append(type_converter(current_key))

            # 重置临时变量
            state["current_key"] = state["current_value"] = state["arg_type"] = ""
            current_state = "current_key"  # 准备解析下一个键

    # 获取并返回过滤器函数及其参数
    filter_name = state["filter_name"]
    if filter_name not in FILTERS:
        raise ValueError(f"解析 `{filter_str}` 时发现了未知的过滤器名称: {filter_name}")
    return lambda file, fn=FILTERS[filter_name], args=filter_args, kwargs=filter_kwargs: fn(file, *args, **kwargs), (filter_name, filter_args, filter_kwargs)


def parse_args(args: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("-r", "--root", type=pathlib.Path, default=pathlib.Path.cwd(), help="音乐文件根目录（默认为当前工作目录）")
    parser.add_argument("-f", "--filter", type=str, action="append", default=["Suffix:.mid;reverse=true#bool"], help=("过滤器。默认为 %(default)s"))
    return parser.parse_args(args)


def main(args: argparse.Namespace):
    # 解析过滤器
    filters = []
    for filter_arg in args.filter:
        parsed_filter, filter_info = parse_filter(filter_arg)
        filters.append(parsed_filter)
        print("***", filter_info)
    print()

    # 创建音乐进程
    player_controller = MusicPlayerController()
    threading.Thread(target=_playback_worker, args=(args.root, player_controller, filters), daemon=True).start()

    try:
        while True:
            # 检查当前播放状态
            current_process = player_controller.process
            if not current_process or current_process.poll() is not None:
                time.sleep(0.1)
                continue

            # 处理用户输入
            command = input()
            player_controller.was_user_interrupted = True
            if command:
                player_controller.current_task, filter_info = parse_filter(command)
                print("***", filter_info)

            # 停止当前播放
            current_process = player_controller.process
            current_process.terminate()
            current_process.wait()
    except KeyboardInterrupt:
        pass
    finally:
        # 确保退出前停止所有播放
        current_process = player_controller.process
        if current_process:
            current_process.terminate()
            current_process.wait()


if __name__ == "__main__":
    main(parse_args())
