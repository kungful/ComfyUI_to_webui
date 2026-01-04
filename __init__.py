import subprocess
import importlib
import sys
import os
import platform # 移到这里，因为下面的代码需要它
import json
import re
import folder_paths
import server
from aiohttp import web
import os
# 强制让当前进程认为没有代理
os.environ.pop('http_proxy', None)
os.environ.pop('https_proxy', None)
os.environ.pop('all_proxy', None)
# --- 改进的自动依赖安装 ---
# 映射 PyPI 包名到导入时使用的模块名（如果不同）
package_to_module_map = {
    "python-barcode": "barcode",
    "Pillow": "PIL",
    "imageio[ffmpeg]": "imageio",
    "websocket-client": "websocket", # 添加 websocket-client 到 websocket 的映射
    # 添加其他需要的映射
}

# 获取当前脚本目录
current_dir = os.path.dirname(os.path.realpath(__file__))
# 推断 ComfyUI 根目录 (假设 custom_nodes 在根目录下)
comfyui_root = os.path.abspath(os.path.join(current_dir, '..', '..'))

# --- 跨平台确定 Python 可执行文件 ---
python_exe_to_use = sys.executable # 默认使用当前 Python 解释器
print(f"Default Python executable: {python_exe_to_use}")

# 检查 Windows 嵌入式 Python
if platform.system() == "Windows":
    embed_python_exe_win = os.path.join(comfyui_root, 'python_embeded', 'python.exe')
    if os.path.exists(embed_python_exe_win):
        print(f"Found ComfyUI Windows embedded Python: {embed_python_exe_win}")
        python_exe_to_use = embed_python_exe_win
    else:
         print(f"Warning: ComfyUI Windows embedded python not found at '{embed_python_exe_win}'. Using system python '{sys.executable}'.")

# 检查 Linux/macOS venv Python
elif platform.system() in ["Linux", "Darwin"]: # Darwin is macOS
    venv_python_exe = os.path.join(comfyui_root, 'venv', 'bin', 'python')
    venv_python3_exe = os.path.join(comfyui_root, 'venv', 'bin', 'python3') # 有些系统可能叫 python3

    if os.path.exists(venv_python_exe):
        print(f"Found ComfyUI venv Python: {venv_python_exe}")
        python_exe_to_use = venv_python_exe
    elif os.path.exists(venv_python3_exe):
         print(f"Found ComfyUI venv Python3: {venv_python3_exe}")
         python_exe_to_use = venv_python3_exe
    else:
         print(f"Warning: ComfyUI venv python not found at '{venv_python_exe}' or '{venv_python3_exe}'. Using system python '{sys.executable}'.")
else:
    # 其他操作系统或未检测到特定环境时的回退
    print(f"Warning: Could not detect specific ComfyUI Python environment for OS '{platform.system()}'. Using system python '{sys.executable}'.")

print(f"Using Python executable for pip: {python_exe_to_use}")
# --- 结束 Python 可执行文件确定 ---


def check_and_install_dependencies(requirements_file):
    print("--- Checking custom node dependencies ---")
    installed_packages = False
    try:
        with open(requirements_file, 'r') as file:
            for line in file:
                package_line = line.strip()
                if package_line and not package_line.startswith('#') and not package_line.startswith('--'):
                    # --- 从行中提取纯包名和安装名 ---
                    package_name_for_install = package_line # 用于 pip install 的完整行
                    package_name_for_import = package_line # 用于 import 的纯包名，先假设一致
                    # 查找版本说明符的位置来分离纯包名
                    for spec in ['==', '>=', '<=', '>', '<', '~=', '!=']:
                        if spec in package_name_for_import:
                            package_name_for_import = package_name_for_import.split(spec)[0].strip()
                            break # 找到第一个就停止
                    # --- 结束提取 ---

                    # 使用提取出的纯包名查找模块名映射 (例如 Pillow -> PIL)
                    module_name = package_to_module_map.get(package_name_for_import, package_name_for_import)
                    try:
                        # 尝试导入纯模块名
                        importlib.import_module(module_name)
                        # print(f"Dependency '{package_name_for_install}' (module: {module_name}) already installed.")
                    except ImportError:
                        print(f"Dependency '{package_name_for_install}' (module: {module_name}) not found. Installing...")
                        try:
                            # 使用包含版本约束的原始行进行安装
                            subprocess.check_call([python_exe_to_use, "-m", "pip", "install", "--disable-pip-version-check", "--no-cache-dir", package_name_for_install])
                            print(f"Successfully installed '{package_name_for_install}'.")
                            importlib.invalidate_caches() # 清除导入缓存很重要
                            importlib.import_module(module_name) # 使用纯模块名再次尝试导入
                            installed_packages = True
                        except subprocess.CalledProcessError as e_main:
                            print(f"## [WARN] ComfyUI_to_webui: Failed to install dependency '{package_name_for_install}' with standard method. Command failed: {e_main}. Attempting with --user.")
                            try:
                                # 尝试使用 --user 参数进行备用安装
                                subprocess.check_call([python_exe_to_use, "-m", "pip", "install", "--user", "--disable-pip-version-check", "--no-cache-dir", package_name_for_install])
                                print(f"Successfully installed '{package_name_for_install}' using --user.")
                                importlib.invalidate_caches()
                                importlib.import_module(module_name)
                                installed_packages = True
                            except subprocess.CalledProcessError as e_user:
                                print(f"## [ERROR] ComfyUI_to_webui: Failed to install dependency '{package_name_for_install}' even with --user. Command failed: {e_user}.")
                                print("Please try installing dependencies manually:")
                                print(f"1. Open a terminal or command prompt.")
                                print(f"2. (Optional) Navigate to ComfyUI root: cd \"{comfyui_root}\"")
                                print(f"3. Run: \"{python_exe_to_use}\" -m pip install {package_name_for_install}")
                                print(f"   Alternatively, try installing all requirements: \"{python_exe_to_use}\" -m pip install -r \"{requirements_file}\"")
                                print("   If issues persist, you can seek help at relevant ComfyUI support channels or the node's repository.")
                            except ImportError:
                                print(f"## [ERROR] ComfyUI_to_webui: Could not import module '{module_name}' for package '{package_name_for_install}' even after attempting --user install. Check if the package name correctly provides the module.")
                        except ImportError:
                             # 调整错误信息，使其更清晰
                             print(f"## [ERROR] ComfyUI_to_webui: Could not import module '{module_name}' after attempting to install package '{package_name_for_install}'. Check if the package name '{package_name_for_install}' correctly provides the module '{module_name}'.")
                        except Exception as e:
                            print(f"## [ERROR] ComfyUI_to_webui: An unexpected error occurred during installation of '{package_name_for_install}': {e}")
    except FileNotFoundError:
         print(f"Warning: requirements.txt not found at '{requirements_file}', skipping dependency check.")
    except Exception as e:
        print(f"ERROR: An unexpected error occurred while processing requirements: {e}")


    if installed_packages:
        print("--- ComfyUI_to_webui: Dependency installation attempt complete. You may need to restart ComfyUI if new packages were installed. ---")
    else:
        print("--- All dependencies seem to be installed. ---")


