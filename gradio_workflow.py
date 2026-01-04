import json
import time
import random
import requests
import shutil
from collections import Counter, deque # 导入 deque
from PIL import Image, ImageSequence, ImageOps
import re
import io # 导入 io 用于更精确的文件处理
import gradio as gr
from packaging import version
import numpy as np
import torch
import threading
from threading import Lock, Event # 导入 Lock 和 Event
from concurrent.futures import ThreadPoolExecutor
import websocket # 添加 websocket 导入
import atexit # For NVML cleanup
from .kelnel_ui.system_monitor import update_floating_monitors_stream, custom_css as monitor_css, cleanup_nvml # 系统监控模块
from .kelnel_ui.k_Preview import ComfyUIPreviewer # <--- 导入 ComfyUIPreviewer
from .kelnel_ui.css_html_js import HACKER_CSS, get_sponsor_html # <--- 从 css_html_js.py 导入
from .kelnel_ui.ui_def import ( # <--- 从 ui_def.py 导入
    calculate_aspect_ratio,
    strip_prefix,
    parse_resolution,
    load_resolution_presets_from_files,
    find_closest_preset,
    get_output_images,
    # fuck, # Removed as it's deprecated and its logic is integrated elsewhere
    get_workflow_defaults_and_visibility
)
# 导入新的配置管理函数和常量
from .kelnel_ui.ui_def import (
    load_plugin_settings, 
    save_plugin_settings, 
    DEFAULT_MAX_DYNAMIC_COMPONENTS # 需要这个作为 MAX_DYNAMIC_COMPONENTS 的备用值
)

# --- 初始化最大动态组件数量 (从 kelnel_ui.ui_def 导入的函数加载) ---
plugin_settings_on_load = load_plugin_settings() 
MAX_DYNAMIC_COMPONENTS = plugin_settings_on_load.get("max_dynamic_components", DEFAULT_MAX_DYNAMIC_COMPONENTS)
print(f"插件启动：最大动态组件数量从配置加载为: {MAX_DYNAMIC_COMPONENTS} (通过 kelnel_ui.ui_def)")
# --- 初始化最大动态组件数量结束 ---

# Register NVML cleanup function to be called on exit
atexit.register(cleanup_nvml)

# --- 日志轮询导入 ---
import requests # requests 可能已导入，确认一下
import json # json 可能已导入，确认一下
import time # time 可能已导入，确认一下
# --- 日志轮询导入结束 ---
import folder_paths
import node_helpers
from pathlib import Path
from server import PromptServer
from server import BinaryEventTypes
import sys
import os
import webbrowser
import glob
from datetime import datetime
from math import gcd
import uuid
import fnmatch
from .kelnel_ui.gradio_cancel_test import cancel_comfyui_task_action # <--- 导入中断函数
from .kelnel_ui.api_json_manage import define_api_json_management_ui # <--- 导入 API JSON 管理 UI 定义函数

# --- 全局状态变量 ---
task_queue = deque()
queue_lock = Lock()
accumulated_image_results = [] # 明确用于图片
last_video_result = None # 用于存储最新的视频路径
results_lock = Lock()
processing_event = Event() # False: 空闲, True: 正在处理
executor = ThreadPoolExecutor(max_workers=1) # 单线程执行生成任务
last_used_seed = -1 # 用于递增/递减模式
seed_lock = Lock() # 用于保护 last_used_seed
interrupt_requested_event = Event() # 新增：用于用户请求中断当前任务的信号

# --- ComfyUI 实时预览器实例 ---
# 使用一个独特的 client_id_suffix 以避免与 k_Preview.py 的独立测试冲突
comfyui_previewer = ComfyUIPreviewer(client_id_suffix="gradio_workflow_integration", min_yield_interval=0.1)
# --- 全局状态变量结束 ---

# --- 日志轮询全局变量和函数 ---
COMFYUI_LOG_URL = "http://127.0.0.1:8188/internal/logs/raw"
all_logs_text = ""

def fetch_and_format_logs():
    global all_logs_text

    try:
        response = requests.get(COMFYUI_LOG_URL, timeout=5)
        response.raise_for_status()
        data = response.json()
        log_entries = data.get("entries", [])

        # 移除多余空行并合并日志内容
        formatted_logs = "\n".join(filter(None, [entry.get('m', '').strip() for entry in log_entries]))
        all_logs_text = formatted_logs

        return all_logs_text

    except requests.exceptions.RequestException as e:
        error_message = f"无法连接到 ComfyUI 服务器: {e}"
        return all_logs_text + "\n" + error_message if all_logs_text else error_message
    except json.JSONDecodeError:
        error_message = "无法解析服务器响应 (非 JSON)"
        return all_logs_text + "\n" + error_message if all_logs_text else error_message
    except Exception as e:
        error_message = f"发生未知错误: {e}"
        return all_logs_text + "\n" + error_message if all_logs_text else error_message

# --- 日志轮询全局变量和函数结束 ---

# --- ComfyUI 节点徽章设置 ---
# 尝试两种可能的 API 路径
COMFYUI_API_NODE_BADGE = "http://127.0.0.1:8188/settings/Comfy.NodeBadge.NodeIdBadgeMode"
# COMFYUI_API_NODE_BADGE = "http://127.0.0.1:8188/api/settings/Comfy.NodeBadge.NodeIdBadgeMode" # 备用路径

def update_node_badge_mode(mode):
    """发送 POST 请求更新 NodeIdBadgeMode"""
    try:
        # 直接尝试 JSON 格式
        response = requests.post(
            COMFYUI_API_NODE_BADGE,
            json=mode,  # 使用 json 参数自动设置 Content-Type 为 application/json
        )

        if response.status_code == 200:
            return f"✅ 成功更新节点徽章模式为: {mode}"
        else:
            # 尝试解析错误信息
            try:
                error_detail = response.json() # 尝试解析 JSON 错误
                error_text = error_detail.get('error', response.text)
                error_traceback = error_detail.get('traceback', '')
                return f"❌ 更新失败 (HTTP {response.status_code}): {error_text}\n{error_traceback}".strip()
            except json.JSONDecodeError: # 如果不是 JSON 错误
                return f"❌ 更新失败 (HTTP {response.status_code}): {response.text}"
    except requests.exceptions.ConnectionError:
         return f"❌ 请求出错: 无法连接到 ComfyUI 服务器 ({COMFYUI_API_NODE_BADGE})。请确保 ComfyUI 正在运行。"
    except Exception as e:
        return f"❌ 请求出错: {str(e)}"
# --- ComfyUI 节点徽章设置结束 ---

# --- 重启和中断函数 ---
COMFYUI_DEFAULT_URL_FOR_WORKFLOW = "http://127.0.0.1:8188" # 定义 ComfyUI URL 常量

def reboot_manager():
    try:
        # 发送重启请求，改为 GET 方法
        reboot_url = f"{COMFYUI_DEFAULT_URL_FOR_WORKFLOW}/api/manager/reboot" # 使用常量
        response = requests.get(reboot_url)  # 改为 GET 请求
        if response.status_code == 200:
            return "重启请求已发送。请稍后检查 ComfyUI 状态。"
        else:
            return f"重启请求失败，状态码: {response.status_code}"
    except Exception as e:
        return f"发生错误: {str(e)}"

def trigger_comfyui_interrupt():
    """包装函数，用于从 Gradio 调用中断功能，使用预定义的 URL"""
    return cancel_comfyui_task_action(COMFYUI_DEFAULT_URL_FOR_WORKFLOW)

# --- 重启和中断函数结束 ---
# handle_interrupt_click 函数将被移除，因为中断按钮被移除，其逻辑将整合到新的 clear_queue 中


# --- 日志记录函数 ---
def log_message(message):
    timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]  # 精确到毫秒
    print(f"{timestamp} - {message}")

# 修改函数以通过 class_type 查找，并重命名参数
def find_key_by_class_type(prompt, class_type):
    for key, value in prompt.items():
        # 直接检查 class_type 字段
        if isinstance(value, dict) and value.get("class_type") == class_type:
            return key
    return None

def check_seed_node(json_file):
    if not json_file or not os.path.exists(os.path.join(OUTPUT_DIR, json_file)):
        print(f"JSON 文件无效或不存在: {json_file}")
        return gr.update(visible=False)
    json_path = os.path.join(OUTPUT_DIR, json_file)
    try:
        with open(json_path, "r", encoding="utf-8") as file_json:
            prompt = json.load(file_json)
        # 使用新的函数和真实类名
        seed_key = find_key_by_class_type(prompt, "Hua_gradio_Seed")
        return gr.update(visible=seed_key is not None)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"读取或解析 JSON 文件时出错 ({json_file}): {e}")
        return gr.update(visible=False)

current_dir = os.path.dirname(os.path.abspath(__file__))
print("当前hua插件文件的目录为：", current_dir)
parent_dir = os.path.dirname(os.path.dirname(current_dir))
sys.path.append(parent_dir)
try:
    from comfy.cli_args import args
except ImportError:
    print("无法导入 comfy.cli_args，某些功能可能受限。")
    args = None # 提供一个默认值以避免 NameError

# 尝试导入图标，如果失败则使用默认值
try:
    from .node.hua_icons import icons
except ImportError:
    print("无法导入 .hua_icons，将使用默认分类名称。")
    icons = {"hua_boy_one": "Gradio"} # 提供一个默认值

class GradioTextOk:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "string": ("STRING", {"multiline": True, "dynamicPrompts": True, "tooltip": "The text to be encoded."}),
                "name": ("STRING", {"multiline": False, "default": "GradioTextOk", "tooltip": "节点名称"}),
            }
        }
    RETURN_TYPES = ("STRING",)
    FUNCTION = "encode"
    CATEGORY = icons.get("hua_boy_one", "Gradio") # 使用 get 提供默认值
    DESCRIPTION = "Encodes a text prompt..."
    def encode(self, string, name):
        return (string,)

INPUT_DIR = folder_paths.get_input_directory()
OUTPUT_DIR = folder_paths.get_output_directory()
TEMP_DIR = folder_paths.get_temp_directory()

# --- Load Resolution Presets from File ---
# resolution_files and resolution_prefixes are defined here
resolution_files = [
    "Sample_preview/flux_resolution.txt",
    "Sample_preview/sdxl_1_5_resolution.txt"
]
resolution_prefixes = [
    "Flux - ",
    "SDXL - "
]
# load_resolution_presets_from_files is now imported from ui_def
# It needs current_dir (script_dir)
resolution_presets = load_resolution_presets_from_files(resolution_files, resolution_prefixes, current_dir)
# Add a print statement to confirm loading
print(f"Final resolution_presets count (including 'custom'): {len(resolution_presets)}")
if len(resolution_presets) < 10: # Print some examples if loading failed or files are short
    print(f"Example presets: {resolution_presets[:10]}")
# --- End Load Resolution Presets ---


def start_queue(prompt_workflow):
    p = {"prompt": prompt_workflow}
    data = json.dumps(p).encode('utf-8')
    URL = "http://127.0.0.1:8188/prompt"
    max_retries = 5
    retry_delay = 10
    request_timeout = 60

    for attempt in range(max_retries):
        try:
            # 简化服务器检查，直接尝试 POST
            response = requests.post(URL, data=data, timeout=request_timeout)
            response.raise_for_status() # 如果是 4xx 或 5xx 会抛出 HTTPError
            print(f"请求成功 (尝试 {attempt + 1}/{max_retries})")
            return True # 返回成功状态
        except requests.exceptions.HTTPError as http_err: # 特别处理 HTTP 错误
            status_code = http_err.response.status_code
            print(f"请求失败 (尝试 {attempt + 1}/{max_retries}, HTTP 状态码: {status_code}): {str(http_err)}")
            if status_code == 400: # Bad Request (例如 invalid prompt)
                print("发生 400 Bad Request 错误，通常表示 prompt 无效。停止重试。")
                return False # 立刻返回失败，不重试
            # 对于其他 HTTP 错误 (例如 5xx)，继续重试逻辑
            if attempt < max_retries - 1:
                print(f"{retry_delay}秒后重试...")
                time.sleep(retry_delay)
            else:
                print("达到最大重试次数 (HTTPError)，放弃请求。")
                return False
        except requests.exceptions.RequestException as e: # 其他网络错误 (超时, 连接错误等)
            error_type = type(e).__name__
            print(f"请求失败 (尝试 {attempt + 1}/{max_retries}, 错误类型: {error_type}): {str(e)}")
            if attempt < max_retries - 1:
                print(f"{retry_delay}秒后重试...")
                time.sleep(retry_delay)
            else:
                print("达到最大重试次数 (RequestException)，放弃请求。")
                print("可能原因: 服务器未运行、网络问题。") # 保留此通用原因
                return False # 返回失败状态
    return False # 确保函数在所有路径都有返回值