# 自动检测并安装依赖 (移到文件顶部执行)
requirements_path = os.path.join(current_dir, "requirements.txt")
check_and_install_dependencies(requirements_path)

# --- 结束自动依赖安装 ---


from .node.hua_word_image import Huaword, HuaFloatNode, HuaIntNode # 移除了 HuaFloatNode2/3/4, HuaIntNode2/3/4
from .node.hua_word_models import Modelhua
# Removed GradioInputImage, GradioTextOk, GradioTextBad from gradio_workflow import
from .node.mind_map import Go_to_image
from .node.hua_nodes import GradioInputImage, GradioTextBad
from .gradio_workflow import GradioTextOk # GradioTextOk 现在从 gradio_workflow.py 导入 (如果它是一个节点类)
# Added GradioInputImage, GradioTextOk, GradioTextBad to hua_nodes import
from .node.hua_nodes import Hua_gradio_Seed, Hua_gradio_jsonsave, Hua_gradio_resolution
# 移除了 Hua_LoraLoaderModelOnly2/3/4 和 GradioTextOk2/3/4
from .node.hua_nodes import Hua_LoraLoader, Hua_LoraLoaderModelOnly, Hua_CheckpointLoaderSimple,Hua_UNETLoader
# from .hua_nodes import GradioTextOk2, GradioTextOk3,GradioTextOk4 # 这一行被移除
from .node.hua_nodes import BarcodeGeneratorNode, Barcode_seed
from .node.output_image_to_gradio import Hua_Output
from .node.output_video_to_gradio import Hua_Video_Output # 添加视频节点导入
from .node.deepseek_api import DeepseekNode

NODE_CLASS_MAPPINGS = {
    "Huaword": Huaword,#不加入组件
    "Modelhua": Modelhua,#不加入组件
    "GradioInputImage": GradioInputImage,
    "Hua_Output": Hua_Output,
    "Go_to_image": Go_to_image,#不加入组件
    "GradioTextOk": GradioTextOk, 
    "GradioTextBad": GradioTextBad,
    "Hua_gradio_Seed": Hua_gradio_Seed,
    "Hua_gradio_resolution": Hua_gradio_resolution,
    "Hua_LoraLoader": Hua_LoraLoader,#不加入组件
    "Hua_LoraLoaderModelOnly": Hua_LoraLoaderModelOnly, 
    "Hua_CheckpointLoaderSimple": Hua_CheckpointLoaderSimple,
    "Hua_UNETLoader": Hua_UNETLoader,
    "BarcodeGeneratorNode": BarcodeGeneratorNode,#不加入组件
    "Barcode_seed": Barcode_seed,#不加入组件
    "Hua_gradio_jsonsave": Hua_gradio_jsonsave,
    "Hua_Video_Output": Hua_Video_Output,
    "HuaFloatNode": HuaFloatNode, 
    "HuaIntNode": HuaIntNode, 
    "DeepseekNode": DeepseekNode,

}

NODE_DISPLAY_NAME_MAPPINGS = {
    "Huaword": "🌵Boolean Image",
    "Modelhua": "🌴Boolean Model",


    "GradioInputImage": "☀️Gradio Frontend Input Image",
    "Hua_Output": "🌙Image Output to Gradio Frontend",
    "Go_to_image": "⭐Mind Map",
    "GradioTextOk": "💧Gradio Positive Prompt",
    "GradioTextBad": "🔥Gradio Negative Prompt",
    "Hua_gradio_Seed": "🧙hua_gradio Random Seed",
    "Hua_gradio_resolution": "📜hua_gradio Resolution",
    "Hua_LoraLoader": "🌊hua_gradio_Lora Loader",
    "Hua_LoraLoaderModelOnly": "🌊hua_gradio_Lora Model Only",
    "Hua_CheckpointLoaderSimple": "🌊hua_gradio Checkpoint Loader",
    "Hua_UNETLoader": "🌊hua_gradio_UNET Loader",
    "BarcodeGeneratorNode": "hua_Barcode Generator",
    "Barcode_seed": "hua_Barcode Seed",
    "Hua_gradio_jsonsave": "📁hua_gradio_json Save",
    "Hua_Video_Output": "🎬Video Output (Gradio)",
    "HuaFloatNode": "🔢Float Input (Hua)",
    "HuaIntNode": "🔢Integer Input (Hua)",
    "DeepseekNode": "✨ Deepseek chat (Hua)",

}