def get_json_files():
    try:
        json_files = [f for f in os.listdir(OUTPUT_DIR) if f.endswith('.json') and os.path.isfile(os.path.join(OUTPUT_DIR, f))]
        return json_files
    except FileNotFoundError:
        print(f"警告: 输出目录 {OUTPUT_DIR} 未找到。")
        return []
    except Exception as e:
        print(f"获取 JSON 文件列表时出错: {e}")
        return []

def refresh_json_files():
    new_choices = get_json_files()
    return gr.update(choices=new_choices)

# strip_prefix, parse_resolution, calculate_aspect_ratio, find_closest_preset are now imported from ui_def

def update_from_preset(resolution_str_with_prefix):
    if resolution_str_with_prefix == "custom":
        # 返回空更新，让用户手动输入
        return "custom", gr.update(), gr.update(), "当前比例: 自定义"

    # parse_resolution is imported, needs resolution_prefixes
    width, height, ratio, original_str = parse_resolution(resolution_str_with_prefix, resolution_prefixes)

    if width is None: # 处理无效格式的情况
        return "custom", gr.update(), gr.update(), "当前比例: 无效格式"

    # Return the original string with prefix for the dropdown value
    return original_str, width, height, f"当前比例: {ratio}"

def update_from_inputs(width, height):
    # calculate_aspect_ratio and find_closest_preset are imported
    # find_closest_preset needs resolution_presets and resolution_prefixes
    ratio = calculate_aspect_ratio(width, height)
    closest_preset = find_closest_preset(width, height, resolution_presets, resolution_prefixes)
    return closest_preset, f"当前比例: {ratio}"

def flip_resolution(width, height):
    if width is None or height is None:
        return None, None
    try:
        # 确保返回的是数字类型
        return int(height), int(width)
    except (ValueError, TypeError):
        return width, height # 如果转换失败，返回原值

# --- 模型列表获取 ---
def get_model_list(model_type):
    try:
        # 添加 "None" 选项，允许不选择
        return ["None"] + folder_paths.get_filename_list(model_type)
    except Exception as e:
        print(f"获取 {model_type} 列表时出错: {e}")
        return ["None"]

lora_list = get_model_list("loras")
checkpoint_list = get_model_list("checkpoints")
unet_list = get_model_list("unet") # 假设 UNet 模型在 'unet' 目录

# get_output_images is now imported from ui_def