jie = """
➕➖✖️➗  ✨✨✨✨✨        ✨  ⭐️✨✨✨✨✨✨    ➖✖️➗ ✨✨           ✨✨   ✨   ➖✖️➗  ✨
⣿⣟⣿⣻⣟⣿⣟⣿⣟⣿⣟⣿⣟⣿⣟⣿⢿⣻⣿⢿⡿⣿⢿⡿⣿⢿⡿⣿⢿⡿⣿⡿⣿⡿⣿⣿⢿⣿⡿⣿⣿⣿⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⣟⣗⣽⣪⠕⡜⡜⢆⠏⡖⡡⡑⢆⢑⠜⢌⠂⡇⢎⠂⡂⠂
⣿⣯⣿⣽⣯⣷⡿⣷⢿⡷⣟⣯⣿⢷⣿⣻⡿⣿⣽⡿⣟⣿⣟⣿⢿⣻⣿⢿⡿⣿⣻⣿⣻⣿⢿⣾⣿⣟⣿⡿⣷⣿⡿⣿⣾⡿⣾⣷⣿⣷⣿⢿⣾⣿⣽⣿⣽⣿⣽⣿⣻⣿⣻⣿⣻⣿⡿⣿⡿⣟⣿⣾⣿⡽⣿⢿⢱⡯⣷⠯⣚⠜⢌⢊⠄⢕⢸⢸⠨⢊⠢⠢⡑⢗⡇⡃⡂⡂⢂⠡
⣿⣾⣳⣿⢾⣯⡿⣟⣿⣻⣿⣻⣽⡿⣯⣿⣻⣟⣷⣿⣿⣻⣽⡿⣿⣟⣿⣟⣿⣿⣻⣽⣿⣽⣿⣟⣷⣿⣟⣿⡿⣷⣿⣿⣷⣿⣿⢿⣾⣿⣾⣿⣿⣯⣿⣿⣽⣿⣟⣿⣿⢿⣿⣿⣿⢿⣿⣿⣿⣿⡿⣿⢮⢻⢚⢎⢮⢫⡏⡇⡗⢅⠕⡐⢍⠌⡐⡐⠌⠢⡘⡐⢬⠣⢊⢐⠐⠄⡁⠂
⣿⣷⣻⣾⢿⣯⣿⣟⣿⣽⣯⣿⣯⣿⢿⣽⣟⣯⣿⡷⣿⣻⣯⣿⣿⣽⣿⣽⣿⣽⣿⣻⣽⡿⣷⣿⢿⣯⣿⣟⣿⣿⣻⣾⡿⣷⣿⣿⡿⣯⣿⣿⣾⡿⣿⣾⣿⣟⣿⣟⣿⣿⣿⣷⣿⣿⣿⣿⣯⣷⣿⡿⡪⣣⡱⡱⡱⡘⡌⢎⠜⡐⡡⠨⡐⡐⡐⠠⠡⢡⢘⠜⠄⢕⢐⠡⠡⡁⡊⠄
⡿⣞⣷⣻⣻⡿⣾⣯⡿⣷⢿⡷⣿⣾⡿⣿⣽⡿⣷⣿⢿⣻⣯⣷⣿⡷⣿⣷⢿⣻⣾⣿⢿⡿⣟⣿⣿⣻⣽⣿⣻⣽⣿⣟⣿⣿⣯⣷⣿⣿⣿⣷⣿⣿⡿⣿⣽⣿⡿⣿⣿⣿⣽⣿⣟⣿⣯⣷⣿⣿⢿⣳⣽⣿⣾⣿⣱⠥⡣⣕⢌⢢⠃⠅⡂⢂⠢⠡⡃⢂⠅⡊⠌⡂⡂⠌⡐⠄⠌⡐
⣿⢿⣺⣷⣻⡿⣿⢾⡿⣟⣿⢿⣻⣾⣿⣻⣷⣿⢿⣽⣿⢿⣻⣯⣷⣿⣿⣻⣿⢿⣿⣽⣿⣿⣿⢿⣯⣿⣿⣻⣿⣟⣯⣿⣿⣷⣿⡿⣟⣯⣷⣿⣷⣻⡻⣟⢿⣟⣿⣿⣿⣾⣿⣿⣿⣿⣿⣿⡿⣿⣿⣿⣿⣿⢿⡺⡺⡹⡸⡸⠢⡱⠡⡑⠄⠅⡊⠨⠠⡁⡂⡂⠅⢂⠄⠅⢂⠂⡁⠄
⣿⡿⡽⣾⣽⢿⣻⡿⣿⣟⣿⡿⣟⣿⡾⣿⢷⣿⢿⣻⣽⣿⣿⣻⣯⣿⣾⣿⣻⣿⣯⣿⣷⣿⣻⣿⣟⣯⣿⣟⣯⣿⣿⢿⣷⣿⣷⣿⣿⣿⣿⣿⣻⣷⣿⣾⣷⣯⣯⣿⣽⣻⢿⣷⣿⣿⣽⣿⣿⢿⣯⣿⣿⣿⢳⢹⢸⢌⠆⢕⡑⡱⣁⠢⠡⢁⠂⠅⡁⢂⢂⢔⠨⢐⢑⠨⢀⠂⡐⢀
⣿⣻⣯⣿⣾⢿⣟⣿⣯⣿⢷⣿⣿⣻⣿⣻⣿⣻⣿⢿⣿⣽⣾⡿⣿⣽⣷⣿⢿⣽⣾⣿⣾⣿⣻⣯⣿⣿⣟⣿⣿⢿⣻⣿⣿⣽⣾⣿⣟⣯⣷⣿⣿⡿⣟⣿⣽⣿⣿⣿⢿⣿⣿⣾⣷⣟⣟⡿⣿⣿⣿⣿⡿⣯⡣⡳⣹⠰⡱⡱⡪⡪⣪⠎⢌⢐⠈⠄⡂⠢⠡⠐⡈⠄⢂⢐⢀⠂⡐⡀
⣿⣻⣞⣷⢿⣻⣟⣯⣷⣿⢿⣻⣾⣿⣽⣿⣽⣿⣽⣿⣯⣿⣽⣿⢿⣯⣿⣾⣿⡿⣿⣽⣾⣿⣻⣿⣯⣷⣿⣿⢿⣿⣿⣿⣽⣿⣟⣿⡿⣿⣿⣿⣻⣿⣿⣿⣿⣿⣿⣾⣿⣿⣿⡿⣿⣿⢿⣻⣗⣯⣻⣽⡽⡧⡣⣫⢾⡸⡌⡪⠪⠪⡘⡜⡐⠠⠨⠐⢄⠅⠅⢌⠐⡈⡐⠠⠐⡀⠂⡀
⣿⣳⣻⣿⢿⣻⣟⣿⣯⣿⢿⣟⣿⡾⣿⡾⣿⡾⣟⣷⣿⣯⣿⣟⣿⣿⣽⡿⣷⣿⣿⣟⣯⣿⣯⣷⣿⣟⣯⣿⡿⣿⢾⡿⣽⣷⣿⢿⣿⢿⣷⣿⣿⢿⣿⣻⣿⣽⣾⣿⣿⣿⣿⣿⣿⣿⣿⡿⣾⡷⣵⣯⢿⣝⢜⣞⢗⡽⡐⡌⡪⠪⡘⡜⣬⣨⠊⠜⢠⠡⢁⢂⢐⢀⢂⠁⡂⠄⡁⡂
⣿⣽⣞⣟⣿⣻⣿⣽⣾⣿⣻⣿⣯⣿⡿⣿⣻⣿⡿⣿⣷⡿⣷⣿⢿⣷⣿⡿⣿⣻⣾⣿⢿⣻⣽⡿⣝⢏⡏⡗⡝⡕⡏⠯⡛⡞⣽⣻⢽⡻⣟⢷⡻⡯⣿⢿⣿⣿⣿⣿⣿⣿⣾⣿⣿⣿⣷⢿⣽⣽⢼⣾⣗⣯⣗⢷⢝⡾⣕⣎⢌⢎⣮⣫⣞⡮⡊⠜⠨⠨⢀⠂⡐⠠⠐⠀⠂⠄⠂⡀
⣿⢷⡯⡾⣷⡿⣷⡿⣷⣿⣻⣷⡿⣷⣿⡿⣟⣯⣿⣿⢷⣿⣿⢿⡿⣿⣾⣿⣿⡿⣟⣿⣿⢿⡫⡞⠕⠣⡑⠅⠕⡨⡈⠪⡨⠨⡂⠪⡑⡩⡘⡌⡪⠪⠪⠫⡳⡻⣽⢿⣷⣿⣿⣿⣿⣯⣿⡿⣽⣟⢮⣟⣯⣿⣞⡵⣻⢮⢗⣗⣗⣽⣺⢺⢪⠸⠨⠨⠨⠈⠄⢂⠄⠡⠈⡈⠄⠡⠐⠀
⡟⣟⣿⡿⣟⣿⣟⣿⢿⣽⡿⣷⣿⣿⣯⣿⣿⡿⣿⣽⣿⣿⣻⣿⣿⢿⣷⣿⣷⣿⣿⢿⢝⠕⡡⠂⠅⠕⡐⠡⡁⠢⠨⠨⡐⠡⠊⠌⢌⠢⡑⠄⠕⡑⣑⢑⠔⢌⠌⡝⢽⣻⢿⣿⣻⣿⣿⣯⣷⣿⢵⣿⣿⣾⣷⣯⣳⢯⣻⣪⣞⢮⠪⠪⡘⠬⠨⡈⠌⡈⢄⠧⠊⠠⢁⠀⠄⠡⢈⠆
⣿⡼⣜⡝⣟⢿⣯⣿⡿⣟⣿⡿⣷⣿⣽⣿⣾⣿⣿⣻⣽⣾⣿⣿⣾⣿⣿⣷⡿⣟⠞⢍⠢⠨⡐⠡⠡⢑⠠⢁⠂⡡⢈⠢⠨⠨⢈⠊⠄⡑⠨⢐⠁⡊⠔⡐⠌⢄⢑⠌⠔⡨⢙⢟⣿⣿⣿⣯⣿⣿⡳⣿⣯⣿⣿⡷⣯⡳⣣⣗⢵⡱⣑⡑⢕⠎⠌⢄⠑⡨⢮⢇⠡⢈⠠⣐⢌⠐⠄⢂
⣿⣯⣷⡿⣜⣕⢗⡻⡿⣿⢿⣿⢿⣽⣿⣾⣿⣾⢿⣻⣿⡿⣿⣾⣿⣽⣾⢿⠝⢅⠕⡡⠊⠌⠄⢅⢑⠐⡈⡀⢂⠐⠠⠂⠡⠨⠀⠌⡐⠠⠑⡐⠐⡈⡐⠨⠨⢐⠠⢈⢂⠂⠅⡂⠝⣽⣿⣿⣽⣿⡽⣽⣿⣿⣿⣿⣣⡯⣳⡳⣻⡺⡑⢅⠭⠅⡕⡁⡂⠪⡱⠱⢈⢒⢑⢃⠅⡅⡁⢄
⣿⢷⣿⣟⣿⢾⣵⣽⣺⡪⣟⢿⢿⣟⣷⣿⣾⣿⣿⣿⢿⣿⢿⣻⣾⣿⠫⡣⠡⡑⢌⢐⠅⠅⠕⡐⡐⢐⢀⠂⡂⠌⢂⢁⠁⡂⠁⡂⠐⡈⢀⠂⠁⠄⠂⠡⠨⢀⠂⡐⠀⠌⢐⠠⠁⡊⢿⢿⣽⣿⡽⣺⣿⣿⣾⣿⣗⡯⡷⡯⡳⢏⠪⠨⢐⠕⡐⠔⡘⣌⣆⢧⠳⢵⢦⢗⠱⠐⠠⡺
⣿⣿⣾⣟⣾⡯⣿⣻⣿⣽⣺⣪⣫⡫⣟⣯⣿⣾⣿⣾⣿⣿⣿⣿⢿⠱⡑⢌⠪⡐⡡⢂⢊⠌⠌⠄⡂⡂⡂⠌⡐⠈⠄⡀⠂⠄⠁⠄⠂⡀⠂⡈⠐⠈⡀⠁⠌⡀⢂⠀⠂⡈⠠⢀⠡⠐⢈⢻⣿⣿⡯⣯⣿⣿⣿⣿⢾⢽⢝⢮⣪⡢⡣⡵⢐⠅⡢⡑⡹⡚⡘⡝⢌⢎⢏⠰⡁⢌⢐⣞
⣿⣿⣿⣯⣷⣿⣿⡿⣿⡯⣯⣷⣿⣿⣪⢞⢮⡻⣾⣯⣿⣷⣿⢿⢹⢘⢌⠢⡑⢔⠨⢂⠢⠡⠡⡁⠢⢐⠠⢁⠂⠅⠡⠀⠅⠨⠐⡀⠁⠄⠂⠠⠈⠠⠀⠌⠀⠄⠂⡀⢁⠀⠄⠠⠐⠈⡀⠂⣟⣿⣯⡺⣿⣿⣷⣿⡿⡭⣯⣳⣳⡳⣜⣞⢔⢁⡢⠂⣆⢣⢇⢣⡲⣕⢴⡡⠄⡅⡢⣪
⣿⣟⣿⣯⣿⣿⣿⣿⣿⣟⣷⡿⣟⣿⣽⣟⣗⣿⣞⢮⡻⡾⣟⢏⢢⠱⡐⢅⠪⡐⢅⢑⠌⢌⢂⢊⠌⠢⡈⡂⠅⢅⠡⠁⠅⠌⠄⢂⠡⠈⠄⠁⠄⠁⡐⢀⠁⠄⠂⠠⠀⠄⠂⠀⠂⠁⡀⠂⠪⣿⣷⢫⣿⣿⣿⣟⡯⣻⢕⡗⡷⡽⣵⡫⣏⣗⡽⣱⡳⡽⣝⡷⡭⠢⢕⠪⡰⡜⡎⡖
⣿⡿⣽⣿⢾⣝⡿⡾⣿⣽⣿⣿⣿⡿⣏⣟⢮⣟⣿⣟⡾⣝⣎⠪⠢⡑⢌⠢⡑⢌⠢⠡⡊⢔⢐⠡⡨⢨⢐⢐⠡⢂⠌⠌⠄⠅⠌⢔⠠⠁⠂⡁⠄⠁⠄⢂⠂⠐⠀⠂⠐⠀⠄⠁⡀⠁⡀⢈⠈⣿⣻⡪⡾⣟⢟⡟⡽⡪⡗⠧⣙⢽⣪⢞⡷⡳⣝⡵⡿⡽⣳⣻⣺⢸⡢⠑⡌⡎⣎⢇
⣿⣟⣿⡿⣯⢿⡽⣿⣿⡯⣟⣟⣿⣻⡳⡽⡵⣻⡯⣿⣻⡽⡢⡃⢇⠕⡡⠱⡨⠢⡑⡑⢌⢂⠢⡑⠌⡂⡢⢂⠕⡐⠌⠌⠌⠌⢌⢂⢊⠈⠄⠠⢀⠁⡈⠄⢈⠀⡁⠈⠄⠁⠠⠀⠠⠀⠠⠀⠐⡽⣟⢜⠸⡈⠪⠨⢂⠕⡘⠌⢌⢳⡳⡝⣎⢓⠜⡮⣯⢯⣗⡯⣞⢎⠗⢕⢧⣣⢇⢗
⣿⣟⣾⣿⣻⡯⣿⣽⣾⣿⣻⣞⣾⢗⡯⡯⣺⢝⡯⣗⡯⣗⢕⠸⡐⡱⢨⢑⢌⢊⢢⠡⡑⡐⢅⢊⠌⡢⠨⡂⢌⠢⠡⡑⡡⠡⡑⡐⡀⢂⠁⡐⠀⠄⠠⠀⠄⠠⠀⡈⠄⢈⠠⠀⠄⠐⠀⠈⡀⡪⡹⠠⡑⠌⢜⢈⠢⡂⡪⣘⢰⡱⣹⡪⡢⡢⢇⡯⣯⡻⡮⡯⡯⣣⢢⢕⣟⢾⡕⢵
⣟⣞⢿⢽⢷⣟⣿⣺⣿⣯⣷⡻⣞⡯⣞⡽⡵⣫⢟⡮⣻⣜⢔⢕⠱⡘⢔⠱⡨⠪⡐⢕⢌⢊⢢⢑⢌⢢⢑⢌⠢⡡⡃⢆⢊⠌⢔⠐⡀⢂⠐⡀⠅⠌⡂⠅⠂⡂⠁⠄⠨⠀⠄⠠⠀⠠⠀⢁⠀⢎⠪⠨⡪⢪⢪⠢⠣⡪⢪⢪⢣⢣⡳⣕⡵⣕⣷⣫⢷⢯⢯⢯⡫⣗⢽⡸⣺⢽⡎⡇
⣣⢳⡹⡪⣗⡯⣟⣯⣿⡷⣗⡯⣳⢯⡳⡽⣝⢞⡽⣺⢵⣳⡱⡰⡑⡕⡱⡱⡱⡱⡱⡱⡘⡜⢜⢌⢎⢢⠱⡰⡑⢬⢨⢢⢑⢅⢅⢂⠂⡂⠡⠠⠡⠑⠠⠁⡂⠂⠅⠌⠌⠨⠨⠐⠀⠠⠀⢰⢽⡵⡽⡸⡨⡊⡎⡪⡨⡪⡪⠪⡪⡪⣚⢮⡺⡕⣗⢹⢝⡽⣣⡣⡝⡼⡕⡽⡸⡯⡷⡽
⡕⣇⢧⣫⣚⢮⢳⢕⡯⣟⣗⢯⡳⣝⢮⡫⡮⣳⢽⢵⡳⡳⣕⢕⠕⡌⡆⡧⡳⡱⡱⡑⢕⢑⢅⢕⢐⠅⡕⢌⢪⠢⡣⡱⡱⡱⡐⠔⡐⠨⢈⢀⢂⠈⡀⠄⠐⠈⡀⠂⡁⠡⠡⠡⢁⠠⠀⢵⣫⡯⡯⡪⢪⠺⡸⡸⡘⢔⢅⠇⡕⡵⣝⡵⡝⡎⡕⡭⡕⡭⡪⡪⡊⡎⡎⡢⠪⡯⡯⣟
⢜⢜⡜⣜⢔⡳⣕⢯⢯⣳⢽⣪⢯⡳⣽⡪⡯⣎⣯⡳⡽⣹⢪⢧⡣⡱⡸⡪⣪⢪⢪⢸⢸⢨⢢⠢⡣⡱⡸⢸⢰⢱⣱⢵⣳⡳⡵⣕⢬⢨⢐⢐⠄⢅⢂⠂⠅⢅⠢⡑⡨⡘⢌⠪⡀⠠⡈⡺⡸⡪⡻⡜⡢⡣⣣⡪⡪⡪⡳⡱⢙⢮⣣⢫⢞⢼⢸⢕⢽⡪⡳⡘⡔⢅⢇⠪⠨⣻⢽⢽
⢹⡱⡹⡪⡳⡹⣸⡹⣕⡯⣯⢞⡮⣫⣞⢞⣺⢺⡺⣪⢳⢕⢽⢕⣗⢕⢵⢹⢜⢎⢇⢗⢕⢕⢕⢕⢕⢜⢜⢼⢼⢵⢯⣳⣳⣝⣞⢮⡳⣝⢮⡲⣕⢅⡆⡕⡩⠢⡑⡌⢆⠪⢢⢑⢌⡜⡄⠂⠌⢎⢎⢎⢇⢇⠇⡣⡱⠨⡂⢇⠹⡪⡮⣝⡕⡏⡮⡪⡧⣗⢕⢅⢇⢕⠜⠕⠡⡹⣹⢽
⢵⢳⡳⡣⡇⡇⣇⡞⣮⣺⣳⣻⣺⡳⡵⣫⢞⡵⣝⡎⣗⢽⡹⡕⣕⢇⡗⣝⢎⢧⢫⢎⢇⢗⢕⢵⢵⢽⢽⢽⢽⢽⢽⣺⣺⡪⡮⡳⣝⢮⢧⡳⣕⢗⢵⢝⢎⢗⢵⢕⢵⡹⡜⡎⡕⡎⠄⢌⢐⠱⡨⢺⠘⢌⢊⠢⡃⠕⢌⠄⢸⢘⢎⢮⡺⢜⢆⢝⣽⡳⡝⡜⡜⡔⣑⢁⢐⢐⢈⢌
⡜⡵⣫⢇⢧⡳⡵⣝⡮⣞⢮⡲⣳⢽⢝⣮⣻⡪⣞⢞⡜⡮⡎⡧⡣⣳⢽⢜⢮⡣⣗⡵⣝⡮⡯⣫⢗⡯⡯⡯⡯⣯⣳⣳⡳⣝⢮⢫⢎⢗⢵⡹⣜⢝⢎⢗⡝⣕⢕⡕⣇⢧⢣⢣⢣⠃⠅⡂⡂⠕⡈⡢⢑⢡⠨⡊⡎⢕⢐⠅⢢⠡⡑⡘⣎⢇⠣⡑⢗⠏⢇⠣⠣⡑⢌⢢⢑⢌⢆⢎
⡪⡪⡪⢭⡳⢽⢕⢧⢯⢞⡵⡫⣗⣯⡻⣮⣺⣺⢾⣷⢝⢜⢇⣗⢝⢼⣝⢽⢕⡯⣳⢝⣞⢮⣻⡺⣽⡺⡽⡽⣝⢮⢞⡮⣞⢮⢎⡗⡭⡳⡱⣕⢕⢧⢫⢎⢞⡜⣕⢝⡜⡜⡜⡜⡜⡌⠔⡰⠨⢊⠔⠨⡢⢡⠣⡢⠱⡐⢔⢐⠰⡑⢕⠔⢕⡥⢑⠠⡱⡠⠁⠌⠨⡘⡜⣜⢔⢽⢸⡱
⡇⡇⡏⡎⡎⡇⡏⡮⡪⣟⢮⡻⣵⣳⣻⣵⣳⣯⣿⣾⢣⣏⢧⢳⡹⡪⢮⣫⡳⡽⡵⣻⡪⡯⣞⢮⣳⢽⢽⢝⡮⡯⣳⢝⡮⡳⡵⡹⡪⡳⡹⣸⢱⢝⢎⢧⢳⢹⡸⡜⡜⡎⢎⢪⢪⠪⡈⡢⡹⣆⢪⢸⢮⣒⡗⢌⠧⡪⡪⡢⠈⡎⢌⠌⢎⡮⡘⡔⣑⢐⢁⠂⡡⣚⢮⢺⢜⢕⡗⣕
⢜⣜⢮⡪⡺⡜⡼⣸⡹⣜⡳⣝⣞⢼⣺⣪⣟⡽⡻⡽⡺⣺⣗⢗⣝⢮⢣⣗⢽⣪⣻⡪⡯⣫⣞⢗⣗⢯⣳⡫⣞⡽⣪⢟⣞⡝⣎⢧⢳⡱⡝⣆⢧⢣⢣⡣⣫⢪⡪⡪⡪⡪⢪⢊⢆⡃⡪⣸⢮⣣⢳⢝⣽⣒⢯⢘⢌⠪⢜⢐⠠⣩⣢⢳⠳⡕⡍⡊⡒⡑⡑⡑⠌⠜⡘⠱⠱⢕⢽⢸
⣗⢵⡳⣹⢸⢜⢎⢷⢝⡮⣺⢵⡳⡝⣞⡼⡺⡪⡳⡝⣜⢯⣯⡳⡕⣗⢵⡳⣝⢮⡺⣺⢽⢵⣳⡫⣞⣵⡳⣝⣗⡽⡽⣕⣗⢽⡪⡎⡮⡺⡸⡪⣪⢪⢣⡣⡳⡱⡕⡕⢕⠜⡌⢆⢕⣵⡯⣯⣳⡼⣔⢖⡳⡵⡹⡆⢆⠪⡸⡲⡸⢘⠨⢐⠨⣪⡂⠢⡑⡡⠐⠨⡨⡈⣐⠡⠡⠈⠠⠁
⢗⡳⣝⢞⣵⢝⡗⣯⣳⣫⢗⣗⢽⣝⣞⢮⢫⡪⣫⢧⣳⡻⡮⡯⣺⢺⢮⡺⣕⢯⡺⡵⣫⢗⣗⢽⡳⣵⡫⣗⣗⢽⣺⢵⢽⢕⡇⣏⢎⡇⡏⡎⡎⣎⢮⢺⢸⢱⠱⡘⡌⡪⠨⢪⣻⣻⢛⢟⠫⡫⣓⢕⢽⢜⣺⣝⠆⡃⡃⠕⡐⠡⡈⠂⠅⡣⡣⢑⠨⢬⢆⠁⠀⠂⡀⡈⠄⢌⡰⡠
⣟⣞⠽⡽⣾⣻⣺⢮⡪⣺⡽⣪⡳⣕⢿⣳⢽⣮⣟⡧⣗⣟⢎⢎⠮⡎⢇⡳⣕⢗⣝⢮⣳⡫⣞⢽⡺⣕⢯⣳⢳⣫⣞⢽⢵⣫⢞⢜⡜⣜⢜⢎⢎⢎⢎⢮⢪⠪⡪⡘⢔⠡⡑⠁⠀⠀⠠⠀⠂⠠⠀⠐⠈⠀⠀⠠⠁⠠⠀⠌⠠⠑⢀⠡⠐⢸⢱⢠⢏⠃⡀⠐⠀⠁⢂⠺⣹⢳⠹⠩
⡷⣣⢯⣞⡼⡷⣿⣽⡎⣗⢯⡳⡝⣎⣟⣽⢽⣺⡷⣽⡺⣮⢣⡫⣣⡣⣧⡳⡵⣝⢼⢕⢗⡽⣪⢗⢯⡺⣝⢮⢯⡺⣪⢯⢳⡳⣝⢜⢜⢜⢜⢜⢜⢜⢕⢕⢕⠱⡐⢅⠕⠨⠐⠀⠐⠀⠀⡀⠀⡀⠠⠀⠀⠄⠂⠀⠀⠀⠀⠀⠄⠁⠔⡨⡐⠸⡸⡀⢌⠪⡠⡐⡄⡪⠰⢑⠈⠤⢒⠠
⡯⡯⣳⡳⡽⣽⡿⣯⣷⡯⣗⢵⢹⢢⢳⡵⣯⢷⣻⢮⣻⣞⡯⣿⣳⡿⣷⣻⣟⣮⣳⢹⢵⢝⢮⣫⡳⣝⢮⢯⡺⣪⢗⢽⢕⡯⣺⢸⢱⢱⢱⢣⢳⢱⢱⠱⡨⢊⢌⢂⢊⠌⠐⠀⠄⠀⢀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠀⠀⠠⠁⠌⡐⠌⡈⢎⢂⠕⢐⢈⠠⡐⡠⠡⡠⢡⠡⡠⡀
⢳⢱⢕⢯⡫⡞⣿⣻⢾⣽⡣⣗⢵⢱⠹⡯⣟⣿⢽⣻⣽⣞⣯⣿⣺⡯⣷⣟⡾⣗⡷⡝⣎⢏⡧⣳⢝⢮⡳⡳⣝⢮⡫⣗⢯⣞⡕⡇⡇⡇⡣⡱⠡⡣⢡⠱⠨⡂⠢⢂⠂⡐⠀⠐⠀⠈⠀⠀⠀⠂⠀⠂⠁⠀⠀⠈⠀⠀⠄⠂⠐⠀⢑⢆⢇⡇⣇⢇⢇⢇⢇⢇⡳⡸⡱⡱⡱⣱⢱⢱
⣣⣳⣱⡳⣕⣯⡷⣟⣿⣞⡯⣪⢣⢳⢱⡻⡯⡿⡽⣻⡺⡽⣺⡳⡽⣝⢵⡳⡽⣕⢯⡺⡪⣳⢹⡪⣏⢗⡽⣹⡪⣗⡽⣪⣗⢵⢝⡜⡜⢌⢪⢨⠪⡨⡂⡣⠑⠌⠌⠄⠂⠠⠀⢀⠈⠀⠁⠈⢀⠐⠀⡀⠀⠄⠈⠀⠀⠠⠀⠀⠂⠈⠸⡸⡱⡕⣇⢯⢪⡣⡳⡕⡕⣇⢗⢝⢜⢼⢸⢱
⣷⣻⣞⣟⣯⡷⡿⡯⡷⣻⢺⢜⡪⣪⡪⣞⣝⢮⢯⣺⡪⣟⢮⡺⣝⢮⡳⣝⢞⢎⡗⠕⠈⡎⡧⡫⡮⡳⣝⢮⡫⣞⢮⡳⣕⢯⡣⡣⡪⡊⢆⢕⢑⠌⠢⠨⠨⢈⠐⠀⠌⠠⠀⢀⠀⠈⢀⠈⠀⠀⠄⠀⠂⠀⠂⠀⠌⠀⠀⠁⠀⠂⠁⢎⢎⢎⠮⡪⡣⡳⣱⢹⢜⢜⡜⡎⡇⡇⡗⡕
⣟⣗⢯⢯⡺⣝⣝⢮⢯⢮⡳⣣⢯⡺⣜⢮⢮⢳⡳⡵⣝⢮⢳⢝⣎⢧⣫⢺⡪⣳⡙⠠⠁⡪⢸⢸⢪⡳⡕⡧⡫⢮⢳⡹⡪⣇⢗⢕⢌⠪⡂⠕⡠⠡⠡⢁⠁⠄⠀⠅⡈⠀⠄⠀⡀⠁⠀⠀⠈⠀⠀⠄⠀⠐⠀⠁⠀⠀⠁⠀⠁⠀⠂⢹⢸⢬⢘⢜⢜⢜⢜⢼⢸⢱⢱⡱⢕⢕⡕⡕
⢷⢕⢯⡺⣪⢧⡳⣝⢮⡳⣝⢮⡺⣪⢮⡳⣝⣕⢗⢵⢳⡹⡪⡧⡳⣕⣕⢧⡫⣎⠊⠄⠂⢪⢘⢔⢑⢕⢕⢕⢝⢜⠜⡌⢎⠎⡎⡇⡊⠌⠄⠅⠂⡁⠁⡀⠄⠂⠁⠠⠀⠠⠀⠄⠀⠠⠀⠁⢀⠐⠀⠠⠈⠀⠐⠀⠈⠀⠈⠀⠈⠀⠠⠀⡇⡇⡏⣎⢎⢎⢎⢪⠪⡪⡣⡪⡣⡣⡣⣣
⡳⣝⢵⢝⢮⡳⣝⢮⡳⣝⢎⣗⢝⣎⢧⡳⣕⢮⡫⡳⡳⡹⣕⡝⡮⡪⡮⣪⡺⡘⡈⠐⢈⠠⢃⠪⡢⡑⢌⢪⠨⠢⡑⠌⡂⡑⠸⠐⠀⡁⢈⠀⢁⠀⠄⠀⡀⠀⠄⠠⠀⠠⠀⢀⠈⠀⠠⠈⠀⢀⠠⠀⠀⠄⠂⠀⠀⠁⠀⠐⠀⠐⠀⠀⢪⢪⢪⢪⢪⢪⢒⢥⢣⠱⡘⡘⠜⡜⡎⢖
⡺⣪⡫⡽⣱⢝⣎⢧⡫⡮⡳⡕⣗⢕⢧⡳⡕⡧⡫⡮⣫⡺⡜⣎⢗⡝⡮⡊⡆⠂⠄⠁⠄⠠⢁⠣⡂⢎⠢⡑⢌⢊⠄⢅⢂⠐⡀⠡⠐⠀⠄⠐⠀⢀⠠⠀⠀⡀⠄⠀⠠⠀⠠⠀⠀⠐⠀⡀⠂⠀⢀⠠⠀⠄⠀⠠⠐⠈⠀⠀⠄⠀⠠⠀⢱⢱⢱⢱⢱⠱⡱⡱⡱⡱⣘⢌⢌⢂⢊⠪
⢯⢺⡪⣫⢮⢳⢕⢗⡝⡮⡳⡝⣎⢏⢧⢳⡹⣪⡫⡺⣜⠮⣝⢼⡱⡙⡔⠕⠡⠐⢈⠠⠁⠐⠀⢂⠊⢄⠕⡈⡂⡢⢑⢐⠀⡂⠐⠠⠈⡀⠂⠀⠂⠀⠀⢀⠀⡀⠀⠐⠀⠀⠂⠀⠈⠀⡀⠀⡀⠐⠀⠀⡀⠄⠈⠀⠄⠠⠐⠀⠐⠈⢀⠐⠨⢪⢪⢪⢒⢍⢎⢪⢸⢘⠜⡔⡕⣌⠢⡡
⣗⢵⢝⢮⣪⢳⢝⢵⡹⣪⡳⡝⡮⡝⣎⢗⣝⡜⡮⡫⣎⢯⢺⢜⠜⠨⢐⢈⠐⢈⠠⠀⠌⢀⠁⠄⠨⠐⠨⢐⠨⡐⡐⢐⠀⠂⠁⠂⠁⠀⠀⠂⠀⠂⠁⠀⡀⠀⡀⠂⠈⠀⠀⠂⠈⠠⠀⠠⠀⠀⠠⠀⠀⠀⡀⠁⠀⡀⠀⡈⠀⢁⠠⠐⠀⠌⠸⡸⡸⢸⢘⢜⢌⢎⢪⢊⢎⢲⠱⡅
⡳⡵⡝⡮⣪⢳⡹⣕⢽⡸⡪⡮⡳⡹⣜⢵⡱⡵⡝⡞⡜⠎⠑⠐⡈⠨⠀⠄⡈⠀⠄⠂⠐⢀⠐⡈⠀⠌⠈⠐⠀⠂⠠⠀⠄⠂⠁⠀⠂⠁⠈⢀⠈⠀⠄⠁⠀⡀⠀⠠⠀⠂⠁⠀⡀⠐⠀⠈⠀⠀⠄⠀⠈⠀⠀⢀⠀⠀⠀⠀⡀⠀⡀⠀⠄⢀⠂⠨⢪⠪⡊⡎⡆⢇⢣⢱⢑⢅⢇⢕
⣸⢪⡺⡪⣎⢧⢳⡱⣕⢝⡎⣗⢝⡕⡧⡳⠕⢃⠃⠡⠐⠈⡈⠠⠐⠈⠐⡀⠄⠁⠄⠁⠌⢀⠐⠀⡀⠂⢁⠈⢀⠁⠄⠂⠀⠄⠂⠁⠠⠈⠀⠠⠀⠂⠀⠐⠀⠀⠐⠀⠄⠀⠐⠀⠀⠀⠐⠀⠈⠀⠀⡀⠁⠀⠐⠀⠀⠀⠁⢀⠀⠄⢀⠂⢈⠀⠠⠐⠀⢁⠃⢇⢇⢇⢇⢇⢇⢇⢎⢜
⣪⢣⡳⡹⡜⣎⢧⢫⢎⢧⡫⣎⠧⡋⢊⠠⠈⡀⠄⠁⡀⢁⠠⠐⠀⡁⢁⠠⠀⠡⠀⠡⠐⠠⠀⠂⠀⠂⠠⠐⠀⢀⠠⠀⠂⢀⠀⠄⠠⠀⡈⢀⠠⠐⠈⠀⠈⠀⢁⠠⠀⠁⠄⠂⠀⠁⠠⠀⠂⠀⠂⠀⢀⠈⠀⠀⠂⠁⠠⠀⢀⠂⠠⠐⠀⡐⠀⠂⡈⠀⠄⠂⡈⠊⠆⢇⢕⢜⢔⢕
⣪⢣⡳⡹⣪⢺⢜⢵⡹⣪⢚⠨⠀⠄⠠⠀⠄⠀⠄⠂⠀⠄⠠⠀⠂⠠⠀⠠⠀⢁⠈⡀⠂⡁⠠⠈⢀⠁⠄⠂⠈⠀⡀⠄⠂⠀⠄⠂⠀⠂⠀⡀⠀⡀⠠⠀⠁⢈⠀⢀⠀⠂⠀⠄⠀⠄⠠⠀⠠⠀⠐⠀⠠⠀⢈⠀⠐⠀⠐⢀⠠⠐⠀⠂⠁⡀⠄⢁⠠⠐⠀⠂⠠⠈⠐⡀⢐⠀⠅⠑
⡗⡵⡹⡪⣎⢧⡫⡺⠘⠠⢀⠐⠀⠐⠀⠄⠂⠀⠂⠀⠂⠐⠀⠐⠈⢀⠈⢀⠐⠀⡀⠄⠐⠀⠄⠂⠀⠄⠀⠂⠈⡀⢀⠠⠀⠁⡀⠐⠈⠀⠄⠀⠄⠀⡀⠄⠈⡀⠠⠀⡐⠀⠠⠐⠀⠠⠀⠄⠂⠀⠂⠁⢀⠈⢀⠀⡈⠀⢁⠀⠠⠐⠈⢀⠁⡀⠐⢀⠠⠐⠈⠀⠂⡈⠠⠀⠄⠂⠈⠄
➕➖✖️➗  ✨✨✨✨✨          ☀️☁️☔️❄️    ➖✖️➗ ✨✨           ✨✨   ✨   ➖✖️➗  ✨    
           
"""
print(jie)

# 之前在这里的 server, web, json, os, folder_paths, re 导入已移到文件顶部
# --- 新增 API 端点用于保存 API JSON ---
@server.PromptServer.instance.routes.post("/comfyui_to_webui/save_api_json")
async def save_api_json_route(request):
    try:
        data = await request.json()
        filename_base = data.get("filename")
        api_data_str = data.get("api_data")

        if not filename_base or not api_data_str:
            return web.json_response({"detail": "文件名或 API 数据缺失"}, status=400)

        # 清理文件名，防止路径遍历和非法字符
        safe_basename = os.path.basename(filename_base)
        # 进一步清理，只允许字母数字、下划线、连字符
        # 移除了点号，因为我们要添加 .json 后缀。如果用户输入了点号，它会被移除。
        safe_filename_stem = re.sub(r'[^\w\-]', '', safe_basename) 
        if not safe_filename_stem: # 如果清理后为空
            safe_filename_stem = "untitled_workflow_api"

        output_dir = folder_paths.get_output_directory()
        os.makedirs(output_dir, exist_ok=True)

        final_filename_json = f"{safe_filename_stem}.json"
        file_path = os.path.join(output_dir, final_filename_json)
        
        # 简单处理文件名冲突：如果存在则附加数字后缀
        counter = 1
        temp_filename_stem = safe_filename_stem
        while os.path.exists(file_path):
            temp_filename_stem = f"{safe_filename_stem}_{counter}"
            final_filename_json = f"{temp_filename_stem}.json"
            file_path = os.path.join(output_dir, final_filename_json)
            counter += 1
            if counter > 100: # 防止无限循环
                 return web.json_response({"detail": "尝试生成唯一文件名失败，请尝试其他名称。"}, status=500)


        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(api_data_str) # api_data_str 已经是格式化好的 JSON 字符串
        
        print(f"[ComfyUI_to_webui] API JSON saved to: {file_path}")
        return web.json_response({
            "message": f"API JSON 已成功保存到 {final_filename_json} (位于 output 目录)", 
            "filename": final_filename_json,
            "filepath": file_path
        })

    except json.JSONDecodeError:
        return web.json_response({"detail": "无效的 JSON 请求体"}, status=400)
    except Exception as e:
        error_message = f"保存 API JSON 时发生服务器内部错误: {str(e)}"
        print(f"[ComfyUI_to_webui] Error saving API JSON: {error_message}")
        return web.json_response({"detail": error_message}, status=500)

print("--- ComfyUI_to_webui: Registered API endpoint /comfyui_to_webui/save_api_json ---")
# --- 结束 API 端点 ---

WEB_DIRECTORY = "./js"

__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "WEB_DIRECTORY",
]