# 修改 generate_image 函数以接受动态组件列表
def generate_image(
    inputimage1, input_video, 
    dynamic_positive_prompts_values: list, # 列表，包含所有 positive_prompt_texts 的值
    prompt_text_negative, 
    json_file, 
    hua_width, hua_height, 
    dynamic_loras_values: list,           # 列表，包含所有 lora_dropdowns 的值
    hua_checkpoint, hua_unet, 
    dynamic_float_nodes_values: list,     # 列表，包含所有 float_inputs 的值
    dynamic_int_nodes_values: list,       # 列表，包含所有 int_inputs 的值
    seed_mode, fixed_seed
):
    global last_used_seed # 声明使用全局变量
    execution_id = str(uuid.uuid4())
    print(f"[{execution_id}] 开始生成任务 (种子模式: {seed_mode})...")
    output_type = None # 'image' or 'video'

    if not json_file:
        print(f"[{execution_id}] 错误: 未选择工作流 JSON 文件。")
        return None, None # 返回 (None, None) 表示失败

    json_path = os.path.join(OUTPUT_DIR, json_file)
    if not os.path.exists(json_path):
        print(f"[{execution_id}] 错误: 工作流 JSON 文件不存在: {json_path}")
        return None, None

    try:
        with open(json_path, "r", encoding="utf-8") as file_json:
            prompt = json.load(file_json)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"[{execution_id}] 读取或解析 JSON 文件时出错 ({json_path}): {e}")
        return None, None

    # --- 更新 Prompt ---
    # 首先获取工作流中实际存在的动态节点的定义
    # 注意：get_workflow_defaults_and_visibility 现在返回更详细的动态组件信息
    # 我们需要从 prompt (原始JSON) 中直接查找节点ID，或者依赖 get_workflow_defaults_and_visibility 返回的ID
    # 为简化，这里假设 get_workflow_defaults_and_visibility 返回的 dynamic_components 包含节点ID
    # 并且 dynamic_*_values 列表中的顺序与 get_workflow_defaults_and_visibility 找到的节点顺序一致

    workflow_info = get_workflow_defaults_and_visibility(json_file, OUTPUT_DIR, resolution_prefixes, resolution_presets, MAX_DYNAMIC_COMPONENTS)
    
    # --- 单例节点查找 ---
    image_input_key = find_key_by_class_type(prompt, "GradioInputImage")
    video_input_key = find_key_by_class_type(prompt, "VHS_LoadVideo")
    seed_key = find_key_by_class_type(prompt, "Hua_gradio_Seed")
    text_bad_key = find_key_by_class_type(prompt, "GradioTextBad")
    fenbianlv_key = find_key_by_class_type(prompt, "Hua_gradio_resolution")
    checkpoint_key = find_key_by_class_type(prompt, "Hua_CheckpointLoaderSimple")
    unet_key = find_key_by_class_type(prompt, "Hua_UNETLoader") # 确保类名正确
    hua_output_key = find_key_by_class_type(prompt, "Hua_Output")
    hua_video_output_key = find_key_by_class_type(prompt, "Hua_Video_Output")
    
    inputfilename = None # 初始化
    if image_input_key:
        if inputimage1 is not None:
            try:
                # 确保 inputimage1 是 PIL Image 对象
                if isinstance(inputimage1, np.ndarray):
                    img = Image.fromarray(inputimage1)
                elif isinstance(inputimage1, Image.Image):
                    img = inputimage1
                else:
                    print(f"[{execution_id}] 警告: 未知的输入图像类型: {type(inputimage1)}。尝试跳过图像输入。")
                    img = None

                if img:
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    inputfilename = f"gradio_input_{timestamp}_{random.randint(100, 999)}.png"
                    save_path = os.path.join(INPUT_DIR, inputfilename)
                    img.save(save_path)
                    prompt[image_input_key]["inputs"]["image"] = inputfilename
                    print(f"[{execution_id}] 输入图像已保存到: {save_path}")
            except Exception as e:
                print(f"[{execution_id}] 保存输入图像时出错: {e}")
                # 不设置图像输入，让工作流使用默认值（如果存在）
                if "image" in prompt[image_input_key]["inputs"]:
                    del prompt[image_input_key]["inputs"]["image"] # 或者设置为 None，取决于节点如何处理
        else:
             # 如果没有输入图像，确保节点输入中没有残留的文件名
             if image_input_key and "image" in prompt.get(image_input_key, {}).get("inputs", {}):
                 # 尝试移除或设置为空，取决于节点期望
                 # prompt[image_input_key]["inputs"]["image"] = None
                 print(f"[{execution_id}] 无输入图像提供，清除节点 {image_input_key} 的 image 输入。")
                 # 或者如果节点必须有输入，则可能需要报错或使用默认图像
                 # return None, None # 如果图生图节点必须有输入

    # --- 处理视频输入 ---
    inputvideofilename = None
    if video_input_key:
        if input_video is not None and os.path.exists(input_video):
            try:
                # Gradio 返回的是临时文件路径，需要复制到 ComfyUI 的 input 目录
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                # 保留原始扩展名
                original_ext = os.path.splitext(input_video)[1]
                inputvideofilename = f"gradio_input_{timestamp}_{random.randint(100, 999)}{original_ext}"
                dest_path = os.path.join(INPUT_DIR, inputvideofilename)
                shutil.copy2(input_video, dest_path) # 使用 copy2 保留元数据
                prompt[video_input_key]["inputs"]["video"] = inputvideofilename
                print(f"[{execution_id}] 输入视频已复制到: {dest_path}")
            except Exception as e:
                print(f"[{execution_id}] 复制输入视频时出错: {e}")
                # 清除节点输入，让其使用默认值（如果存在）
                if "video" in prompt[video_input_key]["inputs"]:
                    del prompt[video_input_key]["inputs"]["video"]
        else:
            # 如果没有输入视频或路径无效，确保节点输入中没有残留的文件名
            if "video" in prompt.get(video_input_key, {}).get("inputs", {}):
                print(f"[{execution_id}] 无有效输入视频提供，清除节点 {video_input_key} 的 video 输入。")
                # 移除或设置为空，取决于节点期望
                 # prompt[video_input_key]["inputs"]["video"] = None

    if seed_key:
        with seed_lock: # 保护对 last_used_seed 的访问
            current_seed = 0
            if seed_mode == "随机":
                current_seed = random.randint(0, 0xffffffff)
                print(f"[{execution_id}] 种子模式: 随机. 生成种子: {current_seed}")
            elif seed_mode == "递增":
                if last_used_seed == -1: # 如果是第一次运行递增
                    last_used_seed = random.randint(0, 0xffffffff -1) # 随机选一个初始值，避免总是从0开始且确保能+1
                last_used_seed = (last_used_seed + 1) & 0xffffffff # 递增并处理溢出 (按位与)
                current_seed = last_used_seed
                print(f"[{execution_id}] 种子模式: 递增. 使用种子: {current_seed}")
            elif seed_mode == "递减":
                if last_used_seed == -1: # 如果是第一次运行递减
                    last_used_seed = random.randint(1, 0xffffffff) # 随机选一个初始值，避免总是从0开始且确保能-1
                last_used_seed = (last_used_seed - 1) & 0xffffffff # 递减并处理下溢 (按位与)
                current_seed = last_used_seed
                print(f"[{execution_id}] 种子模式: 递减. 使用种子: {current_seed}")
            elif seed_mode == "固定":
                try:
                    current_seed = int(fixed_seed) & 0xffffffff # 确保是整数且在范围内
                    last_used_seed = current_seed # 固定模式也更新 last_used_seed
                    print(f"[{execution_id}] 种子模式: 固定. 使用种子: {current_seed}")
                except (ValueError, TypeError):
                    current_seed = random.randint(0, 0xffffffff)
                    last_used_seed = current_seed
                    print(f"[{execution_id}] 种子模式: 固定. 固定种子值无效 ('{fixed_seed}')，回退到随机种子: {current_seed}")
            else: # 未知模式，默认为随机
                current_seed = random.randint(0, 0xffffffff)
                last_used_seed = current_seed
                print(f"[{execution_id}] 未知种子模式 '{seed_mode}'. 回退到随机种子: {current_seed}")

            prompt[seed_key]["inputs"]["seed"] = current_seed
    
    # 更新动态正向提示词
    actual_positive_prompt_nodes = workflow_info["dynamic_components"]["GradioTextOk"]
    for i, node_info in enumerate(actual_positive_prompt_nodes):
        if i < len(dynamic_positive_prompts_values):
            node_id_to_update = node_info["id"]
            if node_id_to_update in prompt:
                prompt[node_id_to_update]["inputs"]["string"] = dynamic_positive_prompts_values[i]
                print(f"[{execution_id}] 更新正向提示节点 {node_id_to_update} (UI组件 {i+1}) 为: '{dynamic_positive_prompts_values[i]}'")
            else:
                print(f"[{execution_id}] 警告: 未在prompt中找到正向提示节点ID {node_id_to_update}")
        else:
            # 通常不应发生，因为 dynamic_positive_prompts_values 应该与可见组件数量匹配
            print(f"[{execution_id}] 警告: 正向提示值列表长度不足以覆盖节点 {node_info['id']}")


    if text_bad_key: prompt[text_bad_key]["inputs"]["string"] = prompt_text_negative

    if fenbianlv_key:
        try:
            width_val = int(hua_width)
            height_val = int(hua_height)
            prompt[fenbianlv_key]["inputs"]["custom_width"] = width_val
            prompt[fenbianlv_key]["inputs"]["custom_height"] = height_val
            print(f"[{execution_id}] 设置分辨率: {width_val}x{height_val}")
            # 添加调试信息
            print(f"[{execution_id}] 分辨率节点ID: {fenbianlv_key}")
            print(f"[{execution_id}] 分辨率节点输入: {prompt[fenbianlv_key]['inputs']}")
        except (ValueError, TypeError, KeyError) as e:
             print(f"[{execution_id}] 更新分辨率时出错: {e}. 使用默认值或跳过。")
             # 打印当前prompt结构帮助调试
             print(f"[{execution_id}] 当前prompt结构: {json.dumps(prompt, indent=2, ensure_ascii=False)}")

    # 更新动态Lora模型选择
    actual_lora_nodes = workflow_info["dynamic_components"]["Hua_LoraLoaderModelOnly"]
    for i, node_info in enumerate(actual_lora_nodes):
        if i < len(dynamic_loras_values):
            node_id_to_update = node_info["id"]
            lora_name_from_ui = dynamic_loras_values[i]
            if node_id_to_update in prompt and lora_name_from_ui != "None":
                prompt[node_id_to_update]["inputs"]["lora_name"] = lora_name_from_ui
                print(f"[{execution_id}] 更新Lora节点 {node_id_to_update} (UI组件 {i+1}) 为: '{lora_name_from_ui}'")
            elif lora_name_from_ui == "None":
                 print(f"[{execution_id}] Lora节点 {node_id_to_update} (UI组件 {i+1}) 选择为 'None'，不更新。")
            else:
                print(f"[{execution_id}] 警告: 未在prompt中找到Lora节点ID {node_id_to_update}")

    if checkpoint_key and hua_checkpoint != "None": prompt[checkpoint_key]["inputs"]["ckpt_name"] = hua_checkpoint
    if unet_key and hua_unet != "None": prompt[unet_key]["inputs"]["unet_name"] = hua_unet

    # 更新动态Int节点输入
    actual_int_nodes = workflow_info["dynamic_components"]["HuaIntNode"]
    for i, node_info in enumerate(actual_int_nodes):
        if i < len(dynamic_int_nodes_values):
            node_id_to_update = node_info["id"]
            int_value_from_ui = dynamic_int_nodes_values[i]
            if node_id_to_update in prompt and int_value_from_ui is not None:
                try:
                    prompt[node_id_to_update]["inputs"]["int_value"] = int(int_value_from_ui)
                    print(f"[{execution_id}] 更新Int节点 {node_id_to_update} (UI组件 {i+1}) 为: {int(int_value_from_ui)}")
                except (ValueError, TypeError, KeyError) as e:
                    print(f"[{execution_id}] 更新Int节点 {node_id_to_update} 时出错: {e}. 使用默认值或跳过。")
            else:
                 print(f"[{execution_id}] 警告: 未在prompt中找到Int节点ID {node_id_to_update} 或值为None")
    
    # 更新动态Float节点输入
    actual_float_nodes = workflow_info["dynamic_components"]["HuaFloatNode"]
    for i, node_info in enumerate(actual_float_nodes):
        if i < len(dynamic_float_nodes_values):
            node_id_to_update = node_info["id"]
            float_value_from_ui = dynamic_float_nodes_values[i]
            if node_id_to_update in prompt and float_value_from_ui is not None:
                try:
                    prompt[node_id_to_update]["inputs"]["float_value"] = float(float_value_from_ui)
                    print(f"[{execution_id}] 更新Float节点 {node_id_to_update} (UI组件 {i+1}) 为: {float(float_value_from_ui)}")
                except (ValueError, TypeError, KeyError) as e:
                    print(f"[{execution_id}] 更新Float节点 {node_id_to_update} 时出错: {e}. 使用默认值或跳过。")
            else:
                print(f"[{execution_id}] 警告: 未在prompt中找到Float节点ID {node_id_to_update} 或值为None")

    # --- 设置输出节点的 unique_id ---
    if hua_output_key:
        prompt[hua_output_key]["inputs"]["unique_id"] = execution_id
        output_type = 'image'
        print(f"[{execution_id}] 已将 unique_id 设置给图片输出节点 {hua_output_key}")
    elif hua_video_output_key:
        prompt[hua_video_output_key]["inputs"]["unique_id"] = execution_id
        output_type = 'video'
        print(f"[{execution_id}] 已将 unique_id 设置给视频输出节点 {hua_video_output_key}")
    else:
        print(f"[{execution_id}] 警告: 未找到 '🌙图像输出到gradio前端' 或 '🎬视频输出到gradio前端' 节点，可能无法获取结果。")
        return None, None # 如果必须有输出节点才能工作，则返回失败

    # --- 发送请求并等待结果 ---
    try:
        print(f"[{execution_id}] 调用 start_queue 发送请求...")
        success = start_queue(prompt) # 发送请求到 ComfyUI
        if not success:
             print(f"[{execution_id}] 请求发送失败 (start_queue returned False). ComfyUI后端拒绝了任务或发生错误。")
             return "COMFYUI_REJECTED", None # 特殊返回值表示后端拒绝
        print(f"[{execution_id}] 请求已发送，开始等待结果...")
    except Exception as e:
        print(f"[{execution_id}] 调用 start_queue 时发生意外错误: {e}")
        return None, None

    # --- 精确文件获取逻辑 ---
    temp_file_path = os.path.join(TEMP_DIR, f"{execution_id}.json")
    # 增加日志，打印 TEMP_DIR 的实际路径
    log_message(f"[{execution_id}] TEMP_DIR is: {TEMP_DIR}")
    log_message(f"[{execution_id}] 开始等待临时文件: {temp_file_path}")

    start_time = time.time()
    wait_timeout = 1000 # 保持原来的超时
    check_interval = 1
    files_in_temp_dir_logged = False # 标志位，确保只记录一次目录内容

    while time.time() - start_time < wait_timeout:
        if os.path.exists(temp_file_path):
            log_message(f"[{execution_id}] 检测到临时文件 (耗时: {time.time() - start_time:.1f}秒)")
            try:
                log_message(f"[{execution_id}] Waiting briefly before reading {temp_file_path}...") # 使用 log_message
                time.sleep(1.0) # 增加等待时间到 1 秒

                with open(temp_file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                    if not content:
                        log_message(f"[{execution_id}] 警告: 临时文件为空。") # 使用 log_message
                        time.sleep(check_interval)
                        continue
                    log_message(f"[{execution_id}] Read content: '{content[:200]}...'") # 使用 log_message

                output_paths_data = json.loads(content)
                log_message(f"[{execution_id}] Parsed JSON data type: {type(output_paths_data)}") # 使用 log_message

                # --- 检查错误结构 ---
                if isinstance(output_paths_data, dict) and "error" in output_paths_data:
                    error_message = output_paths_data.get("error", "Unknown error from node.")
                    generated_files = output_paths_data.get("generated_files", [])
                    log_message(f"[{execution_id}] 错误: 节点返回错误: {error_message}. 文件列表 (可能不完整): {generated_files}") # 使用 log_message
                    try:
                        os.remove(temp_file_path)
                        log_message(f"[{execution_id}] 已删除包含错误的临时文件。") # 使用 log_message
                    except OSError as e:
                        log_message(f"[{execution_id}] 删除包含错误的临时文件失败: {e}") # 使用 log_message
                    return None, None # 返回失败

                # --- 提取路径列表 ---
                output_paths = []
                if isinstance(output_paths_data, dict) and "generated_files" in output_paths_data:
                    output_paths = output_paths_data["generated_files"]
                    log_message(f"[{execution_id}] Extracted 'generated_files': {output_paths} (Count: {len(output_paths)})") # 使用 log_message
                elif isinstance(output_paths_data, list): # 处理旧格式以防万一
                     output_paths = output_paths_data
                     log_message(f"[{execution_id}] Parsed JSON directly as list: {output_paths} (Count: {len(output_paths)})") # 使用 log_message
                else:
                    log_message(f"[{execution_id}] 错误: 无法识别的 JSON 结构。") # 使用 log_message
                    try: os.remove(temp_file_path)
                    except OSError: pass
                    return None, None # 无法识别的结构

                # --- 详细验证路径 ---
                log_message(f"[{execution_id}] Starting path validation for {len(output_paths)} paths...") # 使用 log_message
                valid_paths = []
                invalid_paths = []
                for i, p in enumerate(output_paths):
                    abs_p = os.path.abspath(p)
                    exists = os.path.exists(abs_p)
                    log_message(f"[{execution_id}] Validating path {i+1}/{len(output_paths)}: '{p}' -> Absolute: '{abs_p}' -> Exists: {exists}") # 使用 log_message
                    if exists:
                        valid_paths.append(abs_p)
                    else:
                        invalid_paths.append(p)

                log_message(f"[{execution_id}] Validation complete. Valid: {len(valid_paths)}, Invalid: {len(invalid_paths)}") # 使用 log_message

                try:
                    os.remove(temp_file_path)
                    log_message(f"[{execution_id}] 已删除临时文件。") # 使用 log_message
                except OSError as e:
                    log_message(f"[{execution_id}] 删除临时文件失败: {e}") # 使用 log_message

                if not valid_paths:
                    log_message(f"[{execution_id}] 错误: 未找到有效的输出文件路径。Invalid paths were: {invalid_paths}") # 使用 log_message
                    return None, None

                first_valid_path = valid_paths[0]
                if first_valid_path.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp')):
                    determined_output_type = 'image'
                elif first_valid_path.lower().endswith(('.mp4', '.webm', '.avi', '.mov', '.mkv')):
                    determined_output_type = 'video'
                else:
                    log_message(f"[{execution_id}] 警告: 未知的文件类型: {first_valid_path}。默认为图片。") # 使用 log_message
                    determined_output_type = 'image'

                if output_type and determined_output_type != output_type:
                     log_message(f"[{execution_id}] 警告: 工作流输出节点类型 ({output_type}) 与实际文件类型 ({determined_output_type}) 不匹配。") # 使用 log_message

                log_message(f"[{execution_id}] 任务成功完成，返回类型 '{determined_output_type}' 和 {len(valid_paths)} 个有效路径。") # 使用 log_message
                return determined_output_type, valid_paths

            except json.JSONDecodeError as e:
                log_message(f"[{execution_id}] 读取或解析临时文件 JSON 失败: {e}. 文件内容: '{content[:100]}...'") # 使用 log_message
                time.sleep(check_interval * 2)
            except Exception as e:
                log_message(f"[{execution_id}] 处理临时文件时发生未知错误: {e}") # 使用 log_message
                try: os.remove(temp_file_path)
                except OSError: pass
                return None, None

        # 如果等待超过 N 秒仍未找到文件，记录一下 TEMP_DIR 的内容，帮助调试
        if not files_in_temp_dir_logged and (time.time() - start_time) > 5: # 例如等待5秒后
            try:
                temp_dir_contents = os.listdir(TEMP_DIR)
                log_message(f"[{execution_id}] 等待超过5秒，TEMP_DIR ('{TEMP_DIR}') 内容: {temp_dir_contents}")
            except Exception as e_dir:
                log_message(f"[{execution_id}] 无法列出 TEMP_DIR 内容: {e_dir}")
            files_in_temp_dir_logged = True # 避免重复记录

        time.sleep(check_interval)

    # 超时处理
    log_message(f"[{execution_id}] 等待临时文件超时 ({wait_timeout}秒)。TEMP_DIR ('{TEMP_DIR}') 最终内容可能已在上面记录。") # 使用 log_message
    return None, None # 超时，返回 None

# fuck and get_workflow_defaults_and_visibility are now imported from ui_def.
# The helper find_key_by_class_type_internal was moved to ui_def.py as it's used by them.

# --- 队列处理函数 (更新签名以包含动态组件列表) ---
def run_queued_tasks(
    inputimage1, input_video, 
    # Capture all dynamic positive prompts using *args or by naming them if MAX_DYNAMIC_COMPONENTS is fixed
    # Assuming run_button.click inputs are: input_image, input_video, *positive_prompt_texts, prompt_negative, ...
    # So, we need to capture these based on MAX_DYNAMIC_COMPONENTS
    # Let's define them explicitly for clarity up to MAX_DYNAMIC_COMPONENTS
    # This requires knowing the exact order from run_button.click
    # The order in run_button.click is:
    # input_image, input_video, 
    # *positive_prompt_texts, (size MAX_DYNAMIC_COMPONENTS)
    # prompt_negative, 
    # json_dropdown, hua_width, hua_height, 
    # *lora_dropdowns, (size MAX_DYNAMIC_COMPONENTS)
    # hua_checkpoint_dropdown, hua_unet_dropdown, 
    # *float_inputs, (size MAX_DYNAMIC_COMPONENTS)
    # *int_inputs, (size MAX_DYNAMIC_COMPONENTS)
    # seed_mode_dropdown, fixed_seed_input,
    # queue_count

    # We'll use *args and slicing for dynamic parts if function signature becomes too long,
    # or list them all if MAX_DYNAMIC_COMPONENTS is small and fixed.
    # For now, let's assume they are passed positionally and we'll reconstruct lists.
    # This is tricky. A better way is to pass *args to run_queued_tasks and then unpack.
    # Or, more simply, modify run_button.click to pass lists directly if Gradio allows.
    # Since Gradio passes them as individual args, we must list them or use *args.
    
    # Let's list them out based on run_button.click inputs:
    dynamic_prompt_1, dynamic_prompt_2, dynamic_prompt_3, dynamic_prompt_4, dynamic_prompt_5, # From *positive_prompt_texts
    prompt_text_negative, 
    json_file, 
    hua_width, hua_height, 
    dynamic_lora_1, dynamic_lora_2, dynamic_lora_3, dynamic_lora_4, dynamic_lora_5,       # From *lora_dropdowns
    hua_checkpoint, hua_unet, 
    dynamic_float_1, dynamic_float_2, dynamic_float_3, dynamic_float_4, dynamic_float_5, # From *float_inputs
    dynamic_int_1, dynamic_int_2, dynamic_int_3, dynamic_int_4, dynamic_int_5,         # From *int_inputs
    seed_mode, fixed_seed, 
    queue_count=1, progress=gr.Progress(track_tqdm=True)
):
    global accumulated_image_results, last_video_result, executor

    # Reconstruct lists for dynamic components
    dynamic_positive_prompts_values = [dynamic_prompt_1, dynamic_prompt_2, dynamic_prompt_3, dynamic_prompt_4, dynamic_prompt_5]
    dynamic_loras_values = [dynamic_lora_1, dynamic_lora_2, dynamic_lora_3, dynamic_lora_4, dynamic_lora_5]
    dynamic_float_nodes_values = [dynamic_float_1, dynamic_float_2, dynamic_float_3, dynamic_float_4, dynamic_float_5]
    dynamic_int_nodes_values = [dynamic_int_1, dynamic_int_2, dynamic_int_3, dynamic_int_4, dynamic_int_5]

    current_batch_image_results = []

    # 1. 将新任务加入队列
    if queue_count > 1:
        with results_lock:
            accumulated_image_results = []
            current_batch_image_results = []
            last_video_result = None # 批量任务开始时清除旧视频
    elif queue_count == 1:
         # 单任务模式，清除旧视频结果，图片结果将在成功后直接替换
         with results_lock:
             last_video_result = None

    # 将所有参数打包到 task_params_tuple for generate_image
    task_params_tuple = (
        inputimage1, input_video,
        dynamic_positive_prompts_values, # Pass the list
        prompt_text_negative,
        json_file,
        hua_width, hua_height,
        dynamic_loras_values,            # Pass the list
        hua_checkpoint, hua_unet,
        dynamic_float_nodes_values,      # Pass the list
        dynamic_int_nodes_values,        # Pass the list
        seed_mode, fixed_seed
    )
    log_message(f"[QUEUE_DEBUG] 接收到新任务请求 (种子模式: {seed_mode})。当前队列长度 (加锁前): {len(task_queue)}")
    with queue_lock:
        for _ in range(max(1, int(queue_count))):
            task_queue.append(task_params_tuple) 
        current_queue_size = len(task_queue)
        log_message(f"[QUEUE_DEBUG] 已添加 {queue_count} 个任务到队列。当前队列长度 (加锁后): {current_queue_size}")
    log_message(f"[QUEUE_DEBUG] 任务添加完成，释放锁。")

    # 初始状态更新：显示当前累积结果和队列信息
    # Default to results tab initially
    initial_updates = {
        queue_status_display: gr.update(value=f"队列中: {current_queue_size} | 处理中: {'是' if processing_event.is_set() else '否'}"),
        main_output_tabs_component: gr.Tabs(selected="tab_generate_result") # Default to results tab
    }
    with results_lock:
        initial_updates[output_gallery] = gr.update(value=accumulated_image_results[:])
        initial_updates[output_video] = gr.update(value=last_video_result)

    log_message(f"[QUEUE_DEBUG] 准备 yield 初始状态更新。队列: {current_queue_size}, 处理中: {processing_event.is_set()}")
    yield initial_updates
    log_message(f"[QUEUE_DEBUG] 已 yield 初始状态更新。")

    # 2. 检查是否已有进程在处理队列
    log_message(f"[QUEUE_DEBUG] 检查处理状态: processing_event.is_set() = {processing_event.is_set()}")
    if processing_event.is_set():
        log_message("[QUEUE_DEBUG] 已有任务在处理队列，新任务已排队。函数返回。")
        return

    # 3. 开始处理队列
    log_message(f"[QUEUE_DEBUG] 没有任务在处理，准备设置 processing_event 为 True。")
    processing_event.set()
    log_message(f"[QUEUE_DEBUG] processing_event 已设置为 True。开始处理循环。")

    def process_task(task_params):
        try:
            output_type, new_paths = generate_image(*task_params)
            return output_type, new_paths
        except Exception as e:
            log_message(f"[QUEUE_DEBUG] Exception in process_task: {e}")
            return None, None

    try:
        log_message("[QUEUE_DEBUG] Entering main processing loop (while True).")
        while True:
            task_to_run = None
            current_queue_size = 0
            log_message("[QUEUE_DEBUG] Checking queue for tasks (acquiring lock)...")
            with queue_lock:
                if task_queue:
                    task_to_run = task_queue.popleft()
                    current_queue_size = len(task_queue)
                    log_message(f"[QUEUE_DEBUG] Task popped from queue. Remaining: {current_queue_size}")
                else:
                    log_message("[QUEUE_DEBUG] Queue is empty. Breaking loop.")
                    break
            log_message("[QUEUE_DEBUG] Queue lock released.")

            if not task_to_run:
                 log_message("[QUEUE_DEBUG] Warning: No task found after lock release, but loop didn't break?")
                 continue

            # 更新状态：显示正在处理和队列大小
            with results_lock:
                current_images_copy = accumulated_image_results[:]
                current_video = last_video_result
            log_message(f"[QUEUE_DEBUG] Preparing to yield 'Processing' status. Queue: {current_queue_size}")
            yield {
                queue_status_display: gr.update(value=f"队列中: {current_queue_size} | 处理中: 是"),
                output_gallery: gr.update(value=current_images_copy),
                output_video: gr.update(value=current_video)
            }
            log_message(f"[QUEUE_DEBUG] Yielded 'Processing' status.")

            if task_to_run:
                log_message(f"[QUEUE_DEBUG] Starting execution for popped task. Remaining queue: {current_queue_size}")
                
                # --- KSampler Check and Tab Switch ---
                # task_to_run is the tuple: (inputimage1, input_video, dynamic_positive_prompts_values, 
                #                           prompt_text_negative, json_file, hua_width, hua_height, 
                #                           dynamic_loras_values, ...)
                # json_file is at index 4 of task_to_run (0-indexed)
                current_task_json_file = task_to_run[4] 
                should_switch_to_preview = False
                if current_task_json_file and isinstance(current_task_json_file, str): # Ensure it's a string before using
                    json_path_for_check = os.path.join(OUTPUT_DIR, current_task_json_file)
                    if os.path.exists(json_path_for_check):
                        try:
                            with open(json_path_for_check, "r", encoding="utf-8") as f_check:
                                workflow_prompt = json.load(f_check)
                            VALID_KSAMPLER_CLASS_TYPES = ["KSampler", "KSamplerAdvanced", "KSamplerSelect"]
                            for node_id, node_data in workflow_prompt.items():
                                class_type = node_data.get("class_type")
                                if isinstance(node_data, dict) and class_type in VALID_KSAMPLER_CLASS_TYPES:
                                    should_switch_to_preview = True
                                    log_message(f"[QUEUE_DEBUG] KSampler-like node (type: {class_type}) found in {current_task_json_file}. Will switch to preview tab.")
                                    break
                        except Exception as e_json_check:
                            log_message(f"[QUEUE_DEBUG] Error checking for KSampler in {current_task_json_file}: {e_json_check}")
                
                if should_switch_to_preview:
                    yield { main_output_tabs_component: gr.Tabs(selected="tab_k_sampler_preview") }
                # --- End KSampler Check ---

                progress(0, desc=f"处理任务 (队列剩余 {current_queue_size})")
                log_message(f"[QUEUE_DEBUG] Progress set to 0. Desc: Processing task (Queue remaining {current_queue_size})")
                
                # 提交任务到线程池
                future = executor.submit(process_task, task_to_run)
                log_message(f"[QUEUE_DEBUG] Task submitted to thread pool")

                task_interrupted_by_user = False
                # 等待任务完成，但每0.1秒检查一次，并检查中断信号
                while not future.done():
                    if interrupt_requested_event.is_set():
                        log_message("[QUEUE_DEBUG] User interrupt detected while waiting for future.")
                        task_interrupted_by_user = True
                        break
                    time.sleep(0.1)
                    # 在等待期间，也需要从 results_lock 中获取最新的累积结果
                    with results_lock:
                        current_images_while_waiting = accumulated_image_results[:]
                        current_video_while_waiting = last_video_result
                    yield {
                        queue_status_display: gr.update(value=f"队列中: {current_queue_size} | 处理中: 是 (运行中)"),
                        output_gallery: gr.update(value=current_images_while_waiting),
                        output_video: gr.update(value=current_video_while_waiting)
                    }
                
                if task_interrupted_by_user:
                    log_message("[QUEUE_DEBUG] Task was interrupted by user. Setting result to USER_INTERRUPTED.")
                    output_type, new_paths = "USER_INTERRUPTED", None
                    interrupt_requested_event.clear() # 清除标志

                    # --- 新增：尝试重置 executor ---
                    # global executor # 已在函数顶部声明
                    log_message("[QUEUE_DEBUG] Attempting to shutdown and recreate executor due to user interrupt.")
                    executor.shutdown(wait=False) 
                    executor = ThreadPoolExecutor(max_workers=1)
                    log_message("[QUEUE_DEBUG] Executor shutdown and recreated.")
                    # --- 新增结束 ---
                else:
                    try:
                        output_type, new_paths = future.result()
                        log_message(f"[QUEUE_DEBUG] Future completed. Type: {output_type}, Paths: {'Yes' if new_paths else 'No'}")
                    except Exception as e:
                        log_message(f"[QUEUE_DEBUG] Exception when getting future result: {e}")
                        output_type, new_paths = None, None # 任务执行出错
                
                progress(1) # 任务完成（无论成功与否，或被中断）
                log_message(f"[QUEUE_DEBUG] Progress set to 1.")

                if output_type == "USER_INTERRUPTED":
                    log_message("[QUEUE_DEBUG] Task was interrupted by user. Updating UI.")
                    # current_queue_size 已经是最新的（在 task_to_run = task_queue.popleft() 之后）
                    with results_lock:
                        current_images_copy = accumulated_image_results[:]
                        current_video = last_video_result
                    yield {
                        queue_status_display: gr.update(value=f"队列中: {current_queue_size} | 处理中: 否 (已中断)"),
                        output_gallery: gr.update(value=current_images_copy),
                        output_video: gr.update(value=current_video),
                    }
                    log_message(f"[QUEUE_DEBUG] Yielded USER_INTERRUPTED update. Queue: {current_queue_size}")
                    # 让循环继续，以便 finally 块可以正确清理 processing_event
                    # 如果这是最后一个任务，循环会在下一次迭代时自然结束

                elif output_type == "COMFYUI_REJECTED":
                    log_message("[QUEUE_DEBUG] Task rejected by ComfyUI backend or critical error in start_queue. Clearing remaining Gradio queue.")
                    with queue_lock:
                        task_queue.clear() # 清空Gradio队列中所有剩余任务
                        current_queue_size = len(task_queue) # 应为0
                    with results_lock:
                        current_images_copy = accumulated_image_results[:]
                        current_video = last_video_result
                    log_message(f"[QUEUE_DEBUG] Preparing to yield COMFYUI_REJECTED update. Queue: {current_queue_size}")
                    yield {
                         queue_status_display: gr.update(value=f"队列中: {current_queue_size} | 处理中: 是 (后端错误，队列已清空)"),
                         output_gallery: gr.update(value=current_images_copy),
                         output_video: gr.update(value=current_video),
                    }
                    log_message(f"[QUEUE_DEBUG] Yielded COMFYUI_REJECTED update. Loop will now check empty queue and exit to finally.")
                
                elif new_paths: # 任务成功且有结果 (output_type 不是 COMFYUI_REJECTED or USER_INTERRUPTED)
                    log_message(f"[QUEUE_DEBUG] Task successful, got {len(new_paths)} new paths of type '{output_type}'.")
                    update_dict = {}
                    with results_lock:
                        if output_type == 'image':
                            if queue_count == 1: # 单任务模式
                                accumulated_image_results = new_paths # 替换
                            else: # 批量任务模式
                                current_batch_image_results.extend(new_paths) # 累加到当前批次
                                accumulated_image_results = current_batch_image_results[:] # 更新全局累积结果
                            last_video_result = None # 清除旧视频（如果是图片任务）
                            update_dict[output_gallery] = gr.update(value=accumulated_image_results[:], visible=True)
                            update_dict[output_video] = gr.update(value=None, visible=False) # 隐藏视频输出
                        elif output_type == 'video':
                            last_video_result = new_paths[0] if new_paths else None # 视频只显示最新的一个
                            accumulated_image_results = [] # 清除旧图片（如果是视频任务）
                            update_dict[output_gallery] = gr.update(value=[], visible=False) # 隐藏图片输出
                            update_dict[output_video] = gr.update(value=last_video_result, visible=True) # 显示视频输出
                        else: # 未知类型 (理论上不应发生，因为 generate_image 控制了 output_type)
                             log_message(f"[QUEUE_DEBUG] Unknown or unexpected output type '{output_type}'. Treating as image.")
                             # 默认为图片处理或保持原样
                             accumulated_image_results.extend(new_paths) # 尝试添加
                             update_dict[output_gallery] = gr.update(value=accumulated_image_results[:])
                             update_dict[output_video] = gr.update(value=last_video_result)

                        log_message(f"[QUEUE_DEBUG] Updated results (lock acquired). Images: {len(accumulated_image_results)}, Video: {last_video_result is not None}")

                    update_dict[queue_status_display] = gr.update(value=f"队列中: {current_queue_size} | 处理中: 是 (完成)")
                    log_message(f"[QUEUE_DEBUG] Preparing to yield success update. Queue: {current_queue_size}")
                    yield update_dict
                    log_message(f"[QUEUE_DEBUG] Yielded success update.")
                else: # 任务失败 (output_type is None, or new_paths is None/empty but not COMFYUI_REJECTED)
                    log_message("[QUEUE_DEBUG] Task failed or returned no paths (general failure, not COMFYUI_REJECTED).")
                    with results_lock:
                        current_images_copy = accumulated_image_results[:]
                        current_video = last_video_result
                    log_message(f"[QUEUE_DEBUG] Preparing to yield general failure update. Queue: {current_queue_size}")
                    yield {
                         queue_status_display: gr.update(value=f"队列中: {current_queue_size} | 处理中: 是 (失败)"),
                         output_gallery: gr.update(value=current_images_copy),
                         output_video: gr.update(value=current_video),
                    }
                    log_message(f"[QUEUE_DEBUG] Yielded general failure update.")

    finally:
        log_message(f"[QUEUE_DEBUG] Entering finally block. Clearing processing_event (was {processing_event.is_set()}).")
        processing_event.clear()
        log_message(f"[QUEUE_DEBUG] processing_event cleared (is now {processing_event.is_set()}).")
        with queue_lock: current_queue_size = len(task_queue)
        with results_lock:
            final_images = accumulated_image_results[:]
            final_video = last_video_result
        log_message(f"[QUEUE_DEBUG] Preparing to yield final status update. Queue: {current_queue_size}, Processing: No. Switching to results tab.")
        yield {
            queue_status_display: gr.update(value=f"队列中: {current_queue_size} | 处理中: 否"),
            output_gallery: gr.update(value=final_images),
            output_video: gr.update(value=final_video),
            main_output_tabs_component: gr.Tabs(selected="tab_generate_result") # Switch back to results tab
        }
        log_message("[QUEUE_DEBUG] Yielded final status update. Exiting run_queued_tasks.")

# --- 赞助码处理函数 ---
def show_sponsor_code():
    sponsor_info = get_sponsor_html()
    # 返回一个更新指令，让 Markdown 组件可见并显示内容
    return gr.update(value=sponsor_info, visible=True)

# --- 清除函数 ---
def clear_queue():
    global task_queue, queue_lock, interrupt_requested_event, processing_event
    
    action_log_messages = [] # 用于 gr.Info()

    with queue_lock:
        is_currently_processing_a_task_in_comfyui = processing_event.is_set()
        num_tasks_waiting_in_gradio_queue = len(task_queue)

        log_message(f"[CLEAR_QUEUE] Entry. Gradio pending queue size: {num_tasks_waiting_in_gradio_queue}, ComfyUI processing active: {is_currently_processing_a_task_in_comfyui}")

        if is_currently_processing_a_task_in_comfyui and num_tasks_waiting_in_gradio_queue == 0:
            # 情况1: ComfyUI 正在处理一个任务 (该任务已从Gradio队列取出，在executor中运行), 
            # 且 Gradio 的等待队列为空。这是“仅剩当前任务”的情况，需要中断它。
            log_message("[CLEAR_QUEUE] Action: Interrupting the single, currently running ComfyUI task.")
            
            # 发送 HTTP 中断请求到 ComfyUI
            interrupt_comfyui_status_message = trigger_comfyui_interrupt() 
            action_log_messages.append(f"尝试中断 ComfyUI 当前任务: {interrupt_comfyui_status_message}")
            log_message(f"[CLEAR_QUEUE] ComfyUI interrupt triggered via HTTP: {interrupt_comfyui_status_message}")

            # 设置 Gradio 内部的中断标志。
            # run_queued_tasks 中的循环会检测到这个事件，并为正在运行的 future 对象进行相应处理。
            interrupt_requested_event.set()
            log_message("[CLEAR_QUEUE] Gradio internal interrupt_requested_event was SET.")
            
            # task_queue 此时应为空，无需 clear。
            
        elif num_tasks_waiting_in_gradio_queue > 0:
            # 情况2: Gradio 的等待队列中有任务。清除这些等待中的任务。
            # 不中断可能正在 ComfyUI 中运行的任务。
            cleared_count = num_tasks_waiting_in_gradio_queue
            task_queue.clear() # 清空 Gradio 的等待队列
            log_message(f"[CLEAR_QUEUE] Action: Cleared {cleared_count} task(s) from Gradio's queue. Any ComfyUI task currently processing was NOT interrupted by this action.")
            action_log_messages.append(f"已清除 Gradio 队列中的 {cleared_count} 个等待任务。")
            
            # 如果之前有一个外部中断请求的标志 (例如，通过已被移除的独立中断按钮设置的，理论上不太可能发生)
            # 并且我们这次 *没有* 尝试中断 ComfyUI，那么清除那个旧的标志是安全的。
            if interrupt_requested_event.is_set():
                interrupt_requested_event.clear()
                log_message("[CLEAR_QUEUE] Cleared a pre-existing interrupt_requested_event because we are only clearing the Gradio queue this time.")
        else:
            # 情况3: ComfyUI 没有在处理任务，Gradio 的等待队列也为空。没什么可做的。
            log_message("[CLEAR_QUEUE] Action: No tasks currently processing in ComfyUI and Gradio queue is empty. Nothing to clear or interrupt.")
            action_log_messages.append("队列已为空，无任务处理中。")

    # 通过 gr.Info() 显示操作摘要给用户
    if action_log_messages:
        gr.Info(" ".join(action_log_messages))

    # 更新队列状态的UI显示
    with queue_lock: # 重新获取锁以获得最新的队列大小 (如果清除了，应该是0)
        current_gradio_queue_size_for_display = len(task_queue) 
    
    # processing_event 的状态由 run_queued_tasks 的主循环和 finally 块管理。
    # 如果我们通过此函数中断了一个任务，run_queued_tasks 的 finally 块最终会清除 processing_event。
    # 如果我们只清除了等待队列，processing_event 对于正在运行任务的状态会保持，直到它自然完成或被其他方式中断。
    current_processing_status_for_display = processing_event.is_set()
    
    log_message(f"[CLEAR_QUEUE] Exit. Gradio queue size for display: {current_gradio_queue_size_for_display}, ComfyUI processing status for display: {current_processing_status_for_display}")
    
    return gr.update(value=f"队列中: {current_gradio_queue_size_for_display} | 处理中: {'是' if current_processing_status_for_display else '否'}")

def clear_history():
    global accumulated_image_results, last_video_result
    with results_lock:
        accumulated_image_results.clear()
        last_video_result = None
    log_message("图像和视频历史已清除。")
    with queue_lock: current_queue_size = len(task_queue)
    return {
        output_gallery: gr.update(value=[]), # 清空但不隐藏
        output_video: gr.update(value=None), # 清空但不隐藏
        queue_status_display: gr.update(value=f"队列中: {current_queue_size} | 处理中: {'是' if processing_event.is_set() else '否'}")
    }


# --- Gradio 界面 ---

# Combine imported HACKER_CSS with monitor CSS
combined_css = HACKER_CSS + "\n" + monitor_css

# 检查 Gradio 版本以支持向下兼容
GRADIO_VERSION = gr.__version__
GRADIO_SUPPORTS_NEW_API = version.parse(GRADIO_VERSION) >= version.parse("4.0.0")

with gr.Blocks() as demo:
    with gr.Tab("封装comfyui工作流"):
        with gr.Row():
           with gr.Column():  # 左侧列
               # --- 添加实时日志显示区域 (包含系统监控) ---
               with gr.Accordion("实时日志 (ComfyUI)", open=True, elem_classes="log-display-container"): # 保持日志区域打开
                   with gr.Group(elem_id="log_area_relative_wrapper"): # 新增内部 Group 用于定位系统监控
                       log_display = gr.Textbox(
                           label="日志输出",
                           lines=20,
                           max_lines=20,
                           autoscroll=True,
                           interactive=False,
                           elem_classes="log-display-container"
                       )
                       # 系统监控 HTML 输出组件
                       floating_monitor_html_output = gr.HTML(elem_classes="floating-monitor-outer-wrapper")
                
               image_accordion = gr.Accordion("上传图像 (折叠,有gradio传入图像节点才会显示上传)", visible=True, open=True)
               with image_accordion:
                   input_image = gr.Image(type="pil", label="上传图像", height=256, width=256)
    
               # --- 添加视频上传组件 ---
               video_accordion = gr.Accordion("上传视频 (折叠,有gradio传入视频节点才会显示上传)", visible=False, open=True) # 初始隐藏
               with video_accordion:
                   # 使用 filepath 类型，因为 ComfyUI 节点需要文件名
                   # sources=["upload"] 限制为仅上传
                   input_video = gr.Video(label="上传视频", sources=["upload"], height=256, width=256)
    
               with gr.Row():
                   with gr.Column(scale=3):
                       json_dropdown = gr.Dropdown(choices=get_json_files(), label="选择工作流")
                   with gr.Column(scale=1):
                       with gr.Column(scale=1): # 调整比例使按钮不至于太宽
                           refresh_button = gr.Button("🔄 刷新工作流")
                       with gr.Column(scale=1):
                           refresh_model_button = gr.Button("🔄 刷新模型")
    
    
    
               with gr.Row():
                   with gr.Accordion("正向提示文本(折叠)", open=True) as positive_prompt_col:
                       # prompt_positive = gr.Textbox(label="正向提示文本 1", elem_id="prompt_positive_1") # 将被动态组件取代
                       # prompt_positive_2 = gr.Textbox(label="正向提示文本 2", elem_id="prompt_positive_2")
                       # prompt_positive_3 = gr.Textbox(label="正向提示文本 3", elem_id="prompt_positive_3")
                       # prompt_positive_4 = gr.Textbox(label="正向提示文本 4", elem_id="prompt_positive_4")
                       # --- 动态正向提示词组件 ---
                       positive_prompt_texts = []
                       for i in range(MAX_DYNAMIC_COMPONENTS):
                           positive_prompt_texts.append(
                               gr.Textbox(label=f"正向提示 {i+1}", visible=False, elem_id=f"dynamic_positive_prompt_{i+1}")
                           )
                       # --- 动态正向提示词组件结束 ---
               with gr.Column() as negative_prompt_col: # 负向提示保持单个
                   prompt_negative = gr.Textbox(label="负向提示文本", elem_id="prompt_negative")
    
               with gr.Row() as resolution_row:
                   with gr.Column(scale=1):
                       resolution_dropdown = gr.Dropdown(choices=resolution_presets, label="分辨率预设", value=resolution_presets[0])
                   with gr.Column(scale=1):
                       with gr.Accordion("宽度和高度设置", open=False):
                           with gr.Column(scale=1):
                               hua_width = gr.Number(label="宽度", value=512, minimum=64, step=64, elem_id="hua_width_input")
                               hua_height = gr.Number(label="高度", value=512, minimum=64, step=64, elem_id="hua_height_input")
                               ratio_display = gr.Markdown("当前比例: 1:1")
                       with gr.Row():
                           with gr.Column(scale=1):
                              flip_btn = gr.Button("↔ 切换宽高")
    
    
    

    
    
    
    
    
    
    
           with gr.Column(): # 右侧列
               with gr.Tabs(elem_id="main_output_tabs") as main_output_tabs_component: # WRAPPER TABS
                   with gr.Tab("生成结果", id="tab_generate_result"):
                       output_gallery = gr.Gallery(label="生成图片结果", columns=3, height=600, preview=True, object_fit="contain", visible=False) # 保持原样
                       output_video = gr.Video(label="生成视频结果", height=600, autoplay=True, loop=True, visible=False) # 保持原样
                   with gr.Tab("k采样预览", id="tab_k_sampler_preview"):
                       with gr.Tab("实时预览"): # This is a nested Tab, not an issue for the parent switching
                           live_preview_image = gr.Image(label="实时预览", type="pil", interactive=False, height=512, show_label=False)
                       with gr.Tab("状态"): # This is a nested Tab
                           live_preview_status = gr.Textbox(label="预览状态", interactive=False, lines=2)
                   with gr.Tab("预览所有输出图片", id="tab_all_outputs_preview"):
                       output_preview_gallery = gr.Gallery(label="输出图片预览", columns=4, height="auto", preview=True, object_fit="contain")
                       load_output_button = gr.Button("加载输出图片")



    
               # --- 添加队列控制按钮 ---
               with gr.Row():
                   queue_status_display = gr.Markdown("队列中: 0 | 处理中: 否") # 移到按钮上方
    
               with gr.Row():
                   with gr.Row():
                       run_button = gr.Button("🚀 开始跑图 (加入队列)", variant="primary",elem_id="align-center")
                       clear_queue_button = gr.Button("🧹 清除队列",elem_id="align-center")
                       
    
                   with gr.Row():
                       clear_history_button = gr.Button("🗑️ 清除显示历史")
                        # --- 添加赞助按钮和显示区域 ---
                       sponsor_button = gr.Button("💖 赞助作者")
                   with gr.Row():
                       queue_count = gr.Number(label="队列数量", value=1, minimum=1, step=1, precision=0)

    
    
    
    
               sponsor_display = gr.Markdown(visible=False) # 初始隐藏
               with gr.Row():

                   # interrupt_action_status Textbox 已移除，将通过 gr.Info() 显示弹窗
                   with gr.Column(scale=1, visible=False) as seed_options_col: # 种子选项列，初始隐藏
                       seed_mode_dropdown = gr.Dropdown(
                           choices=["随机", "递增", "递减", "固定"],
                           value="随机",
                           label="种子模式",
                           elem_id="seed_mode_dropdown"
                       )
                       fixed_seed_input = gr.Number(
                           label="固定种子值",
                           value=0,
                           minimum=0,
                           maximum=0xffffffff, # Max unsigned 32-bit int
                           step=1,
                           precision=0,
                           visible=False, # 初始隐藏，仅在模式为 "固定" 时显示
                           elem_id="fixed_seed_input"
                       )
                       
                   with gr.Column(scale=1):
                       hua_unet_dropdown = gr.Dropdown(choices=unet_list, label="选择 UNet 模型", value="None", elem_id="hua_unet_dropdown", visible=False) # 初始隐藏


               with gr.Row():
                   with gr.Column(scale=1):
                       # hua_lora_dropdown = gr.Dropdown(choices=lora_list, label="选择 Lora 模型 1", value="None", elem_id="hua_lora_dropdown", visible=False) # 初始隐藏
                       # hua_lora_dropdown_2 = gr.Dropdown(choices=lora_list, label="选择 Lora 模型 2", value="None", elem_id="hua_lora_dropdown_2", visible=False) # 新增，初始隐藏
                       # hua_lora_dropdown_3 = gr.Dropdown(choices=lora_list, label="选择 Lora 模型 3", value="None", elem_id="hua_lora_dropdown_3", visible=False) # 新增，初始隐藏
                       # hua_lora_dropdown_4 = gr.Dropdown(choices=lora_list, label="选择 Lora 模型 4", value="None", elem_id="hua_lora_dropdown_4", visible=False) # 新增，初始隐藏
                       # --- 动态 Lora 下拉框 ---
                       lora_dropdowns = []
                       for i in range(MAX_DYNAMIC_COMPONENTS):
                           lora_dropdowns.append(
                               gr.Dropdown(choices=lora_list, label=f"Lora {i+1}", value="None", visible=False, elem_id=f"dynamic_lora_dropdown_{i+1}")
                           )
                       # --- 动态 Lora 下拉框结束 ---
                   with gr.Column(scale=1): # Checkpoint 和 Unet 保持单例
                       hua_checkpoint_dropdown = gr.Dropdown(choices=checkpoint_list, label="选择 Checkpoint 模型", value="None", elem_id="hua_checkpoint_dropdown", visible=False) # 初始隐藏


               # --- 添加 Float 和 Int 输入组件 (初始隐藏) ---
               with gr.Row() as float_int_row: # 保持此行用于整体可见性控制（如果需要）
                    with gr.Column(scale=1):
                        # hua_float_input = gr.Number(label="浮点数输入 (Float)", visible=False, elem_id="hua_float_input")
                        # hua_float_input_2 = gr.Number(label="浮点数输入 2 (Float)", visible=False, elem_id="hua_float_input_2")
                        # hua_float_input_3 = gr.Number(label="浮点数输入 3 (Float)", visible=False, elem_id="hua_float_input_3")
                        # hua_float_input_4 = gr.Number(label="浮点数输入 4 (Float)", visible=False, elem_id="hua_float_input_4")
                        # --- 动态 Float 输入 ---
                        float_inputs = []
                        for i in range(MAX_DYNAMIC_COMPONENTS):
                            float_inputs.append(
                                gr.Number(label=f"浮点数 {i+1}", visible=False, elem_id=f"dynamic_float_input_{i+1}")
                            )
                    # --- 动态 Float 输入结束 ---
                    with gr.Column(scale=1):
                        # hua_int_input = gr.Number(label="整数输入 (Int)", precision=0, visible=False, elem_id="hua_int_input") # precision=0 for integer
                        # hua_int_input_2 = gr.Number(label="整数输入 2 (Int)", precision=0, visible=False, elem_id="hua_int_input_2")
                        # hua_int_input_3 = gr.Number(label="整数输入 3 (Int)", precision=0, visible=False, elem_id="hua_int_input_3")
                        # hua_int_input_4 = gr.Number(label="整数输入 4 (Int)", precision=0, visible=False, elem_id="hua_int_input_4")
                        # --- 动态 Int 输入 ---
                        int_inputs = []
                        for i in range(MAX_DYNAMIC_COMPONENTS):
                            int_inputs.append(
                                gr.Number(label=f"整数 {i+1}", precision=0, visible=False, elem_id=f"dynamic_int_input_{i+1}")
                            )
                        # --- 动态 Int 输入结束 ---


               with gr.Row():
                   # interrupt_button_main_tab 已被移除
                   gr.Markdown('我要打十个') # 保留这句骚话                       
                   
               # with gr.Row(): # queue_status_display 已移到上方
               #     with gr.Column(scale=1):
               #         queue_status_display = gr.Markdown("队列中: 0 | 处理中: 否")
               
               

    with gr.Tab("设置"):
        with gr.Column(): # 使用 Column 布局

            gr.Markdown("## 🎛️ ComfyUI 节点徽章控制")
            gr.Markdown("控制 ComfyUI 界面中节点 ID 徽章的显示方式。设置完成请刷新comfyui界面即可。")
            node_badge_mode_radio = gr.Radio(
                choices=["Show all", "Hover", "None"],
                value="Show all", # 默认值可以尝试从 ComfyUI 获取，但这里先设为 Show all
                label="选择节点 ID 徽章显示模式"
            )
            node_badge_output_text = gr.Textbox(label="更新结果", interactive=False)

            # 将事件处理移到 UI 定义之后
            node_badge_mode_radio.change(
                fn=update_node_badge_mode,
                inputs=node_badge_mode_radio,
                outputs=node_badge_output_text
            )
            # TODO: 添加一个按钮或在加载时尝试获取当前设置并更新 Radio 的 value

            gr.Markdown("---") # 添加分隔线
            gr.Markdown("## ⚡ ComfyUI 控制")
            gr.Markdown("重启 ComfyUI 或中断当前正在执行的任务。")

            with gr.Row():
                reboot_button = gr.Button("🔄 重启ComfyUI")
                # interrupt_button (原位置) 已被移除

            reboot_output = gr.Textbox(label="重启结果", interactive=False)
            # interrupt_output (原位置) 已被移除

            # 将事件处理移到 UI 定义之后
            reboot_button.click(fn=reboot_manager, inputs=[], outputs=[reboot_output])
            # interrupt_button.click (原位置) 已被移除



            gr.Markdown("## ⚙️ 插件核心设置") 
            gr.Markdown("---")

            gr.Markdown("### 🎨 动态组件数量")
            gr.Markdown(
                "设置在UI中为正向提示、Lora、浮点数和整数输入动态生成的组件的最大数量。\n"
                "**注意：此更改将在下次启动插件 (或重启 ComfyUI) 后生效，以改变实际显示的组件数量。**"
            )
            
            # UI组件的初始值也从配置文件读取，确保显示的是当前生效的或即将生效的配置
            initial_max_comp_for_ui = load_plugin_settings().get("max_dynamic_components", DEFAULT_MAX_DYNAMIC_COMPONENTS)
            
            max_dynamic_components_input = gr.Number(
                label="最大动态组件数量 (1-20)", 
                value=initial_max_comp_for_ui, 
                minimum=1, 
                maximum=20, # 设定一个合理的上限
                step=1, 
                precision=0,
                elem_id="max_dynamic_components_setting_input"
            )
            save_max_components_button = gr.Button("保存动态组件数量设置")
            max_components_save_status = gr.Markdown("", elem_id="max_components_save_status_md") # 用于显示保存状态和提示

            def handle_save_max_components(new_max_value_from_input):
                try:
                    # Gradio Number input might pass a float if not careful, ensure int
                    new_max_value = int(float(new_max_value_from_input)) 
                    if not (1 <= new_max_value <= 20): # 后端再次验证范围
                        return gr.update(value="<p style='color:red;'>错误：值必须介于 1 和 20 之间。</p>")
                except ValueError:
                    return gr.update(value="<p style='color:red;'>错误：请输入一个有效的整数。</p>")

                # 重新加载当前设置，以防其他设置项被意外覆盖（如果未来有其他设置项）
                current_settings = load_plugin_settings() 
                current_settings["max_dynamic_components"] = new_max_value
                status_message = save_plugin_settings(current_settings)
                
                # 更新全局MAX_DYNAMIC_COMPONENTS，主要用于确保get_workflow_defaults_and_visibility在同一次会话中如果被调用能拿到新值
                # 但这不会改变已经实例化的Gradio组件数量
                # global MAX_DYNAMIC_COMPONENTS
                # MAX_DYNAMIC_COMPONENTS = new_max_value 
                # print(f"UI中更新了max_dynamic_components的配置，新值为: {new_max_value}。重启后生效于UI组件数量。")

                return gr.update(value=f"<p style='color:green;'>{status_message} 请重启插件或 ComfyUI 以使更改生效。</p>")

            save_max_components_button.click(
                fn=handle_save_max_components,
                inputs=[max_dynamic_components_input],
                outputs=[max_components_save_status]
            )
            
            gr.Markdown("---") # 分隔线

    with gr.Tab("信息"):
        with gr.Column():
            gr.Markdown("### ℹ️ 插件与开发者信息") # 添加标题

            # GitHub Repo Button
            github_repo_btn = gr.Button("本插件 GitHub 仓库")
            gitthub_display = gr.Markdown(visible=False) # 此选项卡中用于显示链接的区域
            github_repo_btn.click(lambda: gr.update(value="https://github.com/kungful/ComfyUI_to_webui.git",visible=True), inputs=[], outputs=[gitthub_display]) # 修正: 指向 gitthub_display

            # Free Mirror Button
            free_mirror_btn = gr.Button("开发者的免费镜像")
            free_mirror_diplay = gr.Markdown(visible=False) # 此选项卡中用于显示链接的区域
            free_mirror_btn.click(lambda: gr.update(value="https://www.xiangongyun.com/image/detail/7b36c1a3-da41-4676-b5b3-03ec25d6e197",visible=True), inputs=[], outputs=[free_mirror_diplay]) # 修正: 指向 free_mirror_diplay

            # Sponsor Button & Display Area
            sponsor_info_btn = gr.Button("💖 赞助开发者")
            info_sponsor_display = gr.Markdown(visible=False) # 此选项卡中用于显示赞助信息的区域
            sponsor_info_btn.click(fn=show_sponsor_code, inputs=[], outputs=[info_sponsor_display]) # 目标新的显示区域

            # Contact Button & Display Area
            contact_btn = gr.Button("开发者联系方式")
            contact_display = gr.Markdown(visible=False) # 联系信息显示区域
            # 使用 lambda 更新 Markdown 组件的值并使其可见
            contact_btn.click(lambda: gr.update(value="**邮箱:** blenderkrita@gmail.com", visible=True), inputs=[], outputs=[contact_display])

            # Tutorial Button
            tutorial_btn = gr.Button("使用教程 (GitHub)")
            tutorial_display = gr.Markdown(visible=False) # 此选项卡中用于显示链接的区域
            tutorial_btn.click(lambda: gr.update(value="https://github.com/kungful/ComfyUI_to_webui.git",visible=True), inputs=[], outputs=[tutorial_display]) # 修正: 指向 tutorial_display

            # 添加一些间距或说明
            gr.Markdown("---")
            gr.Markdown("点击上方按钮获取相关信息或跳转链接。")

    with gr.Tab("API JSON 管理"):
        define_api_json_management_ui()


    # --- 事件处理 ---

    def refresh_workflow_and_ui(current_selected_json_file):
        log_message(f"[REFRESH_WORKFLOW_UI] Triggered. Current selection: {current_selected_json_file}")
        
        new_json_choices = get_json_files()
        log_message(f"[REFRESH_WORKFLOW_UI] New JSON choices: {new_json_choices}")

        json_to_load_for_ui_update = None
        
        if current_selected_json_file and current_selected_json_file in new_json_choices:
            json_to_load_for_ui_update = current_selected_json_file
            log_message(f"[REFRESH_WORKFLOW_UI] Current selection '{current_selected_json_file}' is still valid.")
        elif new_json_choices:
            json_to_load_for_ui_update = new_json_choices[0]
            log_message(f"[REFRESH_WORKFLOW_UI] Current selection '{current_selected_json_file}' is invalid or not present. Defaulting to first new choice: '{json_to_load_for_ui_update}'.")
        else:
            # No JSON files available at all
            log_message(f"[REFRESH_WORKFLOW_UI] No JSON files available after refresh.")
            # update_ui_on_json_change(None) will handle hiding/resetting components.

        # Get the UI updates based on the json_to_load_for_ui_update
        # update_ui_on_json_change returns a tuple of gr.update objects
        ui_updates_tuple = update_ui_on_json_change(json_to_load_for_ui_update)
        
        # The first part of the return will be the update for the json_dropdown itself
        dropdown_update = gr.update(choices=new_json_choices, value=json_to_load_for_ui_update)
        
        # Combine the dropdown update with the rest of the UI updates
        final_updates = (dropdown_update,) + ui_updates_tuple
        log_message(f"[REFRESH_WORKFLOW_UI] Returning {len(final_updates)} updates. Dropdown will be set to '{json_to_load_for_ui_update}'.")
        return final_updates

    # --- 节点徽章设置事件 (已在 Tab 内定义) ---
    # node_badge_mode_radio.change(fn=update_node_badge_mode, inputs=node_badge_mode_radio, outputs=node_badge_output_text)

    # --- 其他事件处理 ---
    resolution_dropdown.change(fn=update_from_preset, inputs=resolution_dropdown, outputs=[resolution_dropdown, hua_width, hua_height, ratio_display])
    hua_width.change(fn=update_from_inputs, inputs=[hua_width, hua_height], outputs=[resolution_dropdown, ratio_display])
    hua_height.change(fn=update_from_inputs, inputs=[hua_width, hua_height], outputs=[resolution_dropdown, ratio_display])
    flip_btn.click(fn=flip_resolution, inputs=[hua_width, hua_height], outputs=[hua_width, hua_height])

    # JSON 下拉菜单改变时，更新所有相关组件的可见性、默认值 + 输出区域可见性
    def update_ui_on_json_change(json_file):
        defaults = get_workflow_defaults_and_visibility(json_file, OUTPUT_DIR, resolution_prefixes, resolution_presets, MAX_DYNAMIC_COMPONENTS)
        
        updates = []

        # 单例组件
        updates.append(gr.update(visible=defaults["visible_image_input"]))
        updates.append(gr.update(visible=defaults["visible_video_input"]))
        updates.append(gr.update(visible=defaults["visible_neg_prompt"], value=defaults["default_neg_prompt"]))
        
        updates.append(gr.update(visible=defaults["visible_resolution"])) # resolution_row
        closest_preset = find_closest_preset(defaults["default_width"], defaults["default_height"], resolution_presets, resolution_prefixes)
        ratio_str = calculate_aspect_ratio(defaults["default_width"], defaults["default_height"])
        ratio_display_text = f"当前比例: {ratio_str}"
        updates.append(gr.update(value=closest_preset)) # resolution_dropdown
        updates.append(gr.update(value=defaults["default_width"])) # hua_width
        updates.append(gr.update(value=defaults["default_height"])) # hua_height
        updates.append(gr.update(value=ratio_display_text)) # ratio_display

        updates.append(gr.update(visible=defaults["visible_checkpoint"], value=defaults["default_checkpoint"]))
        updates.append(gr.update(visible=defaults["visible_unet"], value=defaults["default_unet"]))
        updates.append(gr.update(visible=defaults["visible_seed_indicator"])) # seed_options_col
        updates.append(gr.update(visible=defaults["visible_image_output"])) # output_gallery
        updates.append(gr.update(visible=defaults["visible_video_output"])) # output_video

        # 动态组件: GradioTextOk (positive_prompt_texts)
        dynamic_prompts_data = defaults["dynamic_components"]["GradioTextOk"]
        for i in range(MAX_DYNAMIC_COMPONENTS):
            if i < len(dynamic_prompts_data):
                node_data = dynamic_prompts_data[i]
                label = node_data.get("title", f"正向提示 {i+1}")
                if label == node_data.get("id"): # if title was just node id
                    label = f"正向提示 {i+1} (ID: {node_data.get('id')})"
                updates.append(gr.update(visible=True, label=label, value=node_data.get("value", "")))
            else:
                updates.append(gr.update(visible=False, label=f"正向提示 {i+1}", value=""))
        
        # 动态组件: Hua_LoraLoaderModelOnly (lora_dropdowns)
        dynamic_loras_data = defaults["dynamic_components"]["Hua_LoraLoaderModelOnly"]
        # 获取当前的 Lora 列表用于检查
        current_lora_list = get_model_list("loras") # <--- 获取最新列表
        print(f"[UI_UPDATE_DEBUG] Current Lora list for validation: {current_lora_list[:5]}... (Total: {len(current_lora_list)})") # 打印部分列表用于调试

        for i in range(MAX_DYNAMIC_COMPONENTS):
            if i < len(dynamic_loras_data):
                node_data = dynamic_loras_data[i]
                lora_value_from_json = node_data.get("value", "None")
                label = node_data.get("title", f"Lora {i+1}")
                if label == node_data.get("id"):
                    label = f"Lora {i+1} (ID: {node_data.get('id')})"

                # --- 新增检查和日志 ---
                final_lora_value_to_set = "None" # 默认值
                if lora_value_from_json != "None":
                    if lora_value_from_json in current_lora_list:
                        final_lora_value_to_set = lora_value_from_json
                        print(f"[UI_UPDATE_DEBUG] Lora {i+1} (ID: {node_data['id']}): Value '{lora_value_from_json}' found in list. Setting dropdown.")
                    else:
                        print(f"[UI_UPDATE_DEBUG] Lora {i+1} (ID: {node_data['id']}): Value '{lora_value_from_json}' NOT FOUND in current Lora list. Setting dropdown to 'None'.")
                else:
                     print(f"[UI_UPDATE_DEBUG] Lora {i+1} (ID: {node_data['id']}): Value from JSON is 'None'. Setting dropdown to 'None'.")
                # --- 检查和日志结束 ---

                updates.append(gr.update(visible=True, label=label, value=final_lora_value_to_set)) # <--- 使用检查后的值
            else:
                updates.append(gr.update(visible=False, label=f"Lora {i+1}", value="None"))

        # --- 为分辨率添加日志 ---
        print(f"[UI_UPDATE_DEBUG] Resolution: Setting Width={defaults['default_width']}, Height={defaults['default_height']}")
        # --- 日志结束 ---

        # 动态组件: HuaIntNode (int_inputs)
        dynamic_ints_data = defaults["dynamic_components"]["HuaIntNode"]
        for i in range(MAX_DYNAMIC_COMPONENTS):
            if i < len(dynamic_ints_data):
                node_data = dynamic_ints_data[i]
                node_id = node_data.get("id")
                node_title = node_data.get("title")
                # 获取来自 inputs["name"] 的值，假设它被 get_workflow_defaults_and_visibility 传递为 name_from_node
                input_name_prefix = node_data.get("name_from_node")

                label_parts = []
                if input_name_prefix: # 如果 JSON 中定义了 name
                    label_parts.append(input_name_prefix)

                # 添加节点本身的标题或通用名称
                # 如果有 input_name_prefix，node_title 更多是作为补充说明
                if node_title and node_title != node_id:
                    label_parts.append(node_title)
                elif not input_name_prefix: # 只有在没有 name 前缀时，才考虑添加通用描述符 "整数"
                    label_parts.append(f"整数")

                # 确保标签不为空，并添加 ID
                if not label_parts: # 极端情况下的回退
                    label_parts.append(f"整数 {i+1}")
                
                label = " - ".join(label_parts) + f" (ID: {node_id})"
                
                updates.append(gr.update(visible=True, label=label, value=node_data.get("value", 0)))
            else:
                updates.append(gr.update(visible=False, label=f"整数 {i+1}", value=0))

        # 动态组件: HuaFloatNode (float_inputs)
        dynamic_floats_data = defaults["dynamic_components"]["HuaFloatNode"]
        for i in range(MAX_DYNAMIC_COMPONENTS):
            if i < len(dynamic_floats_data):
                node_data = dynamic_floats_data[i]
                node_id = node_data.get("id")
                node_title = node_data.get("title")
                # 获取来自 inputs["name"] 的值，假设它被 get_workflow_defaults_and_visibility 传递为 name_from_node
                input_name_prefix = node_data.get("name_from_node")

                label_parts = []
                if input_name_prefix: # 如果 JSON 中定义了 name
                    label_parts.append(input_name_prefix)

                # 添加节点本身的标题或通用名称
                # 如果有 input_name_prefix，node_title 更多是作为补充说明
                if node_title and node_title != node_id:
                    label_parts.append(node_title)
                elif not input_name_prefix: # 只有在没有 name 前缀时，才考虑添加通用描述符 "浮点数"
                    label_parts.append(f"浮点数")

                # 确保标签不为空，并添加 ID
                if not label_parts: # 极端情况下的回退
                    label_parts.append(f"浮点数 {i+1}")
                
                label = " - ".join(label_parts) + f" (ID: {node_id})"
                
                updates.append(gr.update(visible=True, label=label, value=node_data.get("value", 0.0)))
            else:
                updates.append(gr.update(visible=False, label=f"浮点数 {i+1}", value=0.0))
        
        return tuple(updates)

    json_dropdown.change(
        fn=update_ui_on_json_change,
        inputs=json_dropdown,
        outputs=[ 
            image_accordion, video_accordion, prompt_negative,
            resolution_row, resolution_dropdown, hua_width, hua_height, ratio_display,
            hua_checkpoint_dropdown, hua_unet_dropdown, seed_options_col,
            output_gallery, output_video,
            # Spread out the dynamic component lists into the outputs
            *positive_prompt_texts,
            *lora_dropdowns,
            *int_inputs,
            *float_inputs
        ]
    )

    # --- 新增：根据种子模式显示/隐藏固定种子输入框 ---
    def toggle_fixed_seed_input(mode):
        return gr.update(visible=(mode == "固定"))

    seed_mode_dropdown.change(
        fn=toggle_fixed_seed_input,
        inputs=seed_mode_dropdown,
        outputs=fixed_seed_input
    )
    # --- 新增结束 ---

    refresh_button.click(
        fn=refresh_workflow_and_ui,
        inputs=[json_dropdown], # Pass the current value of json_dropdown
        outputs=[
            json_dropdown, # First output is for the dropdown itself
            # Then all the outputs that update_ui_on_json_change targets
            image_accordion, video_accordion, prompt_negative,
            resolution_row, resolution_dropdown, hua_width, hua_height, ratio_display,
            hua_checkpoint_dropdown, hua_unet_dropdown, seed_options_col,
            output_gallery, output_video,
            *positive_prompt_texts,
            *lora_dropdowns,
            *int_inputs,
            *float_inputs
        ]
    )

    # get_output_images is imported, needs OUTPUT_DIR
    load_output_button.click(fn=lambda: get_output_images(OUTPUT_DIR), inputs=[], outputs=output_preview_gallery)

    # --- 修改运行按钮的点击事件 ---
    run_button.click(
        fn=run_queued_tasks,
        inputs=[
            input_image, input_video, 
            # Pass the lists of dynamic components directly
            *positive_prompt_texts, 
            prompt_negative, # Single negative prompt
            json_dropdown, hua_width, hua_height, 
            *lora_dropdowns,
            hua_checkpoint_dropdown, hua_unet_dropdown, 
            *float_inputs, 
            *int_inputs,
            seed_mode_dropdown, fixed_seed_input,
            queue_count
        ],
        outputs=[queue_status_display, output_gallery, output_video, main_output_tabs_component]
    )
    
    # interrupt_button_main_tab.click 事件处理器已被移除

    # --- 添加新按钮的点击事件 ---
    clear_queue_button.click(fn=clear_queue, inputs=[], outputs=[queue_status_display])
    clear_history_button.click(fn=clear_history, inputs=[], outputs=[output_gallery, output_video, queue_status_display])
    sponsor_button.click(fn=show_sponsor_code, inputs=[], outputs=[sponsor_display])

    refresh_model_button.click(
        lambda: tuple(
            [gr.update(choices=get_model_list("loras")) for _ in range(MAX_DYNAMIC_COMPONENTS)] +
            [gr.update(choices=get_model_list("checkpoints")), gr.update(choices=get_model_list("unet"))]
        ),
        inputs=[],
        outputs=[*lora_dropdowns, hua_checkpoint_dropdown, hua_unet_dropdown]
    )

    # --- 初始加载 ---
    def on_load_setup():
        json_files = get_json_files()
        # The number of outputs from update_ui_on_json_change is now:
        # 13 (single instance UI elements) + 4 * MAX_DYNAMIC_COMPONENTS (dynamic elements)
        # = 13 + 4 * 5 = 13 + 20 = 33
        
        if not json_files:
            print("未找到 JSON 文件，隐藏所有动态组件并设置默认值")
            
            initial_updates = [
                gr.update(visible=False), # image_accordion
                gr.update(visible=False), # video_accordion
                gr.update(visible=False, value=""), # prompt_negative
                gr.update(visible=False), # resolution_row
                gr.update(value="custom"), # resolution_dropdown
                gr.update(value=512),      # hua_width
                gr.update(value=512),      # hua_height
                gr.update(value="当前比例: 1:1"), # ratio_display
                gr.update(visible=False, value="None"), # hua_checkpoint_dropdown
                gr.update(visible=False, value="None"), # hua_unet_dropdown
                gr.update(visible=False), # seed_options_col
                gr.update(visible=False), # output_gallery
                gr.update(visible=False)  # output_video
            ]
            # Add updates for dynamic components (all hidden)
            for _ in range(MAX_DYNAMIC_COMPONENTS): # positive_prompt_texts
                initial_updates.append(gr.update(visible=False, label="正向提示", value=""))
            for _ in range(MAX_DYNAMIC_COMPONENTS): # lora_dropdowns
                initial_updates.append(gr.update(visible=False, label="Lora", value="None"))
            for _ in range(MAX_DYNAMIC_COMPONENTS): # int_inputs
                initial_updates.append(gr.update(visible=False, label="整数", value=0))
            for _ in range(MAX_DYNAMIC_COMPONENTS): # float_inputs
                initial_updates.append(gr.update(visible=False, label="浮点数", value=0.0))
            return tuple(initial_updates)
        else:
            default_json = json_files[0]
            print(f"初始加载，检查默认 JSON: {default_json}")
            return update_ui_on_json_change(default_json) # This now returns a tuple of gr.update calls

    demo.load(
        fn=on_load_setup,
        inputs=[],
        outputs=[ # This list must exactly match the components updated by on_load_setup / update_ui_on_json_change
            image_accordion, video_accordion, prompt_negative,
            resolution_row, resolution_dropdown, hua_width, hua_height, ratio_display,
            hua_checkpoint_dropdown, hua_unet_dropdown, seed_options_col,
            output_gallery, output_video,
            *positive_prompt_texts,
            *lora_dropdowns,
            *int_inputs,
            *float_inputs
        ]
    )

    # --- 添加日志轮询 Timer ---
    # 每 0.1 秒调用 fetch_and_format_logs，并将结果输出到 log_display (加快刷新以改善滚动)
    log_timer = gr.Timer(0.1, active=True)  # 每 0.1 秒触发一次
    log_timer.tick(fetch_and_format_logs, inputs=None, outputs=log_display)

    # --- 系统监控流加载 ---
    # outputs 需要指向在 gr.Blocks 内定义的 floating_monitor_html_output 实例
    # 确保 floating_monitor_html_output 变量在 demo.load 调用时是可访问的
    # (它是在 with gr.Blocks(...) 上下文中定义的，所以 demo 对象知道它)
    demo.load(fn=update_floating_monitors_stream, inputs=None, outputs=[floating_monitor_html_output], show_progress="hidden")

    # --- ComfyUI 实时预览加载 ---
    demo.load(
        fn=comfyui_previewer.get_update_generator(),
        inputs=[],
        outputs=[live_preview_image, live_preview_status],
        show_progress="hidden" # 通常预览不需要进度条
    )
    # 启动预览器的工作线程
    # demo.load(fn=comfyui_previewer.start_worker, inputs=[], outputs=[], show_progress="hidden")
    # 直接在 Gradio 线程启动后调用 start_worker 更可靠
    # 或者在 on_load_setup 中调用


    # --- Gradio 启动代码 ---
def luanch_gradio(demo_instance): # 接收 demo 实例
    # 在 Gradio 启动前启动预览器工作线程
    print("准备启动 ComfyUIPreviewer 工作线程...")
    comfyui_previewer.start_worker()
    print("ComfyUIPreviewer 工作线程已请求启动。")

    try:
        # 尝试查找可用端口，从 7861 开始
        port = 7861
        while True:
            try:
                # share=True 会尝试创建公网链接，可能需要登录 huggingface
                # server_name="0.0.0.0" 允许局域网访问
                # 根据 Gradio 版本调整参数传递方式
                launch_kwargs = {
                    "server_name": "0.0.0.0",
                    "server_port": port,
                    "share": False,
                    "prevent_thread_lock": True
                }
                # Gradio 6.0+ 将 css 移到 launch() 方法
                if GRADIO_SUPPORTS_NEW_API:
                    launch_kwargs["css"] = combined_css
                demo_instance.launch(**launch_kwargs)
                print(f"Gradio 界面已在 http://127.0.0.1:{port} (或局域网 IP) 启动")
                # 启动成功后打开本地链接
                webbrowser.open(f"http://127.0.0.1:{port}/")
                break # 成功启动，退出循环
            except OSError as e:
                if "address already in use" in str(e).lower():
                    print(f"端口 {port} 已被占用，尝试下一个端口...")
                    port += 1
                    if port > 7870: # 限制尝试范围
                        print("无法找到可用端口 (7861-7870)。")
                        break
                else:
                    print(f"启动 Gradio 时发生未知 OS 错误: {e}")
                    break # 其他 OS 错误，退出
            except Exception as e:
                 print(f"启动 Gradio 时发生未知错误: {e}")
                 break # 其他错误，退出
    except Exception as e:
        print(f"执行 luanch_gradio 时出错: {e}")


# 使用守护线程，这样主程序退出时 Gradio 线程也会退出
gradio_thread = threading.Thread(target=luanch_gradio, args=(demo,), daemon=True)
gradio_thread.start()

# 注册 atexit 清理函数，以在程序退出时停止 previewer worker
def cleanup_previewer_on_exit():
    print("Gradio 应用正在关闭，尝试停止 ComfyUIPreviewer 工作线程...")
    if comfyui_previewer:
        comfyui_previewer.stop_worker()
    print("ComfyUIPreviewer 工作线程已请求停止。")

atexit.register(cleanup_previewer_on_exit)


# 主线程可以继续执行其他任务或等待，这里简单地保持运行
# 注意：如果这是插件的一部分，主线程可能是 ComfyUI 本身，不需要无限循环
# print("主线程继续运行... 按 Ctrl+C 退出。")
# try:
#     while True:
#         time.sleep(1)
# except KeyboardInterrupt:
#     print("收到退出信号，正在关闭...")
#     # demo.close() # 关闭 Gradio 服务 (如果需要手动关闭)
#     # cleanup_previewer_on_exit() # 手动调用清理 (atexit 应该会处理)
